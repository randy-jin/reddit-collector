#!/usr/bin/env python3
"""Reddit collector (stdlib + optional certifi). Primary source: Reddit RSS/Atom feeds.

Reddit's JSON endpoints commonly 403 for unauthenticated clients; the Atom feeds
(.rss) still answer with a browser User-Agent, but rate-limit hard (~1 req / few s).
"""
import argparse, csv, html, json, os, re, ssl, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

BASE = "https://www.reddit.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
A = "{http://www.w3.org/2005/Atom}"
POST_F = ["id", "subreddit", "title", "selftext", "author", "score", "num_comments",
          "created", "url", "permalink"]
COM_F = ["post_id", "author", "body", "score", "created"]
LOG = []  # (status, url)

# ---------------------------------------------------------------- HTTP layer
def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

SSL_CTX = _ssl_ctx()
USE_CURL = False  # flipped on first SSL verification failure

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # a redirect here means login wall -> treat as blocked

def _urllib_get(url):
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CTX), _NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with op.open(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""

def _curl_get(url):
    with tempfile.TemporaryDirectory() as d:
        hf, bf = os.path.join(d, "h"), os.path.join(d, "b")
        p = subprocess.run(["curl", "-sS", "-A", UA, "-D", hf, "-o", bf, "-w", "%{http_code}",
                            "--max-time", "30", url], capture_output=True, text=True)
        if p.returncode != 0:
            raise OSError("curl failed: " + p.stderr.strip())
        hdrs = {}
        for ln in open(hf, errors="replace").read().splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1); hdrs[k.strip().title()] = v.strip()
        return int(p.stdout.strip() or 0), hdrs, open(bf, "rb").read()

def fetch(url, delay, retries=5):
    """GET with fixed spacing, Retry-After / exponential backoff on 429. Returns bytes or None."""
    global USE_CURL
    for i in range(retries):
        time.sleep(delay)
        try:
            if USE_CURL:
                code, hdrs, body = _curl_get(url)
            else:
                try:
                    code, hdrs, body = _urllib_get(url)
                except urllib.error.URLError as e:
                    if isinstance(getattr(e, "reason", None), ssl.SSLError) or "CERTIFICATE" in str(e):
                        print("SSL verification failed in urllib; switching to system curl", file=sys.stderr)
                        USE_CURL = True
                        code, hdrs, body = _curl_get(url)
                    else:
                        raise
        except Exception as e:
            LOG.append((repr(e)[:80], url)); time.sleep(min(2 ** (i + 2), 60)); continue
        LOG.append((code, url))
        if code == 200:
            return body
        if code in (429, 500, 502, 503, 504) and i < retries - 1:
            ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
            try: wait = float(ra)
            except (TypeError, ValueError): wait = 10 * 2 ** i
            wait = min(wait, 180)
            print(f"{code} on {url}; waiting {wait:.0f}s", file=sys.stderr); time.sleep(wait); continue
        return None  # 403 / 302 / 404 etc.: not retryable
    return None

# --------------------------------------------------------------- helpers
def iso(v):
    if v in (None, ""): return ""
    try:
        if isinstance(v, (int, float)) or re.fullmatch(r"[\d.]+", str(v)):
            return datetime.fromtimestamp(float(v), timezone.utc).isoformat()
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except Exception:
        return str(v)

def strip_prefix(x):
    return re.sub(r"^t\d_", "", x or "")

class _Html2Text(HTMLParser):
    """Collects text inside <div class="md"> (Reddit's markdown body) and [link] hrefs."""
    BLOCK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.md_depth = 0; self.seen_md = False; self.div_stack = []
        self.md_text, self.all_text = [], []; self.href = None; self.atext = []; self.link = None
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            is_md = "md" in (a.get("class") or "").split()
            self.div_stack.append(is_md)
            if is_md: self.md_depth += 1; self.seen_md = True
        if tag == "a": self.href, self.atext = a.get("href"), []
        if tag in self.BLOCK: self._out("\n")
        if tag == "li": self._out("- ")
    def handle_endtag(self, tag):
        if tag == "div" and self.div_stack and self.div_stack.pop(): self.md_depth -= 1
        if tag == "a":
            if "".join(self.atext).strip() == "[link]" and self.href: self.link = self.href
            self.href = None
        if tag in self.BLOCK: self._out("\n")
    def handle_data(self, d):
        if self.href is not None: self.atext.append(d)
        self._out(d)
    def _out(self, s):
        self.all_text.append(s)
        if self.md_depth > 0: self.md_text.append(s)

