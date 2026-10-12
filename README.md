# RadioShows Podcast RSS

Latest radio shows.

## Initial test phase

The first version scans for up to 50 newest episode pages. It extracts Artist, Show, Serie, Added, Genre and tracklist fields, looks for a direct audio URL, and probes that URL before adding an episode to rss.xml.

- RSS feed: rss.xml
- Episode records: data/items.json
- Run diagnostics: data/test-report.json
- CSV episode checks: data/episode-checks.csv
- Retry queue: data/retry-queue.json

The description formats tracklist entries as an HTML ordered list so compatible podcast clients can display each track on its own line. The enclosure must point to audio, not the source HTML page. The scraper is intentionally limited to the newest 50 episodes until validation is complete.

## Automation

Run manually using Actions → Update Radio Shows Podcast RSS → Run workflow. The schedule targets one run every four hours (UTC), though GitHub may delay scheduled runs. To publish the generated feed, enable GitHub Pages in repository settings and select GitHub Actions as the deployment source.

Review data/test-report.json and data/episode-checks.csv before expanding the archive.
