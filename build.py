#!/usr/bin/env python3
"""Fetch every site in feeds.txt and write the latest posts to dist/."""

import html
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
OUT_DIR = ROOT / "dist"
REPO_URL = "https://github.com/grahamhagenah/news"
POSTS_PER_FEED = 15
USER_AGENT = "Mozilla/5.0 (compatible; rss-reader/1.0)"

FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
COMMON_FEED_PATHS = ["/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml"]


def read_sites():
    sites = []
    for line in FEEDS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, name = line.partition(" ")
        sites.append((url, name.strip()))
    return sites


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.geturl(), response.read()


class FeedLinkFinder(HTMLParser):
    """Collects <link rel="alternate" type="application/rss+xml" href="..."> tags."""

    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        rel = (attrs.get("rel") or "").lower().split()
        if tag == "link" and "alternate" in rel and attrs.get("type") in FEED_TYPES and attrs.get("href"):
            self.hrefs.append(attrs["href"])


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xml(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    return root if local_name(root.tag) in ("rss", "feed", "RDF") else None


def find_feed(url):
    """Return (feed_url, parsed_root), following the page's feed link if given a homepage."""
    page_url, body = fetch(url)
    root = parse_xml(body)
    if root is not None:
        return page_url, root

    finder = FeedLinkFinder()
    finder.feed(body.decode("utf-8", "replace"))
    for candidate in finder.hrefs + COMMON_FEED_PATHS:
        feed_url = urljoin(page_url, candidate)
        try:
            feed_url, body = fetch(feed_url)
        except Exception:
            continue
        root = parse_xml(body)
        if root is not None:
            return feed_url, root
    raise ValueError("no RSS or Atom feed found")


def children(element, name):
    return [child for child in element if local_name(child.tag) == name]


def child_text(element, *names):
    for name in names:
        for child in children(element, name):
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def clean(text):
    text = re.sub(r"<[^>]+>", "", html.unescape(text))
    text = re.sub(r"\s+", " ", text).strip()
    # Some feeds leave gaps where links were stripped: "“ Gimme", "essay , posted".
    return re.sub(r"([“(\[]) | ([,.;:!?)\]”])", r"\1\2", text)


def parse_date(text):
    if not text:
        return None
    try:
        date = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        iso = re.sub(r"\.\d+", "", text).replace("Z", "+00:00")
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
        try:
            date = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc)


def entry_link(entry):
    for link in children(entry, "link"):
        if link.text and link.text.strip():
            return link.text.strip()
        if link.get("href") and link.get("rel", "alternate") == "alternate":
            return link.get("href")
    guid = child_text(entry, "guid", "id")
    return guid if guid.startswith("http") else ""


def read_feed(url, name):
    feed_url, root = find_feed(url)
    channel = children(root, "channel")
    meta = channel[0] if channel else root
    # RSS 2.0 nests items in <channel>; Atom and RSS 1.0 keep them at the top level.
    entries = children(meta, "item") or children(root, "item") or children(root, "entry")

    posts = []
    for entry in entries[:POSTS_PER_FEED]:
        title = clean(child_text(entry, "title"))
        link = entry_link(entry)
        if title and link:
            date = parse_date(child_text(entry, "pubDate", "published", "updated", "date"))
            posts.append({"title": title, "link": urljoin(feed_url, link), "date": date})

    return {
        "name": name or clean(child_text(meta, "title")) or url,
        "url": url,
        "feed_url": feed_url,
        "posts": posts,
    }


def load(site):
    url, name = site
    try:
        return read_feed(url, name)
    except Exception as error:
        return {"name": name or url, "url": url, "error": str(error), "posts": []}


def render_time(date, css_class=""):
    attr = f' class="{css_class}"' if css_class else ""
    return f'<time{attr} datetime="{date.isoformat()}">{date.strftime("%b %-d")}</time>'


