---
name: reddit-collector
description: >-
  Use when the user wants to collect Reddit posts or comments (by subreddit,
  keyword search, or a single thread) into CSV/JSON for research, without a
  Reddit API key.
---
# Reddit data collection

A stdlib Python script that reads Reddit's public RSS/Atom feeds (the `.json` endpoints return 403 without login). The script lives beside this file as `collect.py`.

## Steps
1. Make sure `collect.py` is available: it sits in this skill's folder (`/home/box/agent-data/workflows/reddit-collector/collect.py`). Run it from the box; if the box is blocked, copy it to the user's computer and run it there.
2. Pick a mode from the user's ask:
```bash
python3 collect.py subreddit -r SaaS -s top -t week -n 25 --comments -o out/saas_top_week   # hot|new|top|rising|controversial
python3 collect.py search -q "calgary rent" --search-sort new -n 100 -o out/calgary_rent     # relevance|hot|top|new|comments
python3 collect.py search -r Calgary -q "rent" -t month -n 50 -o out/calgary_sub_rent       # search within one subreddit
python3 collect.py comments --permalink https://www.reddit.com/r/SaaS/comments/<id>/<slug>/ -o out/thread
python3 collect.py search -q x --from-file saved.rss -o out/test                            # offline parse
```
   Options: `-n` limit (pages of 100), `-t hour|day|week|month|year|all`, `--delay` seconds between requests (default 6), `--try-json` (try JSON first for score/num_comments), `-o` output prefix.
3. Long runs (`--comments` on many posts) take ~6-20 s per post because of rate limits; run them in the background.
4. Outputs: `<prefix>_posts.csv/.json` (id, subreddit, title, selftext, author, score, num_comments, created ISO UTC, url, permalink) and `<prefix>_comments.csv/.json` (post_id, author, body, score, created). Check the printed summary for row counts and HTTP statuses, then analyse or hand the files to the user.

## Limits
- RSS has no score or num_comments (columns stay empty).
- About 100 flat comments per post, no nesting.
- Listings stop around 1000 posts.
- Strict rate limits; 429s are retried automatically with backoff.
- On macOS python.org builds, SSL errors fall back to system `curl` automatically.
