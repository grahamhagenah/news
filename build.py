#!/usr/bin/env python3
"""Fetch every site in feeds.txt and write the latest posts to dist/."""

import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
OUT_DIR = ROOT / "dist"
REPO_URL = "https://github.com/grahamhagenah/news"
POSTS_PER_FEED = 15  # Per site; override with limit=N in feeds.txt.
PAGE_SIZE = 30  # Posts per page of the list.
DAYS_TO_KEEP = 3  # Older posts are dropped; override with days=N in feeds.txt.
PREVIEW_CHARS = 600  # Roughly how much text the hover preview shows.
# What sites see when the build fetches them. Keep it: some sites' bot filters (Marginal Revolution,
# InsideEVs) block a user agent containing "newsfeed" but allow this one.
USER_AGENT = "Mozilla/5.0 (compatible; rss-reader/1.0)"

FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
COMMON_FEED_PATHS = ["/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml"]


def is_site_line(line):
    line = line.strip()
    return bool(line) and not line.startswith("#")


def parse_site(line):
    """A feeds.txt line: a URL, then an optional name and options like limit=5 or days=14."""
    url, *words = line.split()
    site = {"url": url, "name": "", "limit": POSTS_PER_FEED, "days": DAYS_TO_KEEP}
    name = []
    for word in words:
        option = re.fullmatch(r"(limit|days)=(\d+)", word)
        if option:
            site[option.group(1)] = int(option.group(2))
        else:
            name.append(word)
    site["name"] = " ".join(name)
    return site


def read_sites():
    return [parse_site(line) for line in FEEDS_FILE.read_text().splitlines() if is_site_line(line)]