def render_index(feeds, built_at):
    posts = [dict(post, source=feed["name"]) for feed in feeds for post in feed["posts"]]
    # Newest first; posts without a date sink to the bottom.
    posts.sort(key=lambda post: post["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    items = []
    for post in posts:
        when = render_time(post["date"]) if post["date"] else ""
        items.append(
            f'<li><span class="source">{html.escape(post["source"])}</span>'
            f'<span><a href="{html.escape(post["link"])}">{html.escape(post["title"])}</a>{when}</span></li>'
        )

    failed = [feed["name"] for feed in feeds if not feed["posts"]]
    failed_note = f"<p>Couldn’t load {html.escape(', '.join(failed))}.</p>\n" if failed else ""

    body = (
        '<ul class="posts">\n' + "\n".join(items) + "\n</ul>\n"
        f"<footer>\n{failed_note}"
        f'<p>Updated {render_time(built_at, "updated")} · <a href="sources.html">Add or remove sites</a></p>\n'
        "</footer>"
    )
    return page("Reader", body)


def render_sources(feeds, built_at):
    rows = []
    for feed in feeds:
        status = f'{len(feed["posts"])} posts' if feed["posts"] else "couldn’t load"
        rows.append(
            f'<li><a href="{html.escape(feed["url"])}">{html.escape(feed["name"])}</a>'
            f'<span class="note">{status}</span></li>'
        )

    body = f"""<h1>Sources</h1>
<ul>
{chr(10).join(rows)}
</ul>

<h1>Add or remove a site</h1>
<ol>
<li>Open <a href="{REPO_URL}/edit/main/feeds.txt">feeds.txt on GitHub</a>, signed in as the repo’s owner.</li>
<li>To add a site, put its homepage on a new line, like <code>https://kottke.org/</code>.
The build finds the site’s feed by itself.</li>
<li>To show a different name, add it after the URL: <code>https://www.nytimes.com/ The New York Times</code>.</li>
<li>To remove a site, delete its line.</li>
<li>Click “Commit changes”. This page and the news rebuild within a minute or two.</li>
</ol>
<p>If a new site doesn’t show up, its homepage may not point to a feed. Find the site’s RSS or Atom link
and put that URL in feeds.txt instead. The <a href="{REPO_URL}/actions">build log</a> says what each site returned.</p>

<footer><p>Updated {render_time(built_at, "updated")} · <a href="./">Back to the news</a></p></footer>"""
    return page("Sources", body)


def page(title, body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{title}</title>
<style>
  html {{ background: #000; }}
  body {{ margin: 0; padding: 3rem 1.25rem 4rem; color: #fff; background: #000;
         font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }}
  main {{ max-width: 46rem; margin: 0 auto; }}
  h1 {{ margin: 3rem 0 .75rem; color: #777; font-size: .75rem; font-weight: 600;
        letter-spacing: .08em; text-transform: uppercase; }}
  h1:first-child {{ margin-top: 0; }}
  ul {{ margin: 0; padding: 0; list-style: none; }}
  li {{ padding: .4rem 0; }}
  ol {{ padding-left: 1.25rem; }}
  ol li {{ padding: .3rem 0; }}
  .posts li {{ display: grid; grid-template-columns: 9rem 1fr; gap: 1.25rem; align-items: baseline; }}
  .source, .note, time, footer {{ color: #666; font-size: .8em; }}
  @media (max-width: 34rem) {{
    .posts li {{ grid-template-columns: 1fr; gap: 0; }}
  }}
  a {{ color: #fff; text-decoration: none; }}
  a:visited {{ color: #666; }}
  a:hover {{ text-decoration: underline; }}
  time, .note {{ margin-left: .6em; white-space: nowrap; }}
  code {{ color: #ccc; font-size: .9em; }}
  footer {{ margin-top: 4rem; }}
  footer p {{ margin: .4rem 0; }}
  footer a, footer a:visited {{ color: #999; }}
  footer time {{ margin: 0; font-size: inherit; }}
</style>
</head>
<body>
<main>
{body}
</main>
<script>
  for (const t of document.querySelectorAll("time")) {{
    const s = Math.max(60, (Date.now() - new Date(t.dateTime)) / 1000);
    const age = s < 3600 ? Math.round(s / 60) + "m" : s < 86400 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d";
    t.textContent = t.classList.contains("updated") ? age + " ago" : age;
  }}
</script>
</body>
</html>
"""


def main():
    sites = read_sites()
    with ThreadPoolExecutor(max_workers=8) as pool:
        feeds = list(pool.map(load, sites))

    for feed in feeds:
        if "error" in feed:
            print(f"✗ {feed['url']}: {feed['error']}", file=sys.stderr)
        else:
            print(f"✓ {feed['name']}: {len(feed['posts'])} posts from {feed['feed_url']}")

    if not any(feed["posts"] for feed in feeds):
        sys.exit("No feeds loaded — not writing the page.")

    built_at = datetime.now(timezone.utc)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "index.html").write_text(render_index(feeds, built_at))
    (OUT_DIR / "sources.html").write_text(render_sources(feeds, built_at))
    print(f"Wrote {OUT_DIR.relative_to(ROOT)}/index.html and sources.html")


if __name__ == "__main__":
    main()
