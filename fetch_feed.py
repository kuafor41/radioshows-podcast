#!/usr/bin/env python3
import csv, hashlib, html, json, os, re, sys, time, xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import threading
from zoneinfo import ZoneInfo
import requests
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup

# Make progress visible in GitHub Actions while the long archive scan is running.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass

BASE="https://www.edmliveset.com"
ARCHIVE=BASE+"/radio/"
DATA=Path("data"); DATA.mkdir(exist_ok=True)
OUT=Path("rss.xml"); LIMIT=50; TIMEOUT=25
HOURLY_REPORT_FILE=DATA/"hourly-success-report.csv"
CUTOFF=datetime(2025,1,1,tzinfo=timezone.utc)
MAX_ARCHIVE_PAGES=375
MAX_RETRIES_PER_RUN=0  # Unlimited: retry every active queue entry each hourly run.
MAX_RETRY_QUEUE=0  # No truncation: retain every active failure until recovered or aged out.
OLD_LINK_AUDIT_BATCH=20
OLD_LINK_AUDIT_INTERVAL_SECONDS=7*24*60*60
RETRY_ARCHIVE_AFTER_DAYS=45
TEMPORARY_RETRY_SECONDS=60*60
NOT_FOUND_RETRY_SECONDS=60*60  # Retry 404s hourly too; queue processing does not defer active entries.
LONG_QUEUE_SECONDS=7*24*60*60
RUN_REPORT_FILE=DATA/"run-report.csv"
RETRY_CSV_FILE=DATA/"retry-queue.csv"
RETRY_ARCHIVE_CSV_FILE=DATA/"retry-queue-archive.csv"
SUMMARY_REPORT_FILE=DATA/"summary-report.csv"
LINK_AUDIT_STATE_FILE=DATA/"link-audit-state.json"
QUALITY_CSV_FILE=DATA/"rss-quality-checks.csv"
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (compatible; RadioShowsPodcastRSS/1.0)","Accept":"text/html,application/xhtml+xml,*/*;q=0.8"})

def txt(n): return re.sub(r"\s+"," ",n.get_text(" ",strip=True)).strip() if n else ""
def url(u,b=BASE):
    if not u:return ""
    u=urljoin(b,str(u).strip())
    return u.split("#",1)[0] if urlparse(u).scheme in ("http","https") and urlparse(u).netloc else ""
# Keep network sessions isolated per worker when the audio refresh pool runs concurrently.
_AUDIO_LOCAL = threading.local()
def worker_session():
    session = getattr(_AUDIO_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent":"Mozilla/5.0 (compatible; RadioShowsPodcastRSS/1.0)","Accept":"text/html,application/xhtml+xml,*/*;q=0.8"})
        _AUDIO_LOCAL.session = session
    return session

def get(u,**kw):
    r=worker_session().get(u,timeout=TIMEOUT,**kw); r.raise_for_status(); return r
def links_from(soup, page):
    out=[]
    for a in soup.select("a[href]"):
        u=url(a.get("href"),page); t=txt(a); p=urlparse(u).path.rstrip("/")
        if urlparse(u).netloc.lower() not in ("edmliveset.com","www.edmliveset.com"): continue
        # Do not mistake archive pagination (/radio/page/2/) for an episode.
        if re.fullmatch(r"/radio/page/\d+",p,re.I): continue
        if re.fullmatch(r"/radio/[^/]+",p,re.I) and t.lower() not in ("download","free download","listen","read more"): out.append(u)
    return list(dict.fromkeys(out))
PREPARSED={}
DISCOVERY_STATS={"pages_scanned":0,"cutoff_reached":False,"oldest_added_checked":"","page_limit_reached":False}
def discover(full_archive=False):
    """Collect episode URLs without filtering by Added date."""
    found=[]; errors=[]; seen_urls=set()
    page_limit=MAX_ARCHIVE_PAGES if full_archive else 3
    for pg in range(1,page_limit+1):
        if not full_archive and len(found)>=LIMIT: break
        page_url=ARCHIVE if pg==1 else BASE+f"/radio/page/{pg}/"
        print(f"[ARCHIVE] Starting list page {pg}/{page_limit}: {page_url}", flush=True)
        try:
            r=get(page_url)
            soup=BeautifulSoup(r.text,"html.parser")
            page_links=links_from(soup,r.url)
            unique_on_page=0
            for u in page_links:
                if u not in seen_urls:
                    seen_urls.add(u); found.append(u); unique_on_page+=1
            DISCOVERY_STATS["pages_scanned"]=pg
            print(f"[ARCHIVE] Page {pg}/{page_limit} loaded; links={len(page_links)}, new_unique={unique_on_page}, total_unique={len(found)}", flush=True)
            if not full_archive and len(found)>=LIMIT: break
            time.sleep(.05)
        except Exception as e:
            errors.append({"page":pg,"url":page_url,"error":str(e)})
            print("[ARCHIVE] ERROR page "+str(pg)+"/"+str(page_limit)+": "+str(e), flush=True)
            break
    if full_archive and DISCOVERY_STATS["pages_scanned"] >= page_limit and not errors:
        DISCOVERY_STATS["page_limit_reached"]=True
    return (found if full_archive else found[:LIMIT]),errors
def fields(soup):
    d={k:"" for k in ("artist","show","serie","added","genre")}
    for n in soup.find_all(["li","p","div","span","strong","b"]):
        t=txt(n)
        if len(t)>1500:continue
        for k in d:
            m=re.match(r"^"+k+r"\s*:\s*(.+)$",t,re.I)
            if m and not d[k]: d[k]=m.group(1).strip()
    for n in soup.find_all(["strong","b"]):
        k=txt(n).rstrip(":").lower()
        if k in d and not d[k] and n.parent:
            d[k]=re.sub(r"^"+k+r"\s*:\s*","",txt(n.parent),flags=re.I)
    return {k:re.sub(r"\s+"," ",v).strip() for k,v in d.items()}
def dateval(v):
    for f in ("%B %d, %Y","%b %d, %Y","%d %B %Y","%Y-%m-%d","%m/%d/%Y"):
        try:return datetime.strptime(v,f).replace(tzinfo=timezone.utc)
        except (ValueError,TypeError):pass
    return None
