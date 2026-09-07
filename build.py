"""Build a single-page morning read from the feeds listed in config.yaml.

Run:  python build.py
Out:  docs/index.html

Nothing in here needs editing to add or remove a source - that all lives in
config.yaml.
"""

from __future__ import annotations

import calendar
import html
import re
import sys
import urllib.parse as up
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.yaml"
OUT = ROOT / "docs" / "index.html"

# Ask the way a browser would. Doesn't defeat an IP block, but some feeds do
# reject the default library agent.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
        "text/xml;q=0.8, text/html;q=0.7, */*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Substack sits behind Cloudflare and returns 403 to datacenter IPs - which is
# exactly where the scheduled build runs, even though the same request works
# fine from a home connection. rss2json reaches it and hands back JSON. This
# was verified on a GitHub runner against every other proxy option, all of
# which got challenged too.
RSS2JSON = "https://api.rss2json.com/v1/api.json?rss_url={}"
BLOCKED = {401, 403, 406, 429, 503}

TAGS = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Periods that end these do not end a sentence.
ABBREV = {
    "vs", "e.g", "i.e", "etc", "approx", "est", "no", "inc", "corp", "co",
    "ltd", "mr", "mrs", "ms", "dr", "jr", "sr", "st", "ave", "blvd", "sq",
    "ft", "u.s", "u.k", "d.c", "a.m", "p.m",
}


@dataclass
class Feed:
    entries: list = field(default_factory=list)
    link: str = ""
    status: int | None = None
    via: str = "direct"


@dataclass
class Item:
    source: str
    section: str
    title: str
    link: str
    summary: str
    published: datetime | None

    @property
    def age(self) -> str:
        if not self.published:
            return ""
        mins = (datetime.now(timezone.utc) - self.published).total_seconds() / 60
        if mins < 60:
            return f"{int(mins)}m ago"
        if mins < 60 * 24:
            return f"{int(mins // 60)}h ago"
        return f"{int(mins // (60 * 24))}d ago"


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch_direct(url: str) -> Feed:
    resp = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    return Feed(list(parsed.entries), parsed.feed.get("link", ""), resp.status_code)


def fetch_via_rss2json(url: str) -> Feed:
    resp = requests.get(
        RSS2JSON.format(up.quote(url, safe="")), headers=HEADERS, timeout=30
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "ok":
        raise RuntimeError("rss2json: " + str(payload.get("message", "not ok"))[:80])

    entries = [
        {
            "title": row.get("title", ""),
            "link": row.get("link", ""),
            "summary": row.get("description", ""),
            "content": [{"value": row.get("content", "")}],
            "published": row.get("pubDate", ""),
        }
        for row in payload.get("items", [])
    ]
    return Feed(
        entries,
        payload.get("feed", {}).get("link", ""),
        resp.status_code,
        via="rss2json",
    )


def fetch_feed(url: str, via: str = "auto") -> Feed:
    """Fetch a feed, routing around an IP block if we hit one."""
    if via == "rss2json":
        return fetch_via_rss2json(url)
    try:
        return fetch_direct(url)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if via == "auto" and code in BLOCKED:
            return fetch_via_rss2json(url)
        raise


# --------------------------------------------------------------------------
# cleaning
# --------------------------------------------------------------------------

def _complete(text: str) -> bool:
    """True if text looks like a whole thought rather than a bad split."""
    if text.count("(") != text.count(")"):
        return False
    last = text.rstrip(".").rsplit(" ", 1)[-1].lower()
    if last in ABBREV:
        return False
    return not (len(last) == 1 and last.isalpha())  # a lone initial


def one_line(raw: str, limit: int = 200) -> str:
    """Reduce a feed's description blob to a single readable sentence."""
    text = WHITESPACE.sub(" ", html.unescape(TAGS.sub(" ", raw or ""))).strip()
    if not text:
        return ""

    parts = SENTENCE_END.split(text)
    candidate = ""
    for part in parts:
        candidate = (candidate + " " + part).strip()
        if len(candidate) >= 60 and _complete(candidate):
            break
    else:
        candidate = candidate or text

    if len(candidate) > limit:
        candidate = candidate[:limit].rsplit(" ", 1)[0].rstrip(",;:-(") + "…"
    return candidate


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def pick_summary(entry, title: str) -> str:
    """Best available one-liner, skipping feeds that just echo the headline."""
    candidates = [entry.get("summary", "") or entry.get("description", "")]
    for block in entry.get("content", []) or []:
        candidates.append(block.get("value", ""))

    nt = _norm(title)
    for raw in candidates:
        line = one_line(raw)
        if not line:
            continue
        nl = _norm(line)
        if nl == nt:
            continue  # pure echo of the headline
        if nl.startswith(nt):
            rest = line[len(title):].lstrip(" -–—:.").strip()
            if len(rest) < 40:
                continue
            line = rest
        return line
    return ""


def pick_link(entry, feed_link: str, fallback: str = "") -> str:
    """A usable URL for an item.

    Podcast feeds routinely omit <link> on items and carry only an audio
    enclosure, so fall back rather than dropping the item on the floor.
    """
    if entry.get("link"):
        return entry["link"]
    for ref in entry.get("links", []) or []:
        if ref.get("rel") == "alternate" and ref.get("href"):
            return ref["href"]
    if fallback:
        return fallback
    if feed_link:
        return feed_link
    for ref in entry.get("links", []) or []:
        if ref.get("href"):
            return ref["href"]
    return ""


def parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        stamp = entry.get(key)
        if stamp:
            return datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc)
    # rss2json hands back a plain string instead of a parsed struct.
    raw = entry.get("published", "")
    if raw:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                when = datetime.strptime(raw, fmt)
                return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def collect(cfg: dict) -> tuple[list[Item], list[str]]:
    settings = cfg.get("settings", {})
    now = datetime.now(timezone.utc)
    default_age = settings.get("max_age_hours", 36)
    default_cap = settings.get("max_per_source", 5)

    items: list[Item] = []
    problems: list[str] = []

    for src in cfg.get("sources", []):
        name = src.get("name", "Unnamed")
        try:
            feed = fetch_feed(src["url"], src.get("via", "auto"))
        except Exception as exc:  # network, DNS, HTTP, malformed - keep going
            problems.append(f"{name}: {type(exc).__name__} - {str(exc)[:120]}")
            continue
        if not feed.entries:
            problems.append(f"{name}: parsed 0 entries (HTTP {feed.status})")
            continue

        kept = 0
        cap = src.get("max_items", default_cap)
        # A weekly source would always look empty under a daily window, so each
        # source may widen its own lookback. max_age_hours: 0 turns the age
        # filter off entirely - use it for anything that publishes a few times
        # a year, where you always want the latest one on the page.
        window = src.get("max_age_hours", default_age)
        cutoff = now - timedelta(hours=window) if window else None

        for entry in feed.entries:
            if kept >= cap:
                break
            when = parse_date(entry)
            if cutoff and when and when < cutoff:
                continue
            title = WHITESPACE.sub(" ", html.unescape(entry.get("title", ""))).strip()
            link = pick_link(entry, feed.link, src.get("link_fallback", ""))
            if not title or not link:
                continue
            items.append(
                Item(
                    source=name,
                    section=src.get("section", "General"),
                    title=title,
                    link=link,
                    summary=pick_summary(entry, title),
                    published=when,
                )
            )
            kept += 1

        if kept == 0:
            problems.append(f"{name}: nothing inside the time window")

    # Sources with no feed anywhere. These are links, not content - LinkedIn
    # posts by individuals can't be pulled by any legitimate route, so put
    # them one tap from the page instead of nowhere on it.
    for mark in cfg.get("bookmarks", []) or []:
        items.append(
            Item(
                source=mark.get("name", "Link"),
                section=mark.get("section", "General"),
                title=mark.get("title", mark.get("name", "Link")),
                link=mark.get("url", ""),
                summary=mark.get("note", ""),
                published=None,
            )
        )

    return items, problems


