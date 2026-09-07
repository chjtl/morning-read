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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

# Substack (and others behind Cloudflare) reject feedparser's default agent
# from datacenter IPs, which is where the scheduled build runs. Ask the way a
# browser would.
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

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.yaml"
OUT = ROOT / "docs" / "index.html"

TAGS = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


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


# Periods that end these do not end a sentence.
ABBREV = {
    "vs", "e.g", "i.e", "etc", "approx", "est", "no", "inc", "corp", "co",
    "ltd", "mr", "mrs", "ms", "dr", "jr", "sr", "st", "ave", "blvd", "sq",
    "ft", "u.s", "u.k", "d.c", "a.m", "p.m",
}


def _complete(text: str) -> bool:
    """True if text looks like a whole thought rather than a bad split."""
    if text.count("(") != text.count(")"):
        return False
    last = text.rstrip(".").rsplit(" ", 1)[-1].lower()
    if last in ABBREV:
        return False
    # A lone initial, e.g. "George W."
    return not (len(last) == 1 and last.isalpha())


def one_line(raw: str, limit: int = 200) -> str:
    """Reduce a feed's description blob to a single readable sentence."""
    text = WHITESPACE.sub(" ", html.unescape(TAGS.sub(" ", raw or ""))).strip()
    if not text:
        return ""

    # Grow a candidate sentence by sentence until it is long enough to stand
    # on its own and isn't a mis-split on an abbreviation or open paren.
    parts = SENTENCE_END.split(text)
    candidate = ""
    for part in parts:
        candidate = f"{candidate} {part}".strip()
        if len(candidate) >= 60 and _complete(candidate):
            break
    else:
        candidate = candidate or text

    if len(candidate) > limit:
        candidate = candidate[:limit].rsplit(" ", 1)[0].rstrip(",;:-(") + "…"
    return candidate


def pick_link(entry, feed, fallback: str = "") -> str:
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
    if feed.feed.get("link"):
        return feed.feed["link"]
    for ref in entry.get("links", []) or []:
        if ref.get("href"):
            return ref["href"]
    return ""


def fetch_feed(url: str):
    """Fetch and parse a feed, keeping the HTTP status for diagnostics."""
    resp = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    parsed.http_status = resp.status_code
    return parsed


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
            # Body that opens by restating the title - keep what follows.
            rest = line[len(title):].lstrip(" -–—:.").strip()
            if len(rest) < 40:
                continue
            line = rest
        return line
    return ""


def parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        stamp = entry.get(key)
        if stamp:
            return datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc)
    return None


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
            feed = fetch_feed(src["url"])
        except Exception as exc:  # network, DNS, HTTP error - keep going
            problems.append(f"{name}: {type(exc).__name__} - {exc}")
            continue
        if not feed.entries:
            problems.append(
                f"{name}: parsed 0 entries (HTTP {feed.http_status})"
            )
            continue

        kept = 0
        cap = src.get("max_items", default_cap)
        # A weekly source would always look empty under a daily window, so
        # each source may widen its own lookback. max_age_hours: 0 turns the
        # age filter off entirely - use it for anything that publishes a few
        # times a year, where you always want the latest one on the page.
        window = src.get("max_age_hours", default_age)
        cutoff = now - timedelta(hours=window) if window else None
        for entry in feed.entries:
            if kept >= cap:
                break
            when = parse_date(entry)
            if cutoff and when and when < cutoff:
                continue
            title = WHITESPACE.sub(" ", html.unescape(entry.get("title", ""))).strip()
            link = pick_link(entry, feed, src.get("link_fallback", ""))
            if not title or not link:
                continue
            summary = pick_summary(entry, title)
            items.append(
                Item(
                    source=name,
                    section=src.get("section", "General"),
                    title=title,
                    link=link,
                    summary=summary,
                    published=when,
                )
            )
            kept += 1

        if kept == 0:
            problems.append(f"{name}: nothing inside the time window")


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
        rows.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)
        entries = []
        for i in rows:
            summary = f'<p class="sum">{html.escape(i.summary)}</p>' if i.summary else ""
            entries.append(
                '<li class="item">'
                f'<div class="meta"><span class="src">{html.escape(i.source)}</span>'
                f'<span class="age">{i.age}</span></div>'
                f'<a class="hed" href="{html.escape(i.link)}">{html.escape(i.title)}</a>'
                f"{summary}</li>"
            )
        blocks.append(
            f'<section><h2>{html.escape(section)}</h2>'
            f'<ul>{"".join(entries)}</ul></section>'
        )

    note = ""
    if problems:
        lines = "".join(f"<li>{html.escape(p)}</li>" for p in problems)
        note = (
            '<details class="problems"><summary>'
            f"{len(problems)} source(s) quiet or failing</summary>"
            f"<ul>{lines}</ul></details>"
        )

    body = "".join(blocks) or '<p class="empty">Nothing new inside the time window.</p>'

    stamp = (
        f'<span>{now.strftime("%A")}, {now.strftime("%B")} {now.day}</span>'
        f'<span class="sep">/</span>'
        f'<span>built {now.strftime("%I:%M %p").lstrip("0").lower()}</span>'
    )

    n_sources = len({i.source for i in items})
    count = (
        f"{len(items)} item{'s' if len(items) != 1 else ''} "
        f"from {n_sources} source{'s' if n_sources != 1 else ''}"
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