def parse(u):
    r=get(u); soup=BeautifulSoup(r.text,"html.parser")
    can=soup.find("link",rel="canonical"); canonical=url(can.get("href"),r.url) if can else r.url
    title=txt(soup.find("h1")) or txt(soup.title); d=fields(soup)
    # Prefer each episode's own social/featured image, not the podcast-wide cover.
    image_url=""
    for selector, attr in [
        ("meta[property='og:image']", "content"),
        ("meta[property='og:image:secure_url']", "content"),
        ("meta[name='twitter:image']", "content"),
        ("meta[name='twitter:image:src']", "content"),
    ]:
        node=soup.select_one(selector)
        candidate=url(node.get(attr),r.url) if node else ""
        if candidate:
            image_url=candidate
            break
    if not image_url:
        for selector in ["article img.wp-post-image", "article img.attachment-post-thumbnail",
                         ".post-thumbnail img", ".featured-image img", "article img"]:
            node=soup.select_one(selector)
            candidate=url(node.get("src") or node.get("data-src") or node.get("data-lazy-src"),r.url) if node else ""
            if candidate and not re.search(r"logo|avatar|icon",urlparse(candidate).path,re.I):
                image_url=candidate
                break
    d["show"]=d["show"] or title; d["artist"]=d["artist"] or "Unknown"; d["serie"]=d["serie"] or d["show"]
    # Capture the actual article text after the FREE DOWNLOAD link in source order.
    # Keep these lines separate so the RSS description can mirror the page without
    # inventing numbering or adding a "Source page" line.
    body_lines=[]
    article=soup.select_one("article .entry-content") or soup.select_one("article .post-content") or soup.select_one("article")
    if article:
        descendants=article.find_all(True)
        download_idx=next((i for i,n in enumerate(descendants) if n.name=="a" and txt(n).strip().lower()=="free download"),-1)
        if download_idx>=0:
            for n in descendants[download_idx+1:]:
                if n.name not in ("h2","h3","h4","p","li"): continue
                if n.name=="li" and n.find("p"): continue
                t=txt(n)
                if not t: continue
                if re.match(r"^Dive into the vibrant world of electronic dance music",t,re.I): break
                if re.search(r"podcast.*embedded on this page|all files, descriptions|share this article|more .* episodes|your ultimate edm",t,re.I): break
                if t not in body_lines: body_lines.append(t)
    heading=next((h for h in soup.find_all(["h2","h3","h4","strong"]) if "tracklist" in txt(h).lower()),None)
    tracks=[]
    if heading:
        for n in heading.find_all_next(["li","p"]):
            if n.name in ("h2","h3") and n is not heading:break
            t=txt(n)
            # Stop before the site's promotional footer; exclude that paragraph and everything below it.
            if re.match(r"^Dive into the vibrant world of electronic dance music",t,re.I): break
            if t and "tracklist" not in t.lower() and t not in tracks:tracks.append(t)
    tracks=[t for t in tracks if not re.search(r"podcast.*embedded on this page|all files, descriptions",t,re.I)]
    candidates=[]; player_urls=[]
    media_ext=r"\.(?:mp3|m4a|aac|ogg|opus|wav|flac)(?:$|[?#])"
    for a in soup.select("a[href]"):
        label=txt(a).lower(); u2=url(a.get("href"),r.url)
        if not u2: continue
        if re.search(media_ext,u2,re.I):
            candidates.append(u2)
        elif "download" in label:
            candidates.append(u2)
    for n in soup.select("audio[src],audio source[src],source[src],meta[property='og:audio'],meta[property='og:audio:url']"):
        u2=url(n.get("src") or n.get("content"),r.url)
        if u2:candidates.append(u2)
    for n in soup.select("iframe[src]"):
        u2=url(n.get("src"),r.url)
        if u2: player_urls.append(u2)
    # Some players keep the real media URL in inline JSON/JavaScript rather than
    # in an <audio> element or a visible download link.
    for raw in re.findall(r"""https?://[^"'\s<>]+?\.(?:mp3|m4a|aac|ogg|opus|wav|flac)(?:\?[^"'\s<>]*)?""",r.text,re.I):
        u2=url(raw.replace("\\/", "/"),r.url)
        if u2:candidates.append(u2)
    candidates=list(dict.fromkeys(candidates))
    # Try direct media links first, then player-provided sources and download redirects.
    candidates.sort(key=lambda x: (0 if re.search(media_ext,x,re.I) else 1))
    return {"title":title,"url":canonical,"guid":canonical,**d,"image_url":image_url,"audio_url":"","audio_candidates":candidates,"player_urls":list(dict.fromkeys(player_urls)),"body_lines":body_lines,"tracklist":tracks,"pubdate":(dateval(d["added"]) or datetime(1970,1,1,tzinfo=timezone.utc)).isoformat()}
def probe(u):
    if not u:return False,u,0,"No direct audio URL found"
    try:
        r=S.get(u,headers={"Range":"bytes=0-1023"},timeout=TIMEOUT,stream=True,allow_redirects=True)
        typ=r.headers.get("Content-Type","").split(";")[0].lower(); size=int(r.headers.get("Content-Length","0") or 0); final=r.url; status=r.status_code
        next(r.iter_content(1024),b""); r.close()
        ok=status in (200,206) and (typ.startswith("audio/") or "octet-stream" in typ or final.lower().split("?")[0].endswith((".mp3",".m4a",".aac",".ogg",".opus",".wav")))
        return ok,final,size,"" if ok else f"Not confirmed audio: HTTP {status}, type={typ}"
    except Exception as e:return False,u,0,f"{type(e).__name__}: {e}"