def render(items: list[Item], problems: list[str], cfg: dict) -> str:
    settings = cfg.get("settings", {})
    tz = ZoneInfo(settings.get("timezone", "America/New_York"))
    now = datetime.now(tz)
    title = settings.get("title", "Morning Read")

    order = list(settings.get("sections", []))
    for item in items:
        if item.section not in order:
            order.append(item.section)

    blocks = []
    for section in order:
        rows = [i for i in items if i.section == section]
        if not rows:
            continue
        rows.sort(
            key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        entries = []
        for i in rows:
            summary = ""
            if i.summary:
                summary = '<p class="sum">' + html.escape(i.summary) + "</p>"
            entries.append(
                '<li class="item">'
                '<div class="meta"><span class="src">'
                + html.escape(i.source)
                + '</span><span class="age">'
                + i.age
                + '</span></div><a class="hed" href="'
                + html.escape(i.link)
                + '">'
                + html.escape(i.title)
                + "</a>"
                + summary
                + "</li>"
            )
        blocks.append(
            "<section><h2>"
            + html.escape(section)
            + "</h2><ul>"
            + "".join(entries)
            + "</ul></section>"
        )

    note = ""
    if problems:
        lines = "".join("<li>" + html.escape(p) + "</li>" for p in problems)
        note = (
            '<details class="problems"><summary>'
            + str(len(problems))
            + " source(s) quiet or failing</summary><ul>"
            + lines
            + "</ul></details>"
        )

    body = "".join(blocks) or '<p class="empty">Nothing new inside the time window.</p>'

    stamp = (
        "<span>"
        + now.strftime("%A") + ", " + now.strftime("%B") + " " + str(now.day)
        + '</span><span class="sep">/</span><span>built '
        + now.strftime("%I:%M %p").lstrip("0").lower()
        + "</span>"
    )
    n_sources = len({i.source for i in items})
    count = (
        str(len(items)) + " item" + ("s" if len(items) != 1 else "")
        + " from " + str(n_sources) + " source" + ("s" if n_sources != 1 else "")
    )

    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    for token, value in (
        ("__TITLE__", html.escape(title)),
        ("__STAMP__", stamp),
        ("__BODY__", body),
        ("__NOTE__", note),
        ("__COUNT__", count),
    ):
        tpl = tpl.replace(token, value)
    return tpl


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    items, problems = collect(cfg)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(items, problems, cfg), encoding="utf-8")
    print(f"{len(items)} items -> {OUT}")
    for p in problems:
        print(f"  ! {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
