# Morning Read

One page, built fresh every morning, holding everything I read.

## Adding a source

Edit `config.yaml`. Nothing else. A source is three lines:

```yaml
  - name: Rational Optimist
    url: https://rationaloptimistsociety.substack.com/feed
    section: Big Picture
```

Optional per source:

| Key | Use it when |
|---|---|
| `max_items` | One feed is loud and crowds the page |
| `max_age_hours` | Anything not daily. Weekly `192`, monthly `780`, **`0` = no age filter, always show the latest** (for a few-times-a-year source like a memo writer) |
| `link_fallback` | The feed omits per-item links, which podcast feeds usually do |

## Testing a feed before you add it

```
python probe.py "https://example.com/feed"
```

Prints HTTP status, entry count, feed title, and the newest item's date.
`n=0` or a 404 means don't bother. A newest-date of two years ago means the
feed is abandoned - which is true of a surprising number of them.

## Running it locally

```
pip install -r requirements.txt
python build.py
```

Writes `docs/index.html`. Open it in a browser.

## How it stays fresh

`.github/workflows/build.yml` rebuilds at 09:00, 10:00 and 11:00 UTC daily
and publishes to GitHub Pages. Three runs covers both daylight-saving states
and survives one feed being briefly down. You can also trigger a rebuild by
hand from the repo's Actions tab.

## Things that are known-awkward

- **Anthropic has no official RSS.** `anthropic.com/rss.xml` is a 404. We use
  a community-maintained mirror, which supplies no descriptions - so Anthropic
  items are headline-only. If that mirror ever goes stale, the page says so in
  the note at the bottom.
- **Summaries come from the feed, not from a model.** Nothing is invented, and
  the build costs nothing to run. The tradeoff is that quality varies with the
  publisher: OpenAI writes real one-liners, Substack gives you the post's
  subtitle, and Anthropic gives nothing.
- **LinkedIn has no feeds.** Posts by individuals have to come through a
  third-party generator (RSS.app) or a Google Alerts RSS feed.

## Files

| File | Edit it? |
|---|---|
| `config.yaml` | **Yes.** Sources live here. |
| `template.html` | Only to restyle. |
| `build.py` | No. Fetch, clean, render. |
| `probe.py` | No. Feed-testing helper. |
| `docs/index.html` | Never - generated. |
