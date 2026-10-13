#!/usr/bin/env python3
"""One-off, evidence-preserving recovery audit for every URL in data/retry-queue.csv."""
import csv, json, re, time
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import fetch_feed as ff

QUEUE = Path("data/retry-queue.csv")
REPORT = Path("data/recovery-audit.csv")
ITEMS = Path("data/items.json")
MEDIA_RE = re.compile(r"\.(?:mp3|m4a|aac|ogg|opus|wav|flac)(?:$|[?#])", re.I)
YT_RE = re.compile(r"(?:youtube\.com|youtu\.be)", re.I)

def candidates_from_response(resp):
    soup = BeautifulSoup(resp.text, "html.parser")
    media, links, players, youtube = [], [], [], []
    for node in soup.select("audio[src],audio source[src],source[src],meta[property='og:audio'],meta[property='og:audio:url']"):
        raw = node.get("src") or node.get("content")
        u = ff.url(raw, resp.url)
        if u: media.append(u)
    for node in soup.select("a[href],button[data-url], [data-src], [data-audio], [data-file]"):
        raw = node.get("href") or node.get("data-url") or node.get("data-src") or node.get("data-audio") or node.get("data-file")
        u = ff.url(raw, resp.url)
        label = ff.txt(node).lower()
        if u and (MEDIA_RE.search(u) or any(k in label for k in ("download", "free download"))):
            links.append(u)
    for node in soup.select("iframe[src],embed[src],video[src]"):
        u = ff.url(node.get("src"), resp.url)
        if u:
            players.append(u)
            if YT_RE.search(u): youtube.append(u)
    # Catch URLs stored in common player JS/JSON configs, including escaped slashes and query-string media URLs.
    raw_text = resp.text.replace("\\/", "/").replace("&amp;", "&")
    for raw in re.findall(r"""https?://[^"'\s<>\\]+""", raw_text, re.I):
        raw = raw.rstrip("),;]}\\")
        u = ff.url(raw, resp.url)
        if not u: continue
        if MEDIA_RE.search(u) or re.search(r"(?:file|audio|mp3|stream|download)=", u, re.I):
            media.append(u)
        if YT_RE.search(u): youtube.append(u)
    return list(dict.fromkeys(media)), list(dict.fromkeys(links)), list(dict.fromkeys(players)), list(dict.fromkeys(youtube))

def probe_candidates(urls):
    blocked, not_found, last_error = False, False, ""
    for candidate in dict.fromkeys(urls):
        good, final_url, size, err = ff.probe(candidate)
        if good:
            return final_url, size, blocked, not_found, ""
        last_error = err or last_error
        if re.search(r"HTTP (?:403|429)|\b(?:Forbidden|Too Many Requests)\b", err, re.I): blocked = True
        if re.search(r"HTTP 404\b|Not Found", err, re.I): not_found = True
    return "", 0, blocked, not_found, last_error

def inspect_one(page_url):
    download_candidates, player_candidates, player_urls, youtube_urls = [], [], [], []
    page_error = ""
    try:
        parsed = ff.parse(page_url)
        download_candidates.extend(parsed.get("audio_candidates", []))
        player_urls.extend(parsed.get("player_urls", []))
        # Independently inspect the article to classify the FREE DOWNLOAD route accurately.
        response = ff.get(page_url)
        media, links, players, yt = candidates_from_response(response)
        download_candidates.extend(media)
        download_candidates.extend(links)
        player_urls.extend(players)
        youtube_urls.extend(yt)
    except Exception as exc:
        page_error = f"{type(exc).__name__}: {exc}"

    # First choice: FREE DOWNLOAD button / download target. Follow intermediate HTML pages up to two hops.
    download_blocked = download_404 = False
    seen_pages = set()
    current = list(dict.fromkeys(download_candidates))
    for _ in range(3):
        direct, size, blocked, not_found, err = probe_candidates([u for u in current if MEDIA_RE.search(u)])
        download_blocked |= blocked
        download_404 |= not_found
        if direct:
            return {"status":"DOWNLOAD_RECOVERED","audio_url":direct,"audio_length":size,
                    "download_candidates":len(download_candidates),"player_candidates":0,
                    "page_error":page_error,"evidence":"FREE DOWNLOAD/direct media candidate verified"}
        next_urls = []
        for candidate in current:
            if candidate in seen_pages or MEDIA_RE.search(candidate): continue
            seen_pages.add(candidate)
            try:
                response = ff.get(candidate)
                media, links, players, yt = candidates_from_response(response)
                next_urls.extend(media); next_urls.extend(links)
                player_urls.extend(players); youtube_urls.extend(yt)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                if re.search(r"403|429|Forbidden|Too Many Requests", msg, re.I): download_blocked = True
                if re.search(r"404|Not Found", msg, re.I): download_404 = True
        current = list(dict.fromkeys(next_urls))
        if not current: break

    # Second choice: player iframe/embed, inspect player HTML/JS configs and test extracted media.
    player_blocked = player_404 = False
    media_candidates = []
    queue = list(dict.fromkeys(player_urls))
    visited = set()
    for _ in range(3):
        new_queue = []
        for player_url in queue:
            if player_url in visited: continue
            visited.add(player_url)
            if YT_RE.search(player_url):
                youtube_urls.append(player_url)
                continue
            try:
                response = ff.get(player_url)
                media, links, players, yt = candidates_from_response(response)
                media_candidates.extend(media)
                # Some player pages point to a separate config/player page, but do not crawl arbitrary external links.
                new_queue.extend(players)
                for candidate in links:
                    if MEDIA_RE.search(candidate): media_candidates.append(candidate)
                youtube_urls.extend(yt)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                if re.search(r"403|429|Forbidden|Too Many Requests", msg, re.I): player_blocked = True
                if re.search(r"404|Not Found", msg, re.I): player_404 = True
        current_media, size, blocked, not_found, err = probe_candidates(media_candidates)
        player_blocked |= blocked
        player_404 |= not_found
        if current_media:
            return {"status":"PLAYER_RECOVERED","audio_url":current_media,"audio_length":size,
                    "download_candidates":len(download_candidates),"player_candidates":len(media_candidates),
                    "page_error":page_error,"evidence":"player iframe/embed/HTML/JS media source verified"}
        queue = list(dict.fromkeys(new_queue))
        if not queue: break

    if (download_blocked or player_blocked):
        status = "ACCESS_BLOCKED"
    elif youtube_urls and not media_candidates:
        status = "YOUTUBE_ONLY"
    elif download_404 and player_404:
        status = "CONFIRMED_404"
    else:
        status = "UNRESOLVED"
    return {"status":status,"audio_url":"","audio_length":0,
            "download_candidates":len(download_candidates),"player_candidates":len(media_candidates),
            "page_error":page_error,"evidence":(page_error + " | " if page_error else "") +
            f"download_404={download_404}; player_404={player_404}; download_blocked={download_blocked}; player_blocked={player_blocked}; youtube={len(youtube_urls)}"}