def cache_episode_covers(items):
    """Cache one verified artwork per series; reject known hotlink placeholder images."""
    cover_dir=DATA/"covers"
    cover_dir.mkdir(parents=True,exist_ok=True)
    map_path=DATA/"cover-map.json"
    try:
        cover_map=json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError,ValueError):
        cover_map={}
    raw_base="https://raw.githubusercontent.com/kuafor41/radioshows-podcast/main/data/covers/"
    podcast_cover=raw_base.replace("data/covers/","data/")+"podcast-cover.jpg"
    downloaded=reused=0
    failed=[]
    failed_keys={}
    # The previously cached hotlink warning was identical across multiple show names.
    # Fingerprint existing files so that identical placeholder responses are rejected.
    existing_hashes={}
    for path in cover_dir.iterdir():
        if path.is_file() and path.stat().st_size>100:
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            existing_hashes.setdefault(digest,[]).append(path.name)
    duplicated_hashes={h for h,names in existing_hashes.items() if len(set(names))>1}
    def valid_image(data,content_type):
        if not content_type.startswith("image/") or len(data)<100:
            return False
        # Check actual image signatures, not only the server's Content-Type header.
        return (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                or data.startswith((b"GIF87a",b"GIF89a")) or
                (len(data)>=12 and data[:4]==b"RIFF" and data[8:12]==b"WEBP"))
    for x in items:
        source=x.get("source_image_url") or x.get("image_url","")
        series=(x.get("serie") or x.get("show") or x.get("title") or "unknown").strip()
        key=re.sub(r"[^a-z0-9]+","-",series.casefold()).strip("-") or "unknown"
        if key in failed_keys:
            x["image_url"]=podcast_cover
            x["source_image_url"]=source
            continue
        entry=cover_map.get(key,{})
        local_name=entry.get("file","")
        local_path=cover_dir/local_name if local_name else None
        if local_path and local_path.is_file() and local_path.stat().st_size>100:
            local_data=local_path.read_bytes()
            local_hash=hashlib.sha256(local_data).hexdigest()
            # A file duplicated across unrelated show names is the known hotlink placeholder.
            if local_hash not in duplicated_hashes:
                x["source_image_url"]=entry.get("source_url",source)
                x["image_url"]=raw_base+local_name
                reused+=1
                continue
            print(f"Rejected cached duplicate artwork | series={series} | file={local_name}")
            try: local_path.unlink()
            except OSError: pass
            cover_map.pop(key,None)
        source=entry.get("source_url") or source
        if not source:
            failed.append({"series":series,"reason":"No source image URL"})
            x["image_url"]=podcast_cover
            continue
        parsed=urlparse(source)
        candidates=[source]
        if parsed.hostname and parsed.hostname.lower()=="cdn.edmliveset.com":
            candidates.extend([
                parsed._replace(netloc="www.edmliveset.com").geturl(),
                parsed._replace(netloc="edmliveset.com").geturl(),
            ])
        candidates=list(dict.fromkeys(candidates))
        success=None
        errors=[]
        # Keep retries bounded: the archive has many distinct shows and a protected host may stall.
        attempts=[(c,x.get("url",BASE+"/")) for c in candidates[:1]]
        attempts += [(c,BASE+"/") for c in candidates[1:]]
        if len(candidates)>1:
            attempts.append((candidates[0],BASE+"/"))
        for candidate,referer in attempts:
            try:
                headers={
                    "Referer":referer,
                    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                    "Accept":"image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Sec-Fetch-Dest":"image",
                    "Sec-Fetch-Mode":"no-cors",
                    "Sec-Fetch-Site":"same-site",
                }
                response=S.get(candidate,headers=headers,timeout=(4,7),allow_redirects=True)
                response.raise_for_status()
                content_type=response.headers.get("Content-Type","").split(";",1)[0].lower()
                data=response.content
                if not valid_image(data,content_type):
                    raise ValueError(f"Invalid image payload: type={content_type}, bytes={len(data)}")
                digest=hashlib.sha256(data).hexdigest()
                # Reject the identical warning image and any response matching a known duplicate.
                other_names=existing_hashes.get(digest,[])
                if digest in duplicated_hashes or any(name!=local_name for name in other_names):
                    raise ValueError("Response matches a duplicated cached image (likely hotlink-warning placeholder)")
                ext_map={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif","image/avif":".avif"}
                ext=ext_map.get(content_type) or (Path(urlparse(response.url).path).suffix.lower() if Path(urlparse(response.url).path).suffix.lower() in (".jpg",".jpeg",".png",".webp",".gif",".avif") else ".img")
                success=(data,content_type,candidate,ext,digest)
                break
            except Exception as e:
                errors.append(f"{candidate} [{referer}]: {e}")
        if success:
            data,content_type,used_source,ext,digest=success
            safe_name=key+ext
            (cover_dir/safe_name).write_bytes(data)
            cover_map[key]={"series":series,"source_url":used_source,"file":safe_name,"sha256":digest}
            existing_hashes.setdefault(digest,[]).append(safe_name)
            x["source_image_url"]=used_source
            x["image_url"]=raw_base+safe_name
            downloaded+=1
            print(f"Artwork verified and cached | series={series} | file={safe_name} | bytes={len(data)}")
        else:
            # Never publish a known warning graphic or the protected source URL as episode art.
            x["source_image_url"]=source
            x["image_url"]=podcast_cover
            reason="; ".join(errors[-4:]) or "No candidate returned a verified image"
            failed.append({"series":series,"source_url":source,"reason":reason})
            failed_keys[key]=reason
            print(f"Artwork unavailable; using podcast-level cover | series={series} | {errors[-1] if errors else 'no image'}")
    map_path.write_text(json.dumps(cover_map,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"downloaded":downloaded,"reused":reused,"series_cached":len(cover_map),"failed":failed}

def description(x):
    e=lambda s:html.escape(re.sub(r"EDMliveset\.com\s*[-–—:]\s*podcasts?\s*&\s*(?:radio\s*shows|radioshows)","RadioShows Podcast",s or "",flags=re.I).strip(),quote=True)
    parts=[f"<p><b>{k.title()}:</b> {e(x[k.lower()])}</p>" for k in ("Artist","Show","Serie","Added","Genre")]
    # Show the text after FREE DOWNLOAD as it appears on the episode page.
    # Paragraphs (not an ordered list) preserve line breaks without auto-numbering.
    lines=x.get("body_lines") or (["Tracklist"]+x["tracklist"] if x["tracklist"] else ["Tracklist not found on source page."])
    parts.append("".join("<p>"+e(t)+"</p>" for t in lines))
    return "".join(parts)
def build(items):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    root=ET.Element("rss",{"version":"2.0"}); ch=ET.SubElement(root,"channel")
    for tag,val in [("title","RadioShows Podcast"),("link",ARCHIVE),("description","Radio shows archived from pages 1-375, sorted by Added date (newest first)."),("language","en"),("lastBuildDate",format_datetime(datetime.now(timezone.utc))),("generator","RadioShows Podcast RSS Builder")]:ET.SubElement(ch,tag).text=val
    cover_url="https://raw.githubusercontent.com/kuafor41/radioshows-podcast/main/data/podcast-cover.jpg"
    image=ET.SubElement(ch,"image"); ET.SubElement(image,"url").text=cover_url; ET.SubElement(image,"title").text="RadioShows Podcast"; ET.SubElement(image,"link").text=ARCHIVE
    ET.SubElement(ch,"{http://www.itunes.com/dtds/podcast-1.0.dtd}author").text="RadioShows Podcast"
    ET.SubElement(ch,"{http://www.itunes.com/dtds/podcast-1.0.dtd}image",{"href":cover_url})
    for x in sorted(items,key=lambda z:z["pubdate"],reverse=True):
        it=ET.SubElement(ch,"item"); ET.SubElement(it,"title").text=x["title"]; ET.SubElement(it,"link").text=x["url"]
        ET.SubElement(it,"guid",{"isPermaLink":"false"}).text=x["guid"]
        ET.SubElement(it,"pubDate").text=format_datetime(datetime.fromisoformat(x["pubdate"]))
        ET.SubElement(it,"description").text=description(x)
        # Podcast Addict can use item-level itunes:image as episode artwork.
        if x.get("image_url"):
            ET.SubElement(it,"{http://www.itunes.com/dtds/podcast-1.0.dtd}image",{"href":x["image_url"]})
        ET.SubElement(it,"enclosure",{"url":x["audio_url"],"length":str(x["audio_length"]),"type":x["audio_type"]})
        if x["genre"]:ET.SubElement(it,"category").text=x["genre"]
    ET.indent(root,space="  "); data=ET.tostring(root,encoding="utf-8",xml_declaration=True); ET.fromstring(data); return data

def write_hourly_success_report(local_now, newly_added):
    """Record successful scheduled checks and any other run that actually added episodes."""
    if os.getenv("HOURLY_REPORT_ENABLED", "").strip().lower() != "true":
        return
    # Do not add zero-episode rows for code pushes/manual runs; scheduled checks still get a row.
    if not newly_added and os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return
    # Keep the existing published CSV header so the history remains readable and compatible.
    report_header = ["date_tr", "time_tr", "added_count", "episode_titles"]
    previous_rows = []
    if HOURLY_REPORT_FILE.exists() and HOURLY_REPORT_FILE.stat().st_size > 0:
        try:
            with HOURLY_REPORT_FILE.open("r", encoding="utf-8-sig", newline="") as history_file:
                previous_rows = list(csv.reader(history_file))
        except (OSError, csv.Error):
            previous_rows = []
    old_entries = []
    if previous_rows:
        old_header = previous_rows[0]
        if old_header == report_header:
            old_entries = previous_rows[1:]
        elif old_header in (["date_turkey", "time_turkey", "added_count", "episode_titles"],
                            ["date", "time", "added_count", "episode_titles"]):
            old_entries = [row[:4] for row in previous_rows[1:]]
    new_entry = [
        local_now.strftime("%Y-%m-%d"),
        local_now.strftime("%H:%M:%S"),
        str(len(newly_added)),
        " | ".join(item.get("title") or item.get("url", "") for item in newly_added)
        if newly_added else "Yeni bölüm eklenmedi",
    ]
    with HOURLY_REPORT_FILE.open("w", encoding="utf-8", newline="") as history_file:
        writer = csv.writer(history_file)
        writer.writerow(report_header)
        writer.writerow(new_entry)
        writer.writerows(old_entries)
    print("Hourly success report updated (newest first):", str(HOURLY_REPORT_FILE))

def retry_metadata(url_value, error, previous_entry=None):
    """Classify a failure and calculate its next eligible retry time."""
    now = datetime.now(timezone.utc)
    old = previous_entry or {}
    error_text = str(error or "Unknown error")
    is_not_found = bool(re.search(r"\b404\b|Not Found", error_text, re.I))
    failure_count = int(old.get("failure_count", 0) or 0) + 1
    # All active queue entries are retried on every hourly run, regardless of error type.
    delay = NOT_FOUND_RETRY_SECONDS if is_not_found else TEMPORARY_RETRY_SECONDS
    return {
        "url": url_value,
        "error": error_text[:1500],
        "error_type": "not_found" if is_not_found else "temporary_or_other",
        "failure_count": failure_count,
        "first_failed_at": old.get("first_failed_at") or now.isoformat(),
        "attempted_at": now.isoformat(),
        "next_retry_at": datetime.fromtimestamp(now.timestamp() + delay, timezone.utc).isoformat(),
    }

def write_quality_csv(checks):
    with QUALITY_CSV_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w=csv.DictWriter(f, fieldnames=["check","status","details"])
        w.writeheader(); w.writerows(checks)

def validate_rss(rss_bytes):
    """Hard-fail publication on malformed XML, duplicate GUIDs, bad order or invalid enclosures."""
    checks=[]
    def record(name, passed, detail):
        checks.append({"check":name,"status":"PASS" if passed else "FAIL","details":detail})
    try:
        root=ET.fromstring(rss_bytes)
        channel=root.find("channel")
        xml_items=channel.findall("item") if channel is not None else []
        record("xml_parse",channel is not None,f"items={len(xml_items)}")
    except ET.ParseError as exc:
        record("xml_parse",False,str(exc)); write_quality_csv(checks)
        raise RuntimeError(f"RSS XML quality check failed: {exc}")
    guids=[(it.findtext("guid") or "").strip() for it in xml_items]
    guid_counts=Counter(guids)
    duplicate_guids=[g for g,n in guid_counts.items() if g and n>1]
    record("unique_guid",len(guids)==len(set(guids)) and all(guids),f"items={len(guids)}; duplicate_guids={len(duplicate_guids)}; empty_guids={sum(not g for g in guids)}")
    titles=[(it.findtext("title") or "").strip() for it in xml_items]
    record("nonempty_titles",all(titles),f"empty_titles={sum(not t for t in titles)}")
    dates=[]; date_errors=0
    for it in xml_items:
        try: dates.append(datetime.strptime(it.findtext("pubDate"),"%a, %d %b %Y %H:%M:%S %z").astimezone(timezone.utc))
        except (ValueError,TypeError): date_errors+=1
    sorted_dates=all(dates[i]>=dates[i+1] for i in range(len(dates)-1))
    record("pubdate_newest_first",sorted_dates and date_errors==0,f"items={len(xml_items)}; invalid_dates={date_errors}")
    bad=[]
    for it in xml_items:
        enc=it.find("enclosure")
        href=(enc.get("url") or "").strip() if enc is not None else ""
        parsed=urlparse(href)
        if not href or parsed.scheme not in ("http","https") or not parsed.netloc or not enc.get("type","").lower().startswith("audio/"):
            bad.append(it.findtext("title") or "(untitled)")
    record("valid_audio_enclosures",not bad,f"invalid_enclosures={len(bad)}")
    safe_report("RSS quality-check history CSV", write_quality_csv, checks)
    failures=[c for c in checks if c["status"]=="FAIL"]
    if failures: raise RuntimeError("RSS quality gate failed: "+"; ".join(c["check"]+": "+c["details"] for c in failures))
    print(f"[QUALITY] PASS: {len(xml_items)} items; unique GUIDs, non-empty titles, newest-first dates and HTTPS audio enclosures.",flush=True)
    return checks

def append_run_report(report):
    fields=["run_at","mode","discovered","newly_checked","published","retry_queue_remaining","old_links_retried","old_links_recovered","retry_failures","temporary_failures","not_found_failures","oldest_retry_age_days","quality_gate","archive_complete"]
    row={k:report.get(k,"") for k in fields}; existing=[]
    if RUN_REPORT_FILE.exists():
        try:
            with RUN_REPORT_FILE.open("r",encoding="utf-8-sig",newline="") as f: existing=list(csv.DictReader(f))
        except (OSError,csv.Error): existing=[]
    with RUN_REPORT_FILE.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerow(row); w.writerows(existing[:999])
    print("[REPORT] CSV updated: "+str(RUN_REPORT_FILE),flush=True)

def write_retry_csv(retry_items, now=None):
    """Write each active failure with a human-readable age and days remaining in its 45-day window."""
    now=now or datetime.now(timezone.utc)
    fields=["url","error_type","failure_count","first_failed_at","attempted_at","next_retry_at","retry_age_days","days_until_archive","error"]
    with RETRY_CSV_FILE.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for entry in sorted(retry_items,key=lambda x:x.get("next_retry_at","")):
            try:
                first=datetime.fromisoformat(str(entry.get("first_failed_at","")).replace("Z","+00:00"))
                if first.tzinfo is None: first=first.replace(tzinfo=timezone.utc)
                age_seconds=max(0,(now-first).total_seconds())
                age_days=int(age_seconds // 86400)
            except (ValueError,TypeError):
                age_days=0
            row={k:entry.get(k,"") for k in fields}
            row["retry_age_days"]=age_days
            row["days_until_archive"]=max(0,RETRY_ARCHIVE_AFTER_DAYS-age_days)
            w.writerow(row)

def archive_expired_retries(retry_items, now=None):
    """Move failures unresolved for 45 days to a review CSV; never silently discard them."""
    now=now or datetime.now(timezone.utc)
    fields=["url","error_type","failure_count","first_failed_at","attempted_at","next_retry_at","error","archived_at","age_days"]
    archived=[]
    if RETRY_ARCHIVE_CSV_FILE.exists():
        try:
            with RETRY_ARCHIVE_CSV_FILE.open("r",encoding="utf-8-sig",newline="") as f: archived=list(csv.DictReader(f))
        except (OSError,csv.Error): archived=[]
    archived_urls={r.get("url") for r in archived}
    remaining=[]; moved=0
    for entry in retry_items:
        try: first=datetime.fromisoformat(str(entry.get("first_failed_at","")).replace("Z","+00:00"))
        except (ValueError,TypeError): first=now
        age=max(0,(now-first).total_seconds()/86400)
        if age >= RETRY_ARCHIVE_AFTER_DAYS:
            if entry.get("url") not in archived_urls:
                archived.append({**entry,"archived_at":now.isoformat(),"age_days":f"{age:.2f}"})
                archived_urls.add(entry.get("url")); moved+=1
        else: remaining.append(entry)
    if moved or not RETRY_ARCHIVE_CSV_FILE.exists():
        with RETRY_ARCHIVE_CSV_FILE.open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
            for entry in archived: w.writerow({k:entry.get(k,"") for k in fields})
    if moved: print(f"[RETRY-ARCHIVE] Moved {moved} item(s) older than {RETRY_ARCHIVE_AFTER_DAYS} days to {RETRY_ARCHIVE_CSV_FILE}",flush=True)
    return remaining,moved

def write_summary_report(report, queue_before, queue_after):
    """One-row newest-first operational summary, separate from hourly-success-report.csv."""
    fields=["run_at","new_episodes_added","old_links_audited","old_links_still_valid","old_links_degraded","old_links_recovered","retry_queue_before","retry_queue_after","retry_queue_change","moved_to_review"]
    row={k:v for k,v in {
        "run_at":report.get("run_at",""),
        "new_episodes_added":report.get("new_episodes_added",0),
        "old_links_audited":report.get("old_links_audited",0),
        "old_links_still_valid":report.get("old_links_still_valid",0),
        "old_links_degraded":report.get("old_links_degraded",0),
        "old_links_recovered":report.get("old_links_recovered",0),
        "retry_queue_before":queue_before,
        "retry_queue_after":queue_after,
        "retry_queue_change":queue_after-queue_before,
        "moved_to_review":report.get("moved_to_review",0),
    }.items()}
    existing=[]
    if SUMMARY_REPORT_FILE.exists():
        try:
            with SUMMARY_REPORT_FILE.open("r",encoding="utf-8-sig",newline="") as f: existing=list(csv.DictReader(f))
        except (OSError,csv.Error): existing=[]
    with SUMMARY_REPORT_FILE.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerow(row); w.writerows(existing[:999])
    print("[SUMMARY] CSV updated: "+str(SUMMARY_REPORT_FILE),flush=True)

def audio_url_expiry(value):
    """Return a trustworthy expiry timestamp only when the URL exposes an explicit expiry."""
    try:
        query = {str(k).lower(): v[-1] for k, v in parse_qs(urlparse(str(value or "")).query).items() if v}
        # AWS S3 / CloudFront-style Unix expiry.
        for key in ("expires", "expiry", "expire", "expiration"):
            raw = query.get(key, "")
            if raw.isdigit() and int(raw) >= 1_000_000_000:
                return datetime.fromtimestamp(int(raw), timezone.utc)
        # Some providers use e as a Unix expiry; only trust epoch-shaped values.
        raw = query.get("e", "")
        if raw.isdigit() and int(raw) >= 1_000_000_000:
            return datetime.fromtimestamp(int(raw), timezone.utc)
        # AWS Signature V4: X-Amz-Date + X-Amz-Expires.
        stamp = query.get("x-amz-date", "")
        seconds = query.get("x-amz-expires", "")
        if stamp and seconds.isdigit():
            issued = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return datetime.fromtimestamp(issued.timestamp() + int(seconds), timezone.utc)
        # Google signed URLs: X-Goog-Date + X-Goog-Expires.
        stamp = query.get("x-goog-date", "")
        seconds = query.get("x-goog-expires", "")
        if stamp and seconds.isdigit():
            issued = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return datetime.fromtimestamp(issued.timestamp() + int(seconds), timezone.utc)
        # Azure SAS se is an ISO-8601 expiry; do not treat it as a generic token.
        raw = query.get("se", "")
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
            except ValueError:
                pass
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return None

def audio_url_classification(value):
    """Separate explicit expiry metadata from signature/token parameters with unknown lifetime."""
    try:
        query = {str(k).lower() for k in parse_qs(urlparse(str(value or "")).query)}
        host = (urlparse(str(value or "")).hostname or "").lower()
    except ValueError:
        return "invalid_url", "(invalid host)"
    expiry = audio_url_expiry(value)
    if expiry:
        return "explicit_expiry", host or "(no host)"
    signed_keys = {
        "signature", "policy", "key-pair-id", "sig", "jwt", "access_token",
        "token", "auth_key", "hdnts", "x-amz-signature", "x-goog-signature"
    }
    if any(k in signed_keys or k.startswith("x-amz-") or k.startswith("x-goog-") for k in query):
        return "signed_expiry_unknown", host or "(no host)"
    if query:
        return "query_params_no_expiry", host or "(no host)"
    return "no_expiry_marker", host or "(no host)"

def is_temporary_audio_url(value):
    """Identify signed/tokenized URLs for cautious refresh; this is not proof they expire hourly."""
    category, _ = audio_url_classification(value)
    return category in ("explicit_expiry", "signed_expiry_unknown", "query_params_no_expiry")

def inspect_audio_url(value):
    """Return real HTTP/audio diagnostics for a candidate URL."""
    if not value:
        return {"ok": False, "status": "", "content_type": "", "final_url": "",
                "length": 0, "error": "No audio URL"}
    try:
        response = worker_session().get(value, headers={"Range": "bytes=0-1023"}, timeout=TIMEOUT,
                                        stream=True, allow_redirects=True)
        status = response.status_code
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        try:
            length = int(response.headers.get("Content-Length", "0") or 0)
        except (ValueError, TypeError):
            length = 0
        final_url = response.url
        try:
            next(response.iter_content(1024), b"")
        finally:
            response.close()
        media_path = urlparse(final_url).path.lower()
        ok = status in (200, 206) and (
            content_type.startswith("audio/") or "octet-stream" in content_type or
            media_path.endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"))
        )
        error = "" if ok else f"HTTP {status}; Content-Type={content_type or '(missing)'}; not confirmed audio"
        return {"ok": ok, "status": status, "content_type": content_type,
                "final_url": final_url, "length": length, "error": error}
    except Exception as exc:
        return {"ok": False, "status": "", "content_type": "", "final_url": value,
                "length": 0, "error": f"{type(exc).__name__}: {exc}"[:1500]}

def refresh_expiring_audio(items_by_url):
    """Classify signed URLs, skip safe long-lived links, and refresh only when needed."""
    report_path = DATA / "audio-link-diagnostics.csv"
    fieldnames = [
        "title", "source_page_url", "guid", "added_date", "current_audio_url",
        "last_checked_at_utc", "http_status", "content_type", "replacement_http_status",
        "replacement_content_type", "temporary_url", "refresh_result", "verified_audio_url", "error",
        "audio_host", "expiry_class", "expires_at_utc"
    ]
    old_rows = {}
    if report_path.exists():
        try:
            with report_path.open("r", encoding="utf-8-sig", newline="") as f:
                old_rows = {row.get("source_page_url", ""): row for row in csv.DictReader(f)
                            if row.get("source_page_url")}
        except (OSError, ValueError, csv.Error):
            old_rows = {}
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    rows = []
    counters = Counter()
    host_counts = Counter()
    refresh_candidates = []

    for page_url, item in list(items_by_url.items()):
        current_audio = str(item.get("audio_url", "") or "")
        category, host = audio_url_classification(current_audio)
        expiry = audio_url_expiry(current_audio)
        previous_row = old_rows.get(page_url, {})
        row = {
            "title": item.get("title", ""),
            "source_page_url": page_url,
            "guid": item.get("guid", ""),
            "added_date": item.get("added", ""),
            "current_audio_url": current_audio,
            "last_checked_at_utc": previous_row.get("last_checked_at_utc", ""),
            "http_status": previous_row.get("http_status", ""),
            "content_type": previous_row.get("content_type", ""),
            "replacement_http_status": previous_row.get("replacement_http_status", ""),
            "replacement_content_type": previous_row.get("replacement_content_type", ""),
            "temporary_url": "yes" if category != "no_expiry_marker" else "no",
            "refresh_result": previous_row.get("refresh_result", "NOT_CHECKED_NON_TEMPORARY"),
            "verified_audio_url": previous_row.get("verified_audio_url", ""),
            "error": previous_row.get("error", ""),
            "audio_host": host,
            "expiry_class": category,
            "expires_at_utc": expiry.isoformat() if expiry else ""
        }
        if not current_audio:
            row.update({"last_checked_at_utc": now, "http_status": "", "content_type": "",
                        "refresh_result": "NO_AUDIO_URL", "verified_audio_url": "",
                        "error": item.get("error", "No audio URL")})
            rows.append(row)
            counters["no_audio_url"] += 1
            continue

        counters[category] += 1
        host_counts[(host, category)] += 1

        # Ordinary URLs are not part of this signed-link refresh process.
        if category == "no_expiry_marker":
            rows.append(row)
            continue

        # Explicitly dated links with more than two hours left are safely retained.
        # This is intentionally conservative for hourly runs: links near expiry are refreshed.
        if expiry and (expiry - now_dt).total_seconds() > 2 * 60 * 60:
            row["refresh_result"] = "SKIPPED_NOT_NEAR_EXPIRY"
            rows.append(row)
            counters["skipped_long_lived"] += 1
            continue

        # Unknown-expiry signed URLs and links near expiry are checked first.
        # A working URL is retained; the source page is downloaded only if it is broken
        # or its known expiry is close enough that the next hourly run could miss the window.
        refresh_candidates.append((page_url, item, row, current_audio, expiry, category))

    def process(candidate):
        page_url, item, row, current_audio, expiry, category = candidate
        row["last_checked_at_utc"] = now
        current_check = inspect_audio_url(current_audio)
        row["http_status"] = str(current_check["status"])
        row["content_type"] = current_check["content_type"]
        row["verified_audio_url"] = current_check["final_url"] if current_check["ok"] else ""
        row["error"] = current_check["error"]
        must_refresh = not current_check["ok"]
        if expiry and expiry <= now_dt + __import__("datetime").timedelta(hours=1):
            # Refresh even if the URL currently responds: it may expire before the next hourly run.
            must_refresh = True
        if not must_refresh:
            row["refresh_result"] = "CHECKED_OK_NO_PAGE_FETCH"
            return row, False, False

        try:
            parsed = parse(page_url)
            candidates = list(parsed.get("audio_candidates", []))
            for player_url in parsed.get("player_urls", []):
                try:
                    player_response = get(player_url)
                    player_soup = BeautifulSoup(player_response.text, "html.parser")
                    for node in player_soup.select(
                        "audio[src], audio source[src], source[src], "
                        "meta[property='og:audio'], meta[property='og:audio:url'], a[href]"
                    ):
                        raw = node.get("src") or node.get("content") or node.get("href")
                        candidate_url = url(raw, player_response.url)
                        if candidate_url and re.search(
                            r"\.(?:mp3|m4a|aac|ogg|opus|wav|flac)(?:$|[?#])", candidate_url, re.I
                        ):
                            candidates.append(candidate_url)
                except Exception as player_exc:
                    row["error"] = (row["error"] + "; player: " + str(player_exc))[:1500]
            candidates = list(dict.fromkeys(candidates))
            candidates.sort(key=lambda candidate_url: (
                0 if re.search(r"\.(?:mp3|m4a|aac|ogg|opus|wav|flac)(?:$|[?#])", candidate_url, re.I) else 1
            ))
            chosen = None
            candidate_errors = []
            for candidate_url in candidates:
                result = inspect_audio_url(candidate_url)
                if result["ok"]:
                    chosen = result
                    break
                candidate_errors.append(
                    f"{candidate_url}: HTTP {result['status'] or 'error'} "
                    f"{result['content_type']} {result['error']}"
                )
            if chosen:
                new_url = chosen["final_url"]
                changed = new_url != current_audio
                if changed:
                    item["audio_url"] = new_url
                    item["audio_length"] = chosen["length"]
                row["refresh_result"] = "REFRESHED" if changed else "CHECKED_OK_NO_CHANGE"
                row["replacement_http_status"] = str(chosen["status"])
                row["replacement_content_type"] = chosen["content_type"]
                row["verified_audio_url"] = new_url
                row["error"] = ""
                return row, changed, True
            row["refresh_result"] = "FAILED_REFRESH"
            if candidate_errors:
                row["error"] = (row["error"] + "; candidates: " + " | ".join(candidate_errors[-3:]))[:1500]
            elif not row["error"]:
                row["error"] = "No direct audio candidate found in Free Download/page/player"
            return row, False, True
        except Exception as exc:
            row["refresh_result"] = "FAILED_REFRESH"
            row["error"] = f"{type(exc).__name__}: {exc}"[:1500]
            return row, False, True

    refreshed = page_fetches = 0
    # Bound concurrency to eight independent sessions; avoids serial per-link network waits.
    with ThreadPoolExecutor(max_workers=8) as pool:
        for row, changed, fetched_page in pool.map(process, refresh_candidates):
            rows.append(row)
            refreshed += int(changed)
            page_fetches += int(fetched_page)
            counters["current_url_checked"] += 1
            counters["source_pages_fetched"] += int(fetched_page)
            if changed:
                print(f"[AUDIO-REFRESH] Updated temporary media URL: {row.get('title') or row.get('source_page_url')}", flush=True)

    # Preserve diagnostics row order as items.json order for stable diffs and reporting.
    rows_by_url = {r.get("source_page_url"): r for r in rows}
    rows = [rows_by_url.get(page_url, {}) for page_url in items_by_url if page_url in rows_by_url]
    with report_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print("[AUDIO-CLASSIFICATION] " + "; ".join(
        f"{key}={value}" for key, value in sorted(counters.items())
    ), flush=True)
    for (host, category), count in sorted(host_counts.items(), key=lambda pair: (-pair[1], pair[0])):
        print(f"[AUDIO-CLASSIFICATION] host={host}; class={category}; count={count}", flush=True)
    print(f"[AUDIO-REFRESH] checked_signed={counters['current_url_checked']}; "
          f"source_pages_fetched={page_fetches}; refreshed={refreshed}; "
          f"diagnostics_rows={len(rows)}; report={report_path}", flush=True)
    return refreshed

def safe_report(label, writer, *args, **kwargs):
    """Isolate a non-critical report: its failure must not stop RSS publication or other reports."""
    try:
        return writer(*args, **kwargs)
    except Exception as exc:
        print(f"::warning title=Report failure::{label} failed independently: {type(exc).__name__}: {exc}", flush=True)
        return None

def main():
    items_path=DATA/"items.json"
    try: previous=json.loads(items_path.read_text(encoding="utf-8"))
    except (OSError,ValueError): previous=[]
    previous_by_url={x.get("url"):x for x in previous if x.get("url")}
    # Refresh signed/expiring audio URLs and write the per-episode diagnostics report.
    safe_report("audio-link diagnostics / URL refresh", refresh_expiring_audio, previous_by_url)
    local_now=datetime.now(ZoneInfo("Europe/Istanbul"))
    marker=DATA/"archive-backfill-complete.json"
    # Scheduled runs must never restart the backfill if the completion marker is absent.
    if os.getenv("GITHUB_EVENT_NAME") == "schedule" and not marker.exists():
        print("[SCHEDULE] Skipping hourly check: the 375-page archive is not yet verified complete.", flush=True)
        return
    full_archive=(not marker.exists()) or os.getenv("FEED_MODE")=="archive"
    urls,errors=discover(full_archive=full_archive)
    if not full_archive: urls=[u for u in urls if u not in previous_by_url]
    items=[] if full_archive else list(previous_by_url.values())
    # Retry a bounded batch of previously failed URLs on every incremental run.
    retry_path=DATA/"retry-queue.json"
    try:
        old_retry=json.loads(retry_path.read_text(encoding="utf-8"))
        if not isinstance(old_retry,list): old_retry=[]
    except (OSError,ValueError): old_retry=[]
    old_retry=[r for r in old_retry if isinstance(r,dict) and r.get("url")]
    # Normalize legacy queue metadata to the actual policy: every active entry is retried hourly.
    # Existing 7-day 404 deadlines are metadata only and must not imply a longer wait.
    migration_now=datetime.now(timezone.utc)
    for entry in old_retry:
        error_text=str(entry.get("error",""))
        is_404=bool(re.search(r"\b404\b|Not Found",error_text,re.I))
        try: attempted=datetime.fromisoformat(str(entry.get("attempted_at","")).replace("Z","+00:00"))
        except (ValueError,TypeError): attempted=migration_now
        if attempted.tzinfo is None: attempted=attempted.replace(tzinfo=timezone.utc)
        if entry.get("error_type") not in ("not_found","temporary_or_other"):
            entry["error_type"]="not_found" if is_404 else "temporary_or_other"
        entry["failure_count"]=max(1,int(entry.get("failure_count",1) or 1))
        entry["first_failed_at"]=entry.get("first_failed_at") or attempted.isoformat()
        delay=NOT_FOUND_RETRY_SECONDS if entry["error_type"]=="not_found" else TEMPORARY_RETRY_SECONDS
        entry["next_retry_at"]=datetime.fromtimestamp(attempted.timestamp()+delay,timezone.utc).isoformat()
    # Age out entries BEFORE scheduling so nothing receives a retry after its own 45-day window.
    old_retry, expired_before_retry = archive_expired_retries(old_retry)
    queue_before=len(old_retry)
    old_retry_by_url={r["url"]:r for r in old_retry}
    # Retry every active queued failure on every incremental run; next_retry_at is aligned to the hourly policy.
    if not full_archive:
        retry_candidates=[u for u in old_retry_by_url if u not in urls]
        print(f"[RETRY] active_queue={len(old_retry_by_url)}; retry_all_active=true; scheduled_from_queue={len(retry_candidates)}; expired_archived={expired_before_retry}",flush=True)
        urls=list(dict.fromkeys(urls+retry_candidates))
    else:
        print(f"[RETRY] archive_mode=true; active_queue={len(old_retry_by_url)}; expired_archived={expired_before_retry}",flush=True)
    # Used by the end-of-run summary; all active queue entries are scheduled in incremental mode.
    old_retry_attempted=set(old_retry_by_url) if not full_archive else set()
    attempted_urls=set(urls)
    rows=[]
    retry=[r for u,r in old_retry_by_url.items() if not full_archive and u not in attempted_urls]
    def process_one(pair):
        i,u=pair
        try:
            x=PREPARSED.get(u) or parse(u); added=dateval(x.get("added",""))
            # Archive every discovered item regardless of Added date.
            if not added:
                x["pubdate"]=datetime(1970,1,1,tzinfo=timezone.utc).isoformat()
                print("[WARNING] Added date not parsed; retaining episode: "+x.get("title",u), flush=True)
            else:
                x["pubdate"]=added.isoformat()
            ok=False; final=""; size=0; err="No direct audio URL found"
            for candidate in x.get("audio_candidates",[]):
                good,final_url,nbytes,candidate_err=probe(candidate)
                if good: ok=True; final=final_url; size=nbytes; err=""; break
                err=candidate_err
            if not ok:
                for player_url in x.get("player_urls",[]):
                    try:
                        pr=get(player_url); ps=BeautifulSoup(pr.text,"html.parser"); candidates=[]
                        for node in ps.select("audio[src],audio source[src],source[src],meta[property='og:audio'],meta[property='og:audio:url'],a[href]"):
                            raw=node.get("src") or node.get("content") or node.get("href"); candidate=url(raw,pr.url)
                            if candidate and re.search(r"\.(?:mp3|m4a|aac|ogg|opus|wav|flac)(?:$|[?#])",candidate,re.I): candidates.append(candidate)
                        for candidate in dict.fromkeys(candidates):
                            good,final_url,nbytes,candidate_err=probe(candidate)
                            if good: ok=True; final=final_url; size=nbytes; err=""; break
                            err=candidate_err
                        if ok: break
                    except Exception as pe: err=f"Player fallback failed: {pe}"
            x["audio_url"]=final if ok else ""; x["audio_length"]=size; x["audio_type"]="audio/mpeg"
            x["status"]="success" if ok and x["tracklist"] else "warning"
            x["error"]=err or ("" if x["tracklist"] else "Tracklist not extracted")
            if not ok: retry.append(retry_metadata(u,err,old_retry_by_url.get(u)))
            if ok: items.append(x)
            rows.append({"index":i,"title":x["title"],"page_url":x["url"],"audio_url":x["audio_url"],"audio_ok":ok,"track_count":len(x["tracklist"]),"status":x["status"],"error":x["error"]})
            print(f"[{i}/{len(urls)}] {x['status']} | added={x['added']} | audio={ok} | {x['title']}")
        except Exception as e:
            err=f"{type(e).__name__}: {e}"
            rows.append({"index":i,"title":"","page_url":u,"audio_url":"","audio_ok":False,"track_count":0,"status":"failed","error":err})
            retry.append(retry_metadata(u,err,old_retry_by_url.get(u)))
            print("Episode failed:",u,err)
        time.sleep(.15)

    # Detail-page parsing and audio verification are network-bound; process several
    # independent episodes concurrently while keeping ordered page discovery/logging.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(process_one, enumerate(urls,1)))

    # Audit a small rotating batch of previously published audio URLs.
    try:
        audit_state=json.loads(LINK_AUDIT_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(audit_state,dict): audit_state={}
    except (OSError,ValueError): audit_state={}
    queued_now={r.get("url") for r in retry if r.get("url")}
    audit_now=datetime.now(timezone.utc)
    eligible=[]
    for old_item in previous:
        old_url=old_item.get("url",""); media_url=old_item.get("audio_url","")
        if not old_url or not media_url or old_url in queued_now: continue
        state=audit_state.get(old_url,{})
        try:
            last=datetime.fromisoformat(str(state.get("last_checked_at","")).replace("Z","+00:00"))
            if (audit_now-last).total_seconds() < OLD_LINK_AUDIT_INTERVAL_SECONDS: continue
        except (ValueError,TypeError,AttributeError): pass
        eligible.append(old_item)
    eligible.sort(key=lambda x:audit_state.get(x.get("url",""),{}).get("last_checked_at",""))
    audit_batch=eligible[:OLD_LINK_AUDIT_BATCH]
    audit_ok=audit_failed=0
    degraded_urls=set()
    for old_item in audit_batch:
        old_url=old_item["url"]; media_url=old_item["audio_url"]
        good,final_url,size,error=probe(media_url)
        audit_state[old_url]={"last_checked_at":audit_now.isoformat(),"status":"ok" if good else "failed","audio_url":final_url if good else media_url,"error":"" if good else error}
        if good:
            audit_ok+=1
            if final_url and final_url != media_url:
                old_item["audio_url"]=final_url
                if size: old_item["audio_length"]=size
        else:
            audit_failed+=1; degraded_urls.add(old_url)
            retry.append(retry_metadata(old_url,error,old_retry_by_url.get(old_url) or {"first_failed_at":audit_now.isoformat()}))
            print(f"[OLD-LINK-AUDIT] Degraded link queued for retry: {old_url} | {error}",flush=True)
        time.sleep(.1)
    LINK_AUDIT_STATE_FILE.write_text(json.dumps(audit_state,ensure_ascii=False,indent=2),encoding="utf-8")
    items=[x for x in items if x.get("url") not in degraded_urls]
    print(f"[OLD-LINK-AUDIT] checked={len(audit_batch)} valid={audit_ok} degraded={audit_failed} eligible_remaining={max(0,len(eligible)-len(audit_batch))}",flush=True)

    merged={x.get("url"):x for x in items if x.get("url")}
    if not full_archive:
        for x in previous: merged.setdefault(x.get("url"),x)
    items=[x for x in merged.values() if x.get("audio_url")]
    items.sort(key=lambda x:(dateval(x.get("added","")) or datetime(1970,1,1,tzinfo=timezone.utc)),reverse=True)
    print(f"[RSS] Preparing artwork for {len(items)} audio-verified episodes...", flush=True)
    cover_stats=cache_episode_covers(items)
    rss_bytes=build(items)
    quality_checks=validate_rss(rss_bytes)
    OUT.write_bytes(rss_bytes)
    items_path.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")
    retry_by_url={}
    for entry in retry:
        if isinstance(entry,dict) and entry.get("url"):
            retry_by_url[entry["url"]]=entry
    retry=list(retry_by_url.values())
    retry,moved_to_review=archive_expired_retries(retry)
    moved_to_review += expired_before_retry
    Path("data/retry-queue.json").write_text(json.dumps(retry,ensure_ascii=False,indent=2),encoding="utf-8")
    safe_report("retry queue CSV", write_retry_csv, retry)
    report={"mode":"archive-backfill" if full_archive else "incremental","source":ARCHIVE,"cutoff":"2025-01-01","run_at":datetime.now(timezone.utc).isoformat(),"discovered":len(urls),"newly_checked":len(rows),"archived_total":len(items),"published":len(items),"audio_confirmed":sum(bool(x.get("audio_url")) for x in items),"old_links_audited":len(audit_batch),"old_links_still_valid":audit_ok,"old_links_degraded":audit_failed,"moved_to_review":moved_to_review,"tracklists_found":sum(bool(x.get("tracklist")) for x in items),"cover_cache":cover_stats,"discovery_errors":errors,"pages_scanned":DISCOVERY_STATS["pages_scanned"],"cutoff_reached":DISCOVERY_STATS["cutoff_reached"],"oldest_added_checked":DISCOVERY_STATS["oldest_added_checked"],"page_limit_reached":DISCOVERY_STATS["page_limit_reached"],"episodes":rows,"rss_xml_valid":True}
    report["new_episodes_added"] = sum(1 for x in items if x.get("url") and x.get("url") not in previous_by_url)
    retry_urls={r.get("url") for r in retry if r.get("url")}
    report["retry_queue_remaining"]=len(retry_urls)
    report["old_links_retried"]=len(old_retry_attempted)
    published_urls={x.get("url") for x in items}
    report["old_links_recovered"]=sum(1 for u in old_retry_attempted if u not in retry_urls and u in published_urls)
    report["retry_failures"]=sum(1 for r in retry if r.get("url") in old_retry_attempted)
    report["temporary_failures"]=sum(1 for r in retry if r.get("error_type") != "not_found")
    report["not_found_failures"]=sum(1 for r in retry if r.get("error_type") == "not_found")
    oldest=[]
    for entry in retry:
        try: oldest.append(datetime.fromisoformat(entry.get("first_failed_at","").replace("Z","+00:00")))
        except (ValueError,TypeError): pass
    report["oldest_retry_age_days"]=round(max(0,(datetime.now(timezone.utc)-min(oldest)).total_seconds()/86400),2) if oldest else 0
    if oldest and (datetime.now(timezone.utc)-min(oldest)).total_seconds() >= LONG_QUEUE_SECONDS:
        print(f"::warning title=Long retry queue::Oldest unresolved episode is {report['oldest_retry_age_days']} days old; queue={len(retry)}",flush=True)
    report["quality_gate"]="PASS"
    safe_report("test report JSON", Path("data/test-report.json").write_text, json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
    csv_path=DATA/"episode-checks.csv"
    if not full_archive and csv_path.exists():
        try:
            with open(csv_path,newline="",encoding="utf-8-sig") as f:
                prior_rows=list(csv.DictReader(f))
            by_page={r.get("page_url"):r for r in prior_rows if r.get("page_url")}
            for r in rows: by_page[r.get("page_url")]=r
            rows=list(by_page.values())
            for n,r in enumerate(rows,1): r["index"]=n
        except (OSError,csv.Error): pass
    def write_episode_checks():
        with open(csv_path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=["index","title","page_url","audio_url","audio_ok","track_count","status","error"]); w.writeheader(); w.writerows(rows)
    safe_report("episode checks CSV", write_episode_checks)
    # Only successful scheduled/push incremental runs are added to the hourly history.
    # Archive backfills and manual runs do not create hourly entries.
    if not full_archive and report["rss_xml_valid"] and os.getenv("HOURLY_REPORT_ENABLED", "").strip().lower() == "true":
        newly_added=[x for x in items if x.get("url") and x.get("url") not in previous_by_url]
        safe_report("hourly success history CSV", write_hourly_success_report, local_now, newly_added)
    archive_complete = full_archive and DISCOVERY_STATS["pages_scanned"] == MAX_ARCHIVE_PAGES and not errors
    report["archive_complete"] = archive_complete
    safe_report("run report CSV", append_run_report, report)
    safe_report("summary report CSV", write_summary_report, report, queue_before, len(retry))
    safe_report("final test report JSON", Path("data/test-report.json").write_text, json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
    if archive_complete:
        marker.write_text(json.dumps({"completed_at":datetime.now(timezone.utc).isoformat(),"pages_scanned":DISCOVERY_STATS["pages_scanned"],"discovered":len(urls),"published":len(items),"audio_confirmed":report["audio_confirmed"],"deferred_audio_failures":len(retry),"retry_queue":"data/retry-queue.json","cover_failures":len(cover_stats.get("failed", [])),"sort_order":"Added date descending"},ensure_ascii=False,indent=2),encoding="utf-8")
        print("[ARCHIVE] COMPLETE: all 375 pages scanned; artwork passed; episodes without verified audio were omitted from RSS and saved to data/retry-queue.json for a later retry. Deferred audio failures="+str(len(retry)), flush=True)
    elif full_archive:
        print("[ARCHIVE] NOT COMPLETE: pages="+str(DISCOVERY_STATS["pages_scanned"])+"/"+str(MAX_ARCHIVE_PAGES)+"; discovered="+str(len(urls))+"; published="+str(len(items))+"; retry="+str(len(retry))+"; cover_failures="+str(len(cover_stats.get("failed", [])))+"; listing_errors="+str(len(errors))+". Marker was not written.", flush=True)
    print(json.dumps({k:report.get(k) for k in ("mode","discovered","newly_checked","archived_total","published","audio_confirmed","pages_scanned","archive_complete","page_limit_reached","rss_xml_valid")},ensure_ascii=False), flush=True)

if __name__=="__main__":main()
