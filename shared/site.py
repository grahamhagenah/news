"""What the newsfeed and the events page share: fetching, the fallback and alert bookkeeping, the icon system,
and the page itself around each one's list (head, header, base styles, and the script that ages timestamps).
Each site's build.py adds its own readers, rows and styles on top."""

import html
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# The two pages, in the order the header names them.
SITES = {
    "news": ("Newsfeed", "https://news.grahamhagenah.com/"),
    "events": ("Events", "https://events.grahamhagenah.com/"),
}


def fetch(url, user_agent, attempts=3, timeout=20):
    """The address the request ended at, after redirects, and the response body."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.geturl(), response.read()
        except Exception as error:
            # Timeouts, dropped connections and 5xx errors (like the 520s Cloudflare gives when a site's own
            # server stumbles) are often momentary; a 404 won't change. Wait a little longer each time.
            momentary = not isinstance(error, urllib.error.HTTPError) or error.code >= 500
            if not momentary or attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))


def previous_build(url, user_agent):
    """The JSON the live page published last build: its sources' items, for falling back on, and which were
    failing. Empty if it can't be had."""
    try:
        return json.loads(fetch(url, user_agent)[1])
    except Exception as error:
        print(f"  no earlier build to fall back on ({error})", file=sys.stderr)
        return {}


def still_failing(errors, previous, built_at):
    """Each failing source's error and when it started failing, carried over from build to build, so
    alerts.py can tell a hiccup from an outage."""
    before = previous.get("failing", {})
    return {
        name: {"since": before.get(name, {}).get("since", built_at.isoformat()), "error": error}
        for name, error in errors.items()
    }


def icon_symbols(drawings):
    """Each icon drawn once, at the top of the page, for rows to point to. Drawings are 16×16 strokes."""
    return '<svg class="symbols" aria-hidden="true">' + "".join(
        f'<symbol id="icon-{name}" viewBox="0 0 16 16"><g fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round">{drawing}</g></symbol>'
        for name, drawing in drawings.items()
    ) + "</svg>"


def icon(name, label=None):
    """An icon; with no label, where the text beside it already says it, screen readers skip it."""
    described = f'role="img" aria-label="{html.escape(label)}"' if label else 'aria-hidden="true"'
    return f'<svg class="icon" {described}><use href="#icon-{name}"/></svg>'


BASE_CSS = """
  html { background: #000; }
  body { margin: 0; padding: 3rem 1.25rem 4rem; color: #fff; background: #000;
         font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }
  main { max-width: 46rem; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; margin-bottom: 2rem; }
  .header-note { color: #666; font-size: .8rem; white-space: nowrap; }
  .header-note time { margin: 0; font-size: inherit; }
  /* The newsfeed and the events page, as a pair: the one you're on in white, the other a gray link to it. */
  .sites { display: flex; gap: .9rem; }
  .sites a, .sites a:visited { color: #555; font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; text-decoration: none; }
  .sites a:hover { color: #999; }
  .sites a[aria-current], .sites a[aria-current]:hover { color: #fff; }
  .filter { display: flex; flex-wrap: wrap; gap: .4rem 1.1rem; margin: -.75rem 0 3.5rem; }
  .filter button { padding: 0; border: 0; background: none; color: #666; font: inherit; font-size: .8rem; cursor: pointer; }
  .filter button:hover { color: #999; }
  .filter button[aria-pressed="true"] { color: #fff; }
  .filter .icon { margin-right: .4em; vertical-align: -1px; }
  /* Whatever the filter and pager hide stays hidden, however specific the rules that lay it out. */
  [hidden] { display: none !important; }
  ul { margin: 0; padding: 0; list-style: none; }
  /* Rows: the source on the left, led by its icon, then the headline, one line tall, with its details after. */
  .row { position: relative; display: grid; grid-template-columns: 10rem 1fr; gap: 1.25rem; align-items: baseline; padding: .4rem 0; }
  .symbols { position: absolute; width: 0; height: 0; overflow: hidden; }
  .icon { flex: none; width: 12px; height: 12px; }
  .source { display: flex; align-items: center; gap: .5em; min-width: 0; color: #666; font-size: .8em; }
  .source > span { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .headline { display: flex; align-items: baseline; min-width: 0; }
  .headline .title { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .pager { display: flex; justify-content: space-between; margin-top: 2.5rem; color: #666; font-size: .8rem; }
  .pager a, .pager a:visited { color: #999; }
  .empty { color: #666; font-size: .9rem; }
  a { color: #fff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  footer { margin-top: 4rem; color: #666; font-size: .8em; }
  footer p { margin: .4rem 0; }
  footer a, footer a:visited { color: #999; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  @media (max-width: 34rem) {
    /* On a phone one line is too few words, so headlines wrap in full, below the source. The icon moves
       beside the headline's first line, into a slot at the left edge that both start after. Its top: the
       row's padding, then (in the source's text size) 1.45em for the source's line and .9em for half the
       headline's, less half the icon. */
    .row { grid-template-columns: 1fr; gap: 0; padding-left: calc(12px + .5em); }
    .headline { display: block; }
    .headline .title { white-space: normal; }
    .source .icon { position: absolute; left: 0; top: calc(.4rem + 2.35em - 6px); }
  }
"""

# Timestamps age in the reader's browser, so a page built hours ago still reads right: "16m" beside an item,
# "Updated 2h ago" in the header. (Showtimes are <time data-time>, with no datetime, and are left alone.)
AGES = """
<script>
  for (const t of document.querySelectorAll("time[datetime]")) {
    const s = Math.max(60, (Date.now() - new Date(t.dateTime)) / 1000);
    const age = s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d";
    t.textContent = t.matches(".updated, .ago") ? age + " ago" : age;
  }
</script>"""


def page(site, title, body, css="", head="", symbols="", updated=None):
    """A whole page: the shared head, header and styles, then this site's styles, icons and body."""
    name = SITES[site][0]
    links = "".join(
        f'<a href="./" aria-current="page">{label}</a>' if key == site else f'<a href="{url}">{label}</a>'
        for key, (label, url) in SITES.items()
    )
    note = (
        f'<span class="header-note">Updated <time class="updated" datetime="{updated.isoformat()}">'
        f'{updated.strftime("%b")} {updated.day}</time></span>' if updated else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-title" content="{name}">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
{head}
<title>{title}</title>
<style>{BASE_CSS}{css}</style>
</head>
<body>
{symbols}
<main>
<header><nav class="sites" aria-label="Sites">{links}</nav>{note}</header>
{body}
</main>{AGES}
</body>
</html>
"""
