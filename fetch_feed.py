#!/usr/bin/env python3
import csv, html, json, os, re, time, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

BASE="https://www.edmliveset.com"
ARCHIVE=BASE+"/podcasts-radio-shows/"
DATA=Path("data"); DATA.mkdir(exist_ok=True)
OUT=Path("rss.xml"); LIMIT=50; TIMEOUT=25
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
        if re.search(r"/radio/[^/]+",p,re.I) and t.lower() not in ("download","free download","listen","read more"): out.append(u)
    return list(dict.fromkeys(out))
def discover():
    found=[]; errors=[]; nxt=ARCHIVE; seen=set()
    for pg in range(1,16):
        if not nxt or nxt in seen or len(found)>=LIMIT: break
        seen.add(nxt)
        try:
            r=get(nxt); soup=BeautifulSoup(r.text,"html.parser")
            for u in links_from(soup,r.url):
                if u not in found: found.append(u)
                if len(found)>=LIMIT: break
            nxt2=""
            for a in soup.select("a[href]"):
                label=txt(a).lower(); rel=" ".join(a.get("rel",[])).lower(); u=url(a.get("href"),r.url)
                if u and ("next" in rel.split() or label in ("next","older posts","→","»","›")): nxt2=u; break
            if not nxt2:
                pages=[]
                for a in soup.select("a[href]"):
                    u=url(a.get("href"),r.url); m=re.search(r"/page/(\d+)/?$",urlparse(u).path)
                    if m and u not in seen: pages.append((int(m.group(1)),u))
                pages=sorted((n,u) for n,u in pages if n>pg)
                if pages:nxt2=pages[0][1]
            nxt=nxt2; print("Archive",pg,"links",len(found)); time.sleep(.2)
        except Exception as e:
            errors.append({"page":pg,"url":nxt,"error":str(e)}); print("Archive error",e); break
    return found[:LIMIT],errors
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
    return datetime.now(timezone.utc)
def parse(u):
    r=get(u); soup=BeautifulSoup(r.text,"html.parser")
    can=soup.find("link",rel="canonical"); canonical=url(can.get("href"),r.url) if can else r.url
    title=txt(soup.find("h1")) or txt(soup.title); d=fields(soup)
    d["show"]=d["show"] or title; d["artist"]=d["artist"] or "Unknown"; d["serie"]=d["serie"] or d["show"]
    heading=next((h for h in soup.find_all(["h2","h3","h4","strong"]) if "tracklist" in txt(h).lower()),None)
    tracks=[]
    if heading:
        for n in heading.find_all_next(["li","p"]):
            if n.name in ("h2","h3") and n is not heading:break
            t=txt(n)
            if t and "tracklist" not in t.lower() and t not in tracks:tracks.append(t)
    tracks=[t for t in tracks if not re.search(r"podcast.*embedded on this page|all files, descriptions",t,re.I)]
    candidates=[]
    for a in soup.select("a[href]"):
        label=txt(a).lower(); u2=url(a.get("href"),r.url)
        if u2 and ("download" in label or ".mp3" in u2.lower()):candidates.append(u2)
    for n in soup.select("audio[src],audio source[src],iframe[src],meta[property='og:audio'],meta[property='og:audio:url']"):
        u2=url(n.get("src") or n.get("content"),r.url)
        if u2:candidates.append(u2)
    # Audio URLs are collected from explicit links, player sources and audio metadata above.
    candidates=list(dict.fromkeys(candidates))
    audio=next((x for x in candidates if re.search(r"\.mp3(?:\?|$)",x,re.I)),"")
    if not audio:audio=next((x for x in candidates if "hearthis.at" in urlparse(x).netloc and "/listen" in urlparse(x).path),"")
    return {"title":title,"url":canonical,"guid":canonical,**d,"audio_url":audio,"tracklist":tracks,"pubdate":dateval(d["added"]).isoformat()}
def probe(u):
    if not u:return False,u,0,"No direct audio URL found"
    try:
        r=S.get(u,headers={"Range":"bytes=0-1023"},timeout=TIMEOUT,stream=True,allow_redirects=True)
        typ=r.headers.get("Content-Type","").split(";")[0].lower(); size=int(r.headers.get("Content-Length","0") or 0); final=r.url; status=r.status_code
        next(r.iter_content(1024),b""); r.close()
        ok=status in (200,206) and (typ.startswith("audio/") or "octet-stream" in typ or final.lower().split("?")[0].endswith((".mp3",".m4a",".aac",".ogg",".opus",".wav")))
        return ok,final,size,"" if ok else f"Not confirmed audio: HTTP {status}, type={typ}"
    except Exception as e:return False,u,0,f"{type(e).__name__}: {e}"