def fetch(url, attempts=2, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.geturl(), response.read()
        except Exception as error:
            # Timeouts, dropped connections and 5xx errors are often momentary; a 404 won't change.
            momentary = not isinstance(error, urllib.error.HTTPError) or error.code >= 500
            if not momentary or attempt == attempts - 1:
                raise
            time.sleep(2)


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


def without_tracking(url):
    """Drop utm_ tracking parameters, which some feeds add to every link."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(key, value) for key, value in query if not key.startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(kept))) if len(kept) < len(query) else url


def entry_comments(entry, link, feed_url):
    """The post's discussion page and comment count, when the feed has them (Hacker News does)."""
    url, count = None, None
    # <comments> holds the discussion URL; WordPress's <slash:comments> holds a count.
    for child in children(entry, "comments"):
        text = (child.text or "").strip()
        if text.startswith("http"):
            url = urljoin(feed_url, text)
        elif text.isdigit():
            count = int(text)
    hnrss_count = re.search(r"# Comments: (\d+)", child_text(entry, "description"))
    if hnrss_count:
        count = int(hnrss_count.group(1))
    # Comments on the post's own page (most blogs, or an Ask HN post) are already one click away.
    if url and url.split("#")[0] == link.split("#")[0]:
        return None, None
    return url, count


BLOCK_TAG = re.compile(r"</?(p|div|blockquote|li|ul|ol|h[1-6]|br|pre|table|tr)\b[^>]*>", re.I)
# hnrss describes link posts with these lines instead of any article text.
BOILERPLATE = re.compile(r"^(Article URL|Comments URL|Points|# Comments):")
BARE_LINKS = re.compile(r"[\s,]*(https?://\S+[\s,]*)+")


def excerpt(markup, max_chars=PREVIEW_CHARS):
    """The first few paragraphs of an HTML snippet, as plain text, cut to about max_chars."""
    markup = re.sub(r"(?is)<(script|style|figure)\b.*?</\1>", " ", markup)
    paragraphs = [clean(part) for part in re.split(r"\n\s*\n", BLOCK_TAG.sub("\n\n", markup))]
    kept = []
    budget = max_chars
    for paragraph in paragraphs:
        # Skip fragments like lone links, handles and "Thanks!" — they don't say what the post is about.
        if len(paragraph.split()) < 4 or BOILERPLATE.match(paragraph) or BARE_LINKS.fullmatch(paragraph):
            continue
        if len(paragraph) > budget:
            kept.append(paragraph[:budget].rsplit(" ", 1)[0] + "…")
            break
        kept.append(paragraph)
        budget -= len(paragraph)
        if len(kept) == 3:
            break
    return kept


class MetaDescriptionFinder(HTMLParser):
    """Collects the summary a page offers for link previews."""

    KEYS = ("og:description", "twitter:description", "description")

    def __init__(self):
        super().__init__()
        self.found = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if tag == "meta" and key in self.KEYS and attrs.get("content"):
            self.found.setdefault(key, attrs["content"])


def page_summary(url):
    """For posts whose feed has no text, use the linked page's own link-preview summary."""
    try:
        _, body = fetch(url, attempts=1, timeout=10)
    except Exception:
        return []
    finder = MetaDescriptionFinder()
    finder.feed(body[:500_000].decode("utf-8", "replace"))
    for key in finder.KEYS:
        # Very short descriptions are usually the site's tagline, not a summary of the page.
        if len(finder.found.get(key, "")) >= 40:
            return excerpt(html.escape(finder.found[key]))
    return []


def read_feed(site):
    feed_url, root = find_feed(site["url"])
    channel = children(root, "channel")
    meta = channel[0] if channel else root
    # RSS 2.0 nests items in <channel>; Atom and RSS 1.0 keep them at the top level.
    entries = children(meta, "item") or children(root, "item") or children(root, "entry")
    cutoff = datetime.now(timezone.utc) - timedelta(days=site["days"])

    posts = []
    for entry in entries:
        title = clean(child_text(entry, "title"))
        link = entry_link(entry)
        date = parse_date(child_text(entry, "pubDate", "published", "updated", "date"))
        if title and link and (date is None or date >= cutoff):
            link = without_tracking(urljoin(feed_url, link))
            summary = excerpt(child_text(entry, "encoded", "content", "description", "summary"))
            comments, comment_count = entry_comments(entry, link, feed_url)
            posts.append({
                "title": title,
                "link": link,
                "date": date,
                "summary": summary,
                "comments": comments,
                "comment_count": comment_count,
            })

    return {
        "name": site["name"] or clean(child_text(meta, "title")) or site["url"],
        "url": site["url"],
        "feed_url": feed_url,
        "posts": posts[: site["limit"]],
    }


def load(site):
    try:
        return read_feed(site)
    except Exception as error:
        return {"name": site["name"] or site["url"], "url": site["url"], "error": str(error), "posts": []}


def render_time(date, css_class=""):
    attr = f' class="{css_class}"' if css_class else ""
    return f'<time{attr} datetime="{date.isoformat()}">{date.strftime("%b %-d")}</time>'


def all_posts(feeds):
    posts = [dict(post, source=feed["name"]) for feed in feeds for post in feed["posts"]]
    # Newest first; posts without a date sink to the bottom.
    posts.sort(key=lambda post: post["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return posts


def render_json(posts, built_at):
    """The current list, which the open page checks for new posts, and which works as an export."""
    return json.dumps(
        {
            "updated": built_at.isoformat(),
            "posts": [
                {
                    "title": post["title"],
                    "link": post["link"],
                    "source": post["source"],
                    "date": post["date"].isoformat() if post["date"] else None,
                    "comments": post["comments"],
                }
                for post in posts
            ],
        },
        ensure_ascii=False,
        indent=1,
    )


def render_index(feeds, posts, built_at):
    items = []
    for post in posts:
        when = render_time(post["date"]) if post["date"] else ""
        # The full headline leads the preview, shown only when the one-line headline is cut off.
        paragraphs = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in post["summary"])
        preview = (
            f'<div class="preview{"" if paragraphs else " title-only"}">'
            f'<p class="full-title">{html.escape(post["title"])}</p>{paragraphs}</div>'
        )
        comments = ""
        if post["comments"]:
            count = post["comment_count"]
            label = "comments" if count is None else "1 comment" if count == 1 else f"{count} comments"
            comments = f'<a class="comments" href="{html.escape(post["comments"])}">{label}</a>'
        items.append(
            f'<li><span class="source"><span>{html.escape(post["source"])}</span></span>'
            f'<div class="headline"><a class="title" href="{html.escape(post["link"])}">{html.escape(post["title"])}</a>'
            f"{when}{comments}{preview}</div></li>"
        )

    failed = [feed["name"] for feed in feeds if not feed["posts"]]
    failed_note = f"<p>Couldn’t load {html.escape(', '.join(failed))}.</p>\n" if failed else ""

    body = (
        '<button class="new-posts" hidden></button>\n'
        f'<ul class="posts" data-page-size="{PAGE_SIZE}">\n' + "\n".join(items) + "\n</ul>\n"
        '<nav class="pager"></nav>\n'
        f"<footer>\n{failed_note}"
        f'<p>Updated {render_time(built_at, "updated")} · <a href="sources.html">Add or remove sites</a></p>\n'
        "</footer>\n"
        f"<script>{INDEX_JS}</script>"
    )
    return page("Newsfeed", body)


INDEX_JS = """
  const links = [...document.querySelectorAll(".posts a.title")];

  // Put a green dot beside headlines you haven't clicked. Browsers keep visited links private from pages,
  // so clicks are remembered here instead, in this browser only, for 30 days.
  let clicked = {};
  try { clicked = JSON.parse(localStorage.getItem("reader-clicked") || "{}"); } catch (error) {}
  const monthAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
  for (const href in clicked) if (clicked[href] < monthAgo) delete clicked[href];
  const saveClicked = () => {
    try { localStorage.setItem("reader-clicked", JSON.stringify(clicked)); } catch (error) {}
  };
  for (const a of links) {
    const li = a.closest("li");
    li.classList.toggle("unread", !clicked[a.href]);
    const markRead = event => {
      if (event.button > 1) return; // Right-clicks only open a menu.
      clicked[a.href] = Date.now();
      li.classList.remove("unread");
      saveClicked();
    };
    a.addEventListener("click", markRead);
    a.addEventListener("auxclick", markRead);
  }
  saveClicked();

  // Show a preview above its headline instead of below when it would run off the bottom of the window.
  for (const a of links) {
    a.addEventListener("mouseenter", () => {
      a.parentElement.classList.toggle("truncated", a.scrollWidth > a.clientWidth);
      const preview = a.parentElement.querySelector(".preview");
      if (preview) preview.classList.toggle("above", a.getBoundingClientRect().bottom + preview.offsetHeight + 24 > innerHeight);
    });
  }

  // Opened from the home screen there's no pull-to-refresh, so reload on return after five minutes away.
  let hiddenAt = 0;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) hiddenAt = Date.now();
    else if (hiddenAt && Date.now() - hiddenAt > 5 * 60 * 1000) location.reload();
  });

  // While the page is on screen, check every two minutes whether a newer build has posts this page
  // doesn't, and offer a button to load them.
  const button = document.querySelector(".new-posts");
  const onPage = new Set(links.map(a => a.href));
  async function checkForNewPosts() {
    if (document.hidden) return;
    try {
      const latest = await (await fetch("posts.json?" + Date.now(), { cache: "no-store" })).json();
      const count = latest.posts.filter(post => !onPage.has(new URL(post.link, location.href).href)).length;
      button.textContent = count === 1 ? "1 new post" : count + " new posts";
      button.hidden = count === 0;
    } catch (error) {}
  }
  setInterval(checkForNewPosts, 2 * 60 * 1000);
  document.addEventListener("visibilitychange", checkForNewPosts);
  button.addEventListener("click", () => {
    // New posts arrive at the top, so go to page one. Reloading (rather than following a link)
    // makes the browser fetch the page fresh instead of from its cache.
    history.replaceState(null, "", location.pathname);
    scrollTo(0, 0);
    location.reload();
  });

  // Show one page of posts at a time; ?page=2 shows the next set. Every post is in the page, so the
  // new-post check and the unread dots still see all of them.
  const list = document.querySelector(".posts");
  const pageSize = Number(list.dataset.pageSize);
  const items = [...list.children];
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.min(pages, Math.max(1, parseInt(new URLSearchParams(location.search).get("page")) || 1));
  items.forEach((li, i) => { li.hidden = i < (page - 1) * pageSize || i >= page * pageSize; });
  list.classList.add("paged");
  if (pages > 1) {
    const link = (n, text) => `<a href="${n === 1 ? location.pathname : "?page=" + n}">${text}</a>`;
    document.querySelector(".pager").innerHTML =
      (page > 1 ? link(page - 1, "← Newer") : "<span></span>") +
      `<span>Page ${page} of ${pages}</span>` +
      (page < pages ? link(page + 1, "Older →") : "<span></span>");
  }
"""


ISSUE_NOTE = (
    "Press Create to make this change. Within a minute a bot updates feeds.txt, replies here, "
    "and closes this issue."
)


def render_sources(feeds, built_at):
    rows = []
    for feed in feeds:
        status = f'{len(feed["posts"])} posts' if feed["posts"] else "couldn’t load"
        remove = f"{REPO_URL}/issues/new?" + urlencode({"title": f"Remove {feed['url']}", "body": ISSUE_NOTE})
        rows.append(
            f'<li><a href="{html.escape(feed["url"])}">{html.escape(feed["name"])}</a>'
            f'<span class="note">{status}</span><a class="remove" href="{html.escape(remove)}" target="_blank" rel="noopener">remove</a></li>'
        )

    body = f"""<h1>Add a site</h1>
<form class="add-site" action="{REPO_URL}/issues/new" data-note="{html.escape(ISSUE_NOTE)}">
<input name="site" placeholder="https://example.com/" autocapitalize="off" autocorrect="off" spellcheck="false" required>
<input name="label" placeholder="Name (optional)">
<button>Add</button>
</form>
<p class="help">This opens a pre-filled GitHub issue; press Create. Within a minute a bot finds the site’s feed,
adds it, and replies on the issue, or tells you if it couldn’t find one. Removing a site works the same way.</p>

<h1>Sources</h1>
<ul class="sources">
{chr(10).join(rows)}
</ul>

<h1>Give a site more or less room</h1>
<p>Each site shows up to {POSTS_PER_FEED} posts from the last {DAYS_TO_KEEP} days. Change that for one site with
options after its name, when you add it or in feeds.txt:</p>
<ul class="options">
<li><code>limit=5</code> shows at most 5 posts, for sites that post constantly.</li>
<li><code>days=14</code> keeps posts for 14 days, for blogs that post rarely.</li>
</ul>
<p>For example, a name of <code>The New York Times limit=8</code>. To change a site that’s already here, edit
<a href="{REPO_URL}/edit/main/feeds.txt">feeds.txt on GitHub</a>. If a site doesn’t show up, the
<a href="{REPO_URL}/actions">build log</a> says what it returned.</p>

<h1>Export</h1>
<p><a href="feeds.opml" download>Download these sites as OPML</a>, the file format other feed readers import.
The current list of posts is also available as <a href="posts.json">JSON</a>.</p>

<footer><p>Updated {render_time(built_at, "updated")} · <a href="./">Back to the news</a></p></footer>
<script>
  document.querySelector(".add-site").addEventListener("submit", event => {{
    event.preventDefault();
    const form = event.target;
    const title = ["Add", form.elements.site.value.trim(), form.elements.label.value.trim()].filter(Boolean).join(" ");
    // Open GitHub in a new tab, and clear the form so this page is ready for the next site.
    window.open(form.action + "?" + new URLSearchParams({{ title, body: form.dataset.note }}), "_blank", "noopener");
    form.reset();
  }});
</script>"""
    return page("Sources", body)


def render_opml(feeds, built_at):
    def attr(value):
        return html.escape(value, quote=True)

    outlines = [
        f'    <outline type="rss" text="{attr(feed["name"])}" title="{attr(feed["name"])}" '
        f'xmlUrl="{attr(feed.get("feed_url", feed["url"]))}" htmlUrl="{attr(feed["url"])}"/>'
        for feed in feeds
    ]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>Newsfeed sources</title>
    <dateCreated>{format_datetime(built_at)}</dateCreated>
  </head>
  <body>
{chr(10).join(outlines)}
  </body>
</opml>
"""


def page(title, body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-title" content="Newsfeed">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="manifest.webmanifest">
<title>{title}</title>
<style>
  html {{ background: #000; }}
  body {{ margin: 0; padding: 3rem 1.25rem 4rem; color: #fff; background: #000;
         font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }}
  main {{ max-width: 46rem; margin: 0 auto; }}
  h1 {{ margin: 3rem 0 .75rem; color: #777; font-size: .75rem; font-weight: 600;
        letter-spacing: .08em; text-transform: uppercase; }}
  h1:first-child, header + h1 {{ margin-top: 0; }}
  header {{ margin-bottom: 2rem; }}
  .home, .home:visited {{ color: #fff; font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; }}
  .home:hover {{ text-decoration: none; }}
  ul {{ margin: 0; padding: 0; list-style: none; }}
  li {{ padding: .4rem 0; }}
  ol {{ padding-left: 1.25rem; }}
  ol li {{ padding: .3rem 0; }}
  .posts li {{ display: grid; grid-template-columns: 9rem 1fr; gap: 1.25rem; align-items: baseline; }}
  /* Until the script picks the page, show the first one, so the whole list never flashes up. */
  .posts:not(.paged) li:nth-child(n+{PAGE_SIZE + 1}), .posts li[hidden] {{ display: none; }}
  .pager {{ display: flex; justify-content: space-between; margin-top: 2.5rem; color: #666; font-size: .8rem; }}
  .pager a, .pager a:visited {{ color: #999; }}
  .source, .note, time, footer {{ color: #666; font-size: .8em; }}
  /* Every row is one line tall: long headlines and source names end in an ellipsis. */
  .headline {{ position: relative; display: flex; align-items: baseline; min-width: 0; }}
  .headline a.title {{ min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
  .headline time, .headline .comments {{ flex: none; }}
  .source {{ position: relative; min-width: 0; }}
  .source > span {{ display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }}
  .unread .source::before {{ content: ""; position: absolute; left: -.9rem; top: .5em; width: 6px; height: 6px;
                            border-radius: 50%; background: #34d399; }}
  .preview {{ position: absolute; z-index: 1; top: calc(100% + .5rem); left: -1rem; width: min(34rem, calc(100% + 1rem));
             box-sizing: border-box; padding: .9rem 1rem; background: #000; border: 1px solid #333; border-radius: 6px;
             color: #bbb; font-size: .85em; line-height: 1.5; pointer-events: none;
             visibility: hidden; opacity: 0; transition: opacity .1s, visibility 0s .1s; }}
  .preview.above {{ top: auto; bottom: calc(100% + .5rem); }}
  .preview p {{ margin: 0 0 .7em; }}
  .preview p:last-child {{ margin-bottom: 0; }}
  .preview .full-title {{ display: none; color: #fff; }}
  .truncated .preview .full-title {{ display: block; }}
  .headline:not(.truncated) .preview.title-only {{ display: none; }}
  .headline a.title:hover ~ .preview {{ visibility: visible; opacity: 1; transition: opacity .1s .4s, visibility 0s .4s; }}
  @media (hover: none) {{ .preview {{ display: none; }} }}
  .new-posts {{ position: fixed; z-index: 2; top: calc(env(safe-area-inset-top) + .75rem); left: 50%;
               transform: translateX(-50%); padding: .45rem 1.1rem; border: 0; border-radius: 999px;
               background: #fff; color: #000; font-family: inherit; font-size: .8rem; font-weight: 600; cursor: pointer; }}
  .new-posts[hidden] {{ display: none; }}
  @media (max-width: 34rem) {{
    /* On a phone one line is too few words, so headlines wrap in full. */
    .posts li {{ grid-template-columns: 1fr; gap: 0; }}
    .headline {{ display: block; }}
    .headline a.title {{ white-space: normal; }}
    .preview {{ display: none; }}
    /* The unread dot moves beside the headline's first line, below the source name. */
    .posts li {{ position: relative; }}
    .unread .source::before {{ display: none; }}
    .unread::before {{ content: ""; position: absolute; left: -.9rem; top: calc(.4rem + 1.16em + .725em - 1px);
                      width: 6px; height: 6px; border-radius: 50%; background: #34d399; }}
  }}
  a {{ color: #fff; text-decoration: none; }}
  a:visited {{ color: #666; }}
  .comments, .comments:visited {{ margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }}
  a:hover {{ text-decoration: underline; }}
  p a, ol a {{ text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }}
  time, .note {{ margin-left: .6em; white-space: nowrap; }}
  .remove, .remove:visited {{ margin-left: .8em; color: #666; font-size: .8em; }}
  .sources {{ columns: 2; column-gap: 2.5rem; }}
  .sources li {{ break-inside: avoid; }}
  @media (max-width: 34rem) {{ .sources {{ columns: 1; }} }}
  .add-site {{ display: flex; flex-wrap: wrap; gap: .5rem; }}
  .add-site input {{ flex: 1 1 12rem; min-width: 0; padding: .55rem .75rem; border: 1px solid #333; border-radius: 6px;
                    background: #000; color: #fff; font: inherit; font-size: .9rem; }}
  .add-site input::placeholder {{ color: #555; }}
  .add-site input:focus {{ outline: none; border-color: #888; }}
  .add-site button {{ padding: .55rem 1.3rem; border: 0; border-radius: 999px; background: #fff; color: #000;
                     font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer; }}
  .help {{ color: #777; font-size: .85em; }}
  code {{ color: #ccc; font-size: .9em; }}
  footer {{ margin-top: 4rem; }}
  footer p {{ margin: .4rem 0; }}
  footer a, footer a:visited {{ color: #999; }}
  footer time {{ margin: 0; font-size: inherit; }}
</style>
</head>
<body>
<main>
<header><a class="home" href="./">Newsfeed</a></header>
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

    missing = [post for feed in feeds for post in feed["posts"] if not post["summary"]]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for post, summary in zip(missing, pool.map(lambda post: page_summary(post["link"]), missing)):
            post["summary"] = summary

    for feed in feeds:
        if "error" in feed:
            print(f"✗ {feed['url']}: {feed['error']}", file=sys.stderr)
        else:
            previews = sum(1 for post in feed["posts"] if post["summary"])
            print(f"✓ {feed['name']}: {len(feed['posts'])} posts, {previews} with previews, from {feed['feed_url']}")

    if not any(feed["posts"] for feed in feeds):
        sys.exit("No feeds loaded — not writing the page.")

    built_at = datetime.now(timezone.utc)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    posts = all_posts(feeds)
    (OUT_DIR / "index.html").write_text(render_index(feeds, posts, built_at))
    (OUT_DIR / "posts.json").write_text(render_json(posts, built_at))
    (OUT_DIR / "sources.html").write_text(render_sources(feeds, built_at))
    (OUT_DIR / "feeds.opml").write_text(render_opml(feeds, built_at))
    print(f"Wrote {OUT_DIR.relative_to(ROOT)}/index.html, posts.json, sources.html and feeds.opml")


if __name__ == "__main__":
    main()