def html_to_text(h):
    """Return (plain_text, external_link). Only the post/comment body (div.md) is kept."""
    p = _Html2Text(); p.feed(h or ""); p.close()
    txt = "".join(p.md_text) if p.seen_md else ""
    txt = html.unescape(txt).replace("\xa0", " ")
    txt = re.sub(r"[ \t]+", " ", txt); txt = re.sub(r" *\n *", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt, p.link

# ---------------------------------------------------------------- Atom parsing
def _t(e, path):
    x = e.find(path); return (x.text or "").strip() if x is not None and x.text else ""

def parse_feed(xml_bytes):
    """Parse a Reddit Atom feed into raw entry dicts."""
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall(A + "entry"):
        rid = _t(e, A + "id")
        cat = e.find(A + "category")
        link = e.find(A + "link")
        body, ext = html_to_text(_t(e, A + "content"))
        author = _t(e, f"{A}author/{A}name")
        out.append({
            "kind": rid.split("_")[0] if "_" in rid else "", "id": strip_prefix(rid),
            "subreddit": cat.get("term", "").strip() if cat is not None else "",
            "title": html.unescape(_t(e, A + "title")),
            "author": author[3:] if author.startswith("/u/") else author,
            "created": iso(_t(e, A + "published") or _t(e, A + "updated")),
            "permalink": link.get("href", "") if link is not None else "",
            "body": body, "link": ext,
        })
    return out

def entries_to_posts(entries):
    posts = []
    for x in entries:
        if x["kind"] != "t3": continue  # search feeds also return subreddits (t5)
        sub = x["subreddit"] or (re.search(r"/r/([^/]+)/", x["permalink"]) or [None, ""])[1]
        posts.append({"id": x["id"], "subreddit": sub, "title": x["title"], "selftext": x["body"],
                      "author": x["author"], "score": "", "num_comments": "", "created": x["created"],
                      "url": x["link"] or x["permalink"], "permalink": x["permalink"]})
    return posts

def entries_to_comments(entries, post_id=None):
    if not post_id:
        t3 = [x for x in entries if x["kind"] == "t3"]
        post_id = t3[0]["id"] if t3 else ""
    out = []
    for x in entries:
        if x["kind"] != "t1": continue
        pid = post_id or (re.search(r"/comments/([^/]+)/", x["permalink"]) or [None, ""])[1]
        out.append({"post_id": pid, "author": x["author"], "body": x["body"], "score": "",
                    "created": x["created"]})
    return out

# ------------------------------------------------------------------ RSS source
def rss_listing(path, params, limit, delay):
    """Paginate an RSS listing via after=t3_<last id>. Returns list of posts or None if blocked."""
    posts, seen, after = [], set(), None
    while len(posts) < limit:
        prm = {k: v for k, v in params.items() if v not in (None, "")}
        prm["limit"] = 100
        if after: prm["after"] = after
        body = fetch(f"{BASE}{path}?{urllib.parse.urlencode(prm)}", delay)
        if body is None: return posts if posts else None
        try: page = entries_to_posts(parse_feed(body))
        except ET.ParseError as e:
            print(f"Feed parse error: {e}", file=sys.stderr); break
        new = [p for p in page if p["id"] not in seen]
        if not new: break  # pagination not honored / exhausted
        for p in new: seen.add(p["id"])
        posts += new
        after = "t3_" + page[-1]["id"]
        if len(page) < 25: break  # short page = end of listing
    return posts[:limit]

def rss_comments(post_id, permalink, delay):
    url = (permalink.rstrip("/") + "/" if permalink else f"{BASE}/comments/{post_id}/")
    if url.startswith("/"): url = BASE + url
    body = fetch(url + ".rss?limit=100", delay)
    if body is None: return None
    return entries_to_comments(parse_feed(body), post_id)

# --------------------------------------------------- optional JSON source (--try-json)
def _norm_json_post(p):
    pl = p.get("permalink", "")
    return {"id": p.get("id"), "subreddit": p.get("subreddit"), "title": p.get("title"),
            "selftext": p.get("selftext", ""), "author": p.get("author"), "score": p.get("score"),
            "num_comments": p.get("num_comments"), "created": iso(p.get("created_utc")),
            "url": p.get("url"), "permalink": BASE + pl if pl.startswith("/") else pl}

def json_listing(path, params, limit, delay):
    out, after = [], None
    while len(out) < limit:
        prm = {**{k: v for k, v in params.items() if v not in (None, "")}, "limit": 100, "raw_json": 1}
        if after: prm["after"] = after
        body = fetch(f"{BASE}{path}?{urllib.parse.urlencode(prm)}", delay, retries=2)
        if body is None: return out if out else None
        d = json.loads(body)["data"]
        out += [_norm_json_post(c["data"]) for c in d.get("children", []) if c.get("kind") == "t3"]
        after = d.get("after")
        if not after: break
    return out[:limit]

def json_comments(post_id, permalink, delay):
    path = urllib.parse.urlparse(permalink).path.rstrip("/") if permalink else f"/comments/{post_id}"
    body = fetch(f"{BASE}{path}.json?limit=500&raw_json=1", delay, retries=2)
    if body is None: return None
    out = []
    def walk(ch):
        for c in ch:
            if c.get("kind") != "t1": continue
            d = c["data"]
            out.append({"post_id": post_id, "author": d.get("author"), "body": d.get("body"),
                        "score": d.get("score"), "created": iso(d.get("created_utc"))})
            if isinstance(d.get("replies"), dict): walk(d["replies"]["data"]["children"])
    walk(json.loads(body)[1]["data"]["children"])
    return out

# ------------------------------------------------------------------- orchestration
def listing_spec(a):
    if a.mode == "subreddit":
        return f"/r/{a.subreddit}/{a.sort}", {"t": a.time if a.sort in ("top", "controversial") else None}
    prm = {"q": a.query, "sort": a.search_sort, "t": a.time}
    if a.subreddit:
        prm["restrict_sr"] = "on"; return f"/r/{a.subreddit}/search", prm
    return "/search", prm

def get_posts(a):
    path, prm = listing_spec(a)
    if a.try_json:
        res = json_listing(path + ".json", prm, a.limit, a.delay)
        if res is not None: return res, "json"
        print("JSON endpoint blocked; using RSS", file=sys.stderr)
    if a.mode == "subreddit": path += "/"  # /r/<sub>/<sort>/.rss
    return rss_listing(path + ".rss", prm, a.limit, a.delay) or [], "rss"

def get_comments(a, post_id, permalink):
    if a.try_json:
        res = json_comments(post_id, permalink, a.delay)
        if res is not None: return res
    return rss_comments(post_id, permalink, a.delay) or []

def write(rows, fields, base):
    os.makedirs(os.path.dirname(os.path.abspath(base)), exist_ok=True)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    with open(base + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)

def post_id_from(s):
    m = re.search(r"/comments/([a-z0-9]+)", s or "")
    return m.group(1) if m else strip_prefix(s)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["subreddit", "search", "comments"])
    ap.add_argument("-r", "--subreddit")
    ap.add_argument("-q", "--query")
    ap.add_argument("-s", "--sort", default="hot", choices=["hot", "new", "top", "rising", "controversial"])
    ap.add_argument("--search-sort", default="relevance", choices=["relevance", "hot", "top", "new", "comments"])
    ap.add_argument("-t", "--time", default="all", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("-n", "--limit", type=int, default=25)
    ap.add_argument("--comments", action="store_true", help="also fetch comments for each post (1 request/post)")
    ap.add_argument("--permalink", help="post URL, permalink or id (comments mode)")
    ap.add_argument("--delay", type=float, default=6.0, help="seconds between requests (default 6)")
    ap.add_argument("--try-json", action="store_true", help="try .json endpoints first, fall back to RSS")
    ap.add_argument("--from-file", help="parse a saved Atom feed instead of fetching (offline test)")
    ap.add_argument("-o", "--out", default="reddit_out", help="output path prefix")
    a = ap.parse_args()

    posts, comments, src = [], [], "rss"
    if a.from_file:
        src = "file:" + a.from_file
        entries = parse_feed(open(a.from_file, "rb").read())
        if a.mode == "comments":
            comments = entries_to_comments(entries, post_id_from(a.permalink) if a.permalink else None)
        else:
            posts = entries_to_posts(entries)[: a.limit]
    elif a.mode == "comments":
        if not a.permalink: ap.error("comments mode needs --permalink (URL, permalink or post id)")
        pl = a.permalink if "/" in a.permalink else None
        comments = get_comments(a, post_id_from(a.permalink), pl)
    else:
        if a.mode == "subreddit" and not a.subreddit: ap.error("--subreddit required")
        if a.mode == "search" and not a.query: ap.error("--query required")
        posts, src = get_posts(a)
        if a.comments:
            for i, p in enumerate(posts, 1):
                print(f"comments {i}/{len(posts)}: {p['id']}", file=sys.stderr)
                comments += get_comments(a, p["id"], p["permalink"])

    if a.mode != "comments": write(posts, POST_F, a.out + "_posts")
    if a.mode == "comments" or a.comments: write(comments, COM_F, a.out + "_comments")
    print(json.dumps({"source": src, "posts": len(posts), "comments": len(comments),
                      "last_requests": [f"{s} {u}" for s, u in LOG][-8:]}, indent=1))

if __name__ == "__main__":
    main()
