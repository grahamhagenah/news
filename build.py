#!/usr/bin/env python3
"""Fetch every site in feeds.txt and write the latest posts to dist/index.html."""

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
OUT_FILE = ROOT / "dist" / "index.html"
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


def render(feeds, built_at):
    sections = []
    for feed in feeds:
        items = []
        for post in feed["posts"]:
            when = render_time(post["date"]) if post["date"] else ""
            items.append(
                f'<li><a href="{html.escape(post["link"])}">{html.escape(post["title"])}</a>{when}</li>'
            )
        if not items:
            items.append('<li class="empty">Couldn’t load this feed.</li>')
        sections.append(
            f'<section>\n<h2>{html.escape(feed["name"])}</h2>\n<ul>\n' + "\n".join(items) + "\n</ul>\n</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Reader</title>
<style>
  html {{ background: #000; }}
  body {{ margin: 0; padding: 3rem 1.25rem 4rem; color: #fff; background: #000;
         font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }}
  main {{ max-width: 40rem; margin: 0 auto; }}
  h2 {{ margin: 3rem 0 .5rem; color: #777; font-size: .75rem; font-weight: 600;
        letter-spacing: .08em; text-transform: uppercase; }}
  section:first-child h2 {{ margin-top: 0; }}
  ul {{ margin: 0; padding: 0; list-style: none; }}
  li {{ padding: .4rem 0; }}
  a {{ color: #fff; text-decoration: none; }}
  a:visited {{ color: #666; }}
  a:hover {{ text-decoration: underline; }}
  time {{ margin-left: .6em; color: #555; font-size: .8em; white-space: nowrap; }}
  .empty, footer {{ color: #555; }}
  footer {{ margin-top: 4rem; font-size: .8rem; }}
  footer time {{ margin: 0; font-size: inherit; }}
</style>
</head>
<body>
<main>
{chr(10).join(sections)}
<footer>Updated {render_time(built_at, "updated")}</footer>
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

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(render(feeds, datetime.now(timezone.utc)))
    print(f"Wrote {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
