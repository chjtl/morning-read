import sys, feedparser, warnings
warnings.filterwarnings("ignore")

for url in sys.argv[1:]:
    try:
        f = feedparser.parse(url, agent="Mozilla/5.0 (compatible; MorningRead/1.0)")
        status = getattr(f, "status", "?")
        n = len(f.entries)
        title = f.feed.get("title", "")[:45]
        newest = ""
        if n:
            e = f.entries[0]
            newest = (e.get("published", e.get("updated", "no date")) or "")[:25]
        mark = "OK " if n else "-- "
        print(f"{mark}{status:>4} n={n:<3} {title:<45} {newest:<26} {url}")
    except Exception as exc:
        print(f"ERR      {type(exc).__name__:<40} {url}")