def description(x):
    e=lambda s:html.escape(s or "",quote=True)
    parts=[f"<p><b>{k.title()}:</b> {e(x[k.lower()])}</p>" for k in ("Artist","Show","Serie","Added","Genre")]
    parts.append("<p><b>Tracklist</b></p>")
    parts.append("<ol>"+ "".join("<li>"+e(t)+"</li>" for t in x["tracklist"])+"</ol>" if x["tracklist"] else "<p>Tracklist not found on source page.</p>")
    parts.append('<p><a href="'+e(x["url"])+'">Source page</a></p>')
    return "".join(parts)
def build(items):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    root=ET.Element("rss",{"version":"2.0"}); ch=ET.SubElement(root,"channel")
    for tag,val in [("title","EDMliveset.com — Podcasts & Radio Shows"),("link",ARCHIVE),("description","Newest 50 radio shows from EDMliveset.com; test feed."),("language","en"),("lastBuildDate",format_datetime(datetime.now(timezone.utc))),("generator","RadioShows Podcast RSS Builder")]:ET.SubElement(ch,tag).text=val
    cover_url="https://kuafor41.github.io/radioshows-podcast/data/podcast-cover.jpg"
    image=ET.SubElement(ch,"image"); ET.SubElement(image,"url").text=cover_url; ET.SubElement(image,"title").text="EDMliveset.com — Podcasts & Radio Shows"; ET.SubElement(image,"link").text=ARCHIVE
    ET.SubElement(ch,"itunes:image",{"href":cover_url})
    for x in sorted(items,key=lambda z:z["pubdate"],reverse=True):
        it=ET.SubElement(ch,"item"); ET.SubElement(it,"title").text=x["title"]; ET.SubElement(it,"link").text=x["url"]
        ET.SubElement(it,"guid",{"isPermaLink":"false"}).text=x["guid"]
        ET.SubElement(it,"pubDate").text=format_datetime(datetime.fromisoformat(x["pubdate"]))
        ET.SubElement(it,"description").text=description(x)
        ET.SubElement(it,"enclosure",{"url":x["audio_url"],"length":str(x["audio_length"]),"type":x["audio_type"]})
        if x["genre"]:ET.SubElement(it,"category").text=x["genre"]
    ET.indent(root,space="  "); data=ET.tostring(root,encoding="utf-8",xml_declaration=True); ET.fromstring(data); return data
def main():
    urls,errors=discover(); items=[]; rows=[]; retry=[]
    for i,u in enumerate(urls,1):
        try:
            x=parse(u); ok,final,size,err=probe(x["audio_url"])
            x["audio_url"]=final if ok else x["audio_url"]; x["audio_length"]=size
            x["audio_type"]="audio/mpeg" if x["audio_url"].lower().split("?")[0].endswith(".mp3") else "audio/mpeg"
            x["status"]="success" if ok and x["tracklist"] else "warning"; x["error"]=err or ("" if x["tracklist"] else "Tracklist not extracted")
            if not ok:retry.append({"url":u,"error":err,"attempted_at":datetime.now(timezone.utc).isoformat()})
            items.append(x); rows.append({"index":i,"title":x["title"],"page_url":x["url"],"audio_url":x["audio_url"],"audio_ok":ok,"track_count":len(x["tracklist"]),"status":x["status"],"error":x["error"]})
            print(f"[{i}/{len(urls)}] {x['status']} | tracks={len(x['tracklist'])} | audio={ok} | {x['title']}")
        except Exception as e:
            err=f"{type(e).__name__}: {e}"; rows.append({"index":i,"title":"","page_url":u,"audio_url":"","audio_ok":False,"track_count":0,"status":"failed","error":err}); retry.append({"url":u,"error":err})
            print("Episode failed:",u,err)
        time.sleep(.15)
    publish=[x for x in items if x["audio_url"]]
    if not urls:raise SystemExit("No episodes discovered; not publishing an empty feed.")
    OUT.write_bytes(build(publish))
    Path("data/items.json").write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")
    Path("data/retry-queue.json").write_text(json.dumps(retry,ensure_ascii=False,indent=2),encoding="utf-8")
    report={"mode":os.getenv("FEED_MODE","test"),"source":ARCHIVE,"run_at":datetime.now(timezone.utc).isoformat(),"discovered":len(urls),"parsed":len(items),"published":len(publish),"audio_confirmed":sum(r["audio_ok"] for r in rows),"tracklists_found":sum(bool(x["tracklist"]) for x in items),"discovery_errors":errors,"episodes":rows,"rss_xml_valid":True}
    Path("data/test-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with open("data/episode-checks.csv","w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=["index","title","page_url","audio_url","audio_ok","track_count","status","error"]); w.writeheader(); w.writerows(rows)
    print(json.dumps({k:report[k] for k in ("discovered","parsed","published","audio_confirmed","tracklists_found","rss_xml_valid")},ensure_ascii=False))
if __name__=="__main__":main()