def main():
    if not QUEUE.exists():
        raise SystemExit("data/retry-queue.csv not found")
    with QUEUE.open(encoding="utf-8-sig", newline="") as f:
        queue_rows = list(csv.DictReader(f))
    print(f"[RECOVERY] Auditing {len(queue_rows)} queue entries, download first then player.", flush=True)
    results = []
    for i, row in enumerate(queue_rows, 1):
        page_url = row.get("url", "").strip()
        if not page_url: continue
        try:
            result = inspect_one(page_url)
        except Exception as exc:
            result = {"status":"UNRESOLVED","audio_url":"","audio_length":0,"download_candidates":0,
                      "player_candidates":0,"page_error":f"{type(exc).__name__}: {exc}","evidence":"audit exception"}
        results.append({"url":page_url,"status":result["status"],"audio_url":result["audio_url"],
                        "audio_length":result["audio_length"],"download_candidates":result["download_candidates"],
                        "player_candidates":result["player_candidates"],"page_error":result["page_error"],
                        "evidence":result["evidence"],"audited_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
        print(f"[RECOVERY {i}/{len(queue_rows)}] {result['status']} | {page_url}", flush=True)
        time.sleep(0.35)

    fields=["url","status","audio_url","audio_length","download_candidates","player_candidates","page_error","evidence","audited_at"]
    with REPORT.open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader(); writer.writerows(results)

    recovered={r["url"]:r for r in results if r["status"] in ("DOWNLOAD_RECOVERED","PLAYER_RECOVERED") and r["audio_url"]}
    try: items=json.loads(ITEMS.read_text(encoding="utf-8"))
    except (OSError,ValueError): items=[]
    by_url={x.get("url"):x for x in items if x.get("url")}
    for page_url, result in recovered.items():
        try:
            item=by_url.get(page_url) or ff.parse(page_url)
            item["audio_url"]=result["audio_url"]
            item["audio_length"]=int(result["audio_length"] or 0)
            item["audio_type"]="audio/mpeg"
            item["status"]="success" if item.get("tracklist") else "warning"
            item["error"]="" if item.get("tracklist") else "Tracklist not extracted"
            by_url[page_url]=item
        except Exception as exc:
            print(f"[RECOVERY] URL verified but RSS item could not be built: {page_url}: {exc}",flush=True)

    if recovered:
        items=list(by_url.values())
        items.sort(key=lambda x:(ff.dateval(x.get("added","")) or ff.datetime(1970,1,1,tzinfo=ff.timezone.utc)),reverse=True)
        cover_stats=ff.cache_episode_covers(items)
        rss_bytes=ff.build(items)
        ff.validate_rss(rss_bytes)
        ff.OUT.write_bytes(rss_bytes)
        ITEMS.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")
        print(f"[RECOVERY] RSS restored {len(recovered)} verified audio source(s); cover cache: {cover_stats}",flush=True)

    remaining=[r for r in queue_rows if r.get("url","").strip() not in recovered]
    with QUEUE.open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["url","error_type","failure_count","first_failed_at","attempted_at","next_retry_at","error"])
        writer.writeheader()
        for row in remaining:
            result=next((x for x in results if x["url"]==row.get("url")),None)
            if result:
                row["error_type"]="temporary_or_other"
                row["attempted_at"]=result["audited_at"]
                row["error"]=f"Recovery audit: {result['status']} — {result['evidence']}"
                # Do not label 403/429 as permanent; keep unresolved/blocked entries retryable.
                if result["status"] in ("ACCESS_BLOCKED","UNRESOLVED","YOUTUBE_ONLY","CONFIRMED_404"):
                    row["next_retry_at"]=""
            writer.writerow({k:row.get(k,"") for k in writer.fieldnames})
    print("[RECOVERY] Summary: "+json.dumps({s:sum(1 for r in results if r["status"]==s) for s in
          ("DOWNLOAD_RECOVERED","PLAYER_RECOVERED","YOUTUBE_ONLY","CONFIRMED_404","ACCESS_BLOCKED","UNRESOLVED")},ensure_ascii=False),flush=True)
if __name__=="__main__": main()
