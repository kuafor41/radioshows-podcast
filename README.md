# RadioShows Podcast RSS

Automated RSS feed for radio shows and DJ podcasts published on [EDMliveset.com](https://www.edmliveset.com/radio/).

## Production feed

- **RSS:** [rss.xml](https://kuafor41.github.io/radioshows-podcast/rss.xml)
- **Ordering:** newest first, using each episode page's `Added` date
- **Scheduled check:** every hour at **:50, GMT+3 local time**
- **Hosting/deployment:** GitHub Actions + GitHub Pages

## How an update works

1. The scheduled workflow opens only the newest three archive listing pages. It does not walk the historical 375-page archive again.
2. Episode links already stored in `data/items.json` are ignored as new arrivals. Previously failed audio URLs in `data/retry-queue.json` are also retried, up to 30 queued URLs per run.
3. For each candidate, the scraper reads the episode page, extracts its metadata, description/tracklist, and artwork, then checks that a candidate enclosure resolves to audio.
4. Only episodes with confirmed audio are included in the feed. Failed audio checks remain in the retry queue and are reconsidered on later hourly runs.
5. The RSS is rebuilt in descending `Added` date order. Existing saved episodes are preserved, and genuinely new or newly recovered episodes appear at the top when their date is newest.
6. The workflow validates the XML, saves diagnostics and CSV reports, commits changed feed data, and deploys the site.

GitHub Actions schedules are best-effort: a run can start a little later than its target minute if GitHub is under load.

## Data files

| File | Purpose |
|---|---|
| `rss.xml` | Published RSS 2.0 feed |
| `data/items.json` | Stored episodes with confirmed audio; source of truth for incremental updates |
| `data/retry-queue.json` | Audio-resolution failures waiting for another attempt |
| `data/episode-checks.csv` | Per-episode check results, including audio status and errors |
| `data/hourly-success-report.csv` | Successful scheduled checks, newest first; records zero-addition runs too |
| `data/test-report.json` | Latest run summary, discovery counts, audio/artwork diagnostics, and validation state |
| `data/cover-map.json`, `data/covers/` | Cached episode/series artwork |
| `data/archive-backfill-complete.json` | Verified marker that prevents hourly jobs from restarting the historical backfill |

## Audio retry behavior

- New episodes are checked during each scheduled run.
- The retry queue is attempted in batches of up to 30 older failed URLs per run.
- A successful audio resolution publishes the episode and removes its URL from the retry queue.
- A failed resolution stays queued with the latest error and attempt time.
- The queue is capped at 1,000 unique URLs to prevent unbounded growth.
- Episodes whose audio is not confirmed are never published with an HTML page as an enclosure.

## CSV reports

`hourly-success-report.csv` uses the same structure as the UniversalMixDJ project:

`date,time,added_count,episode_titles`

Each successful scheduled incremental run adds a new row at the top. A run with no new episodes is still recorded with `added_count=0`. The per-episode CSV is retained separately for debugging.

## Validation and maintenance

Every run checks Python syntax and parses the generated XML before deployment. Review `data/test-report.json`, `data/episode-checks.csv`, and `data/retry-queue.json` when troubleshooting.

### Quality-of-life recommendations

- Keep the retry queue and run report committed so failures survive between GitHub Actions runs.
- Use the hourly CSV to monitor missed runs and sudden changes in new-episode counts.
- If the source site changes its page layout, inspect a few recent rows in `episode-checks.csv` before changing extraction rules.
- Avoid manual full-archive runs during normal operation; the production workflow is incremental after the initial archive.
