#!/usr/bin/env python3
import csv, hashlib, html, json, os, re, time, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

BASE="https://www.edmliveset.com"
ARCHIVE=BASE+"/radio/"
DATA=Path("data"); DATA.mkdir(exist_ok=True)
OUT=Path("rss.xml"); LIMIT=50; TIMEOUT=25
CUTOFF=datetime(2025,1,1,tzinfo=timezone.utc)
MAX_ARCHIVE_PAGES=500
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (compatible; RadioShowsPodcastRSS/1.0)","Accept":"text/html,application/xhtml+xml,*/*;q=0.8"})

def txt(n): return re.sub(r"\s+"," ",n.get_text(" ",strip=True)).strip() if n else ""
def url(u,b=BASE):
    if not u:return ""
    u=urljoin(b,str(u).strip())
    return u.split("#",1)[0] if urlparse(u).scheme in ("http","https") and urlparse(u).netloc else ""
def get(u,**kw):
    r=S.get(u,timeout=TIMEOUT,**kw); r.raise_for_status(); return r
def links_from(soup, page):
    out=[]
    for a in soup.select("a[href]"):
        u=url(a.get("href"),page); t=txt(a); p=urlparse(u).path.rstrip("/")
        if urlparse(u).netloc.lower() not in ("edmliveset.com","www.edmliveset.com"): continue
        # Do not mistake archive pagination (/radio/page/2/) for an episode.
        if re.fullmatch(r"/radio/page/\d+",p,re.I): continue
        if re.fullmatch(r"/radio/[^/]+",p,re.I) and t.lower() not in ("download","free download","listen","read more"): out.append(u)
    return list(dict.fromkeys(out))
def discover(full_archive=False):
    found=[]; errors=[]; nxt=ARCHIVE; seen=set()
    page_limit=MAX_ARCHIVE_PAGES if full_archive else 3
    for pg in range(1,page_limit+1):
        if not nxt or nxt in seen or (not full_archive and len(found)>=LIMIT): break
        seen.add(nxt)
        try:
            try:
                r=get(nxt)
            except requests.HTTPError as page_error:
                # The visible Podcasts & Radio Shows pagination links lead to
                # /podcasts-radio-shows/page/N/, which returns 404. The working
                # paginated archive is /radio/page/N/, so retry that route.
                fallback=BASE+f"/radio/page/{pg}/"
                if pg == 1 or urlparse(nxt).path.rstrip("/") == urlparse(fallback).path.rstrip("/"):
                    raise
                print(f"Archive URL failed ({nxt}); trying {fallback}: {page_error}")
                r=get(fallback)
            soup=BeautifulSoup(r.text,"html.parser")
            for u in links_from(soup,r.url):
                if u not in found: found.append(u)
                if not full_archive and len(found)>=LIMIT: break
            nxt2=""
            for a in soup.select("a[href]"):
                label=txt(a).lower(); rel=" ".join(a.get("rel",[])).lower(); u=url(a.get("href"),r.url)
                # Follow the actual pagination control (e.g. "Next →"), not
                # just exact text "Next". This supports normal WordPress links
                # and themes that append arrows or accessibility labels.
                is_next = ("next" in rel.split() or bool(re.match(r"^next\b", label, re.I)) or label in ("older posts","→","»","›"))
                if u and is_next:
                    nxt2=u
                    break
            if not nxt2:
                pages=[]
                for a in soup.select("a[href]"):
                    u=url(a.get("href"),r.url); m=re.search(r"/page/(\d+)/?$",urlparse(u).path)
                    if m and u not in seen: pages.append((int(m.group(1)),u))
                pages=sorted((n,u) for n,u in pages if n>pg)
                if pages:nxt2=pages[0][1]
            if not nxt2 and len(found)<LIMIT:
                # Fallback when pagination is JavaScript-driven or has no usable next href.
                nxt2=BASE+f"/radio/page/{pg+1}/"
            nxt=nxt2; print("Archive",pg,"links",len(found)); time.sleep(.2)
        except Exception as e:
            errors.append({"page":pg,"url":nxt,"error":str(e)}); print("Archive error",e); break
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
    e=lambda s:html.escape(re.sub(r"\bEDMliveset\.com\b","",s or "",flags=re.I).strip(),quote=True)
    parts=[f"<p><b>{k.title()}:</b> {e(x[k.lower()])}</p>" for k in ("Artist","Show","Serie","Added","Genre")]
    # Show the text after FREE DOWNLOAD as it appears on the episode page.
    # Paragraphs (not an ordered list) preserve line breaks without auto-numbering.
    lines=x.get("body_lines") or (["Tracklist"]+x["tracklist"] if x["tracklist"] else ["Tracklist not found on source page."])
    parts.append("".join("<p>"+e(t)+"</p>" for t in lines))
    return "".join(parts)
def build(items):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    root=ET.Element("rss",{"version":"2.0"}); ch=ET.SubElement(root,"channel")
    for tag,val in [("title","EDMliveset.com — Podcasts & Radio Shows"),("link",ARCHIVE),("description","Radio shows published on or after January 1, 2025."),("language","en"),("lastBuildDate",format_datetime(datetime.now(timezone.utc))),("generator","RadioShows Podcast RSS Builder")]:ET.SubElement(ch,tag).text=val
    cover_url="https://raw.githubusercontent.com/kuafor41/radioshows-podcast/main/data/podcast-cover.jpg"
    image=ET.SubElement(ch,"image"); ET.SubElement(image,"url").text=cover_url; ET.SubElement(image,"title").text="EDMliveset.com — Podcasts & Radio Shows"; ET.SubElement(image,"link").text=ARCHIVE
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
def main():
    items_path=DATA/"items.json"
    try: previous=json.loads(items_path.read_text(encoding="utf-8"))
    except (OSError,ValueError): previous=[]
    previous_by_url={x.get("url"):x for x in previous if x.get("url")}
    marker=DATA/"archive-backfill-complete.json"
    full_archive=(not marker.exists()) or os.getenv("FEED_MODE")=="archive"
    cutoff_reached=False
    urls,errors=discover(full_archive=full_archive)
    if not full_archive: urls=[u for u in urls if u not in previous_by_url]
    items=[] if full_archive else list(previous_by_url.values())
    rows=[]; retry=[]
    for i,u in enumerate(urls,1):
        try:
            x=parse(u); added=dateval(x.get("added",""))
            if not added:
                err="Could not parse Added date; excluded until date can be verified."
                rows.append({"index":i,"title":x["title"],"page_url":x["url"],"audio_url":"","audio_ok":False,"track_count":len(x["tracklist"]),"status":"warning","error":err})
                retry.append({"url":u,"error":err,"attempted_at":datetime.now(timezone.utc).isoformat()})
                continue
            x["pubdate"]=added.isoformat()
            if added<CUTOFF:
                print(f"Reached archive cutoff at {x['added']}: {x['title']}")
                if full_archive:
                    cutoff_reached=True
                    break
                continue
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
            if not ok: retry.append({"url":u,"error":err,"attempted_at":datetime.now(timezone.utc).isoformat()})
            if ok: items.append(x)
            rows.append({"index":i,"title":x["title"],"page_url":x["url"],"audio_url":x["audio_url"],"audio_ok":ok,"track_count":len(x["tracklist"]),"status":x["status"],"error":x["error"]})
            print(f"[{i}/{len(urls)}] {x['status']} | added={x['added']} | audio={ok} | {x['title']}")
        except Exception as e:
            err=f"{type(e).__name__}: {e}"
            rows.append({"index":i,"title":"","page_url":u,"audio_url":"","audio_ok":False,"track_count":0,"status":"failed","error":err})
            retry.append({"url":u,"error":err,"attempted_at":datetime.now(timezone.utc).isoformat()})
            print("Episode failed:",u,err)
        time.sleep(.15)
    if full_archive and cutoff_reached:
        marker.write_text(json.dumps({"completed_at":datetime.now(timezone.utc).isoformat(),"cutoff":"2025-01-01"},indent=2),encoding="utf-8")
    merged={x.get("url"):x for x in items if x.get("url")}
    if not full_archive:
        for x in previous: merged.setdefault(x.get("url"),x)
    items=[x for x in merged.values() if x.get("audio_url") and (dateval(x.get("added","")) or datetime(1970,1,1,tzinfo=timezone.utc))>=CUTOFF]
    items.sort(key=lambda x:(dateval(x.get("added","")) or datetime(1970,1,1,tzinfo=timezone.utc)),reverse=True)
    cover_stats=cache_episode_covers(items)
    OUT.write_bytes(build(items))
    items_path.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")
    Path("data/retry-queue.json").write_text(json.dumps(retry,ensure_ascii=False,indent=2),encoding="utf-8")
    report={"mode":"archive-backfill" if full_archive else "incremental","source":ARCHIVE,"cutoff":"2025-01-01","run_at":datetime.now(timezone.utc).isoformat(),"discovered":len(urls),"newly_checked":len(rows),"archived_total":len(items),"published":len(items),"audio_confirmed":sum(bool(x.get("audio_url")) for x in items),"tracklists_found":sum(bool(x.get("tracklist")) for x in items),"cover_cache":cover_stats,"discovery_errors":errors,"episodes":rows,"rss_xml_valid":True}
    Path("data/test-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
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
    with open(csv_path,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=["index","title","page_url","audio_url","audio_ok","track_count","status","error"]); w.writeheader(); w.writerows(rows)
    print(json.dumps({k:report[k] for k in ("mode","discovered","newly_checked","archived_total","published","audio_confirmed","rss_xml_valid")},ensure_ascii=False))

if __name__=="__main__":main()
