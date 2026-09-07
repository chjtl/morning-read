"""Try several ways to reach a blocked feed. Run this on CI, not locally -
the whole point is to test from the network that is being refused.
"""
import urllib.parse as up
import feedparser, requests

FEED = "https://rationaloptimistsociety.substack.com/feed"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
enc = up.quote(FEED, safe="")

STRATEGIES = {
    "direct":        FEED,
    "substack-api":  "https://rationaloptimistsociety.substack.com/api/v1/archive?sort=new&limit=12",
    "allorigins":    f"https://api.allorigins.win/raw?url={enc}",
    "jina":          f"https://r.jina.ai/{FEED}",
    "rss2json":      f"https://api.rss2json.com/v1/api.json?rss_url={enc}",
    "codetabs":      f"https://api.codetabs.com/v1/proxy?quest={enc}",
}

for name, url in STRATEGIES.items():
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        n = len(feedparser.parse(r.content).entries)
        body = r.text[:60].replace("\n", " ")
        print(f"{name:<14} HTTP {r.status_code:<4} bytes={len(r.content):<8} feed_entries={n:<4} {body!r}")
    except Exception as exc:
        print(f"{name:<14} ERROR {type(exc).__name__}: {str(exc)[:70]}")
