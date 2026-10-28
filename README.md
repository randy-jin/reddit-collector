## Reddit data collection (`collect.py`)

Collects Reddit posts and comments with no API key, using Python's standard library (plus `certifi` if it's installed). It reads Reddit's public **RSS/Atom feeds**. Reddit's `.json` endpoints return 403 when you aren't logged in, and old.reddit sends you to a login page. The feeds still work if the request uses a browser User-Agent, which the script sends for you.

**Usage**
```bash
# subreddit listing (hot|new|top|rising|controversial; -t applies to top/controversial)
python3 collect.py subreddit -r SaaS -s top -t week -n 25 --comments -o out/saas_top_week
# site-wide search (--search-sort relevance|hot|top|new|comments, -t hour..all)
python3 collect.py search -q "calgary rent" --search-sort new -n 100 -o out/calgary_rent
# search within one subreddit
python3 collect.py search -r Calgary -q "rent" -t month -n 50 -o out/calgary_sub_rent
# comments for a single post
python3 collect.py comments --permalink https://www.reddit.com/r/SaaS/comments/<id>/<slug>/ -o out/thread
# offline parse of a saved feed (tests)
python3 collect.py search -q x --from-file fixtures/search_sample.rss -o out/fixture
python3 collect.py comments --from-file fixtures/comments_sample.rss -o out/fixture
```
Options: `-n` limit (pages of 100 via `after=t3_<id>`), `--delay` seconds between requests (default 6), `--try-json` (try `.json` first, then RSS), `-o` output prefix.

**Output**: `<prefix>_posts.csv/.json` with columns id, subreddit, title, selftext, author, score, num_comments, created (ISO UTC), url, permalink. `<prefix>_comments.csv/.json` with columns post_id, author, body, score, created. Each run prints a summary: which source answered, row counts, and the last few HTTP statuses.

**Limitations**
- RSS has no `score` or `num_comments`. Those columns are left empty unless `--try-json` succeeds.
- A comment feed returns at most about 100 comments per post, with no nesting or parent IDs, and it can't expand "more comments".
- `url` holds the external link (from `[link]`). For self posts it's the same as the permalink.
- Search results can include subreddit (t5) entries. These are dropped.
- Rate limits are strict: about one request every few seconds. A 429 is retried after the server's Retry-After time, or with growing waits of 10, 20, 40… seconds (max 180s). `--comments` makes one request per post, so 25 posts takes about 3 minutes.
- Listings stop at about 1000 posts. Selftext comes from rendered HTML, so markdown formatting is lost.
- SSL: if `certifi` is installed its certificate bundle is used; otherwise the system default. If urllib still fails certificate checks (python.org builds on macOS), the script switches to the system `curl`.
