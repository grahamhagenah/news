#!/usr/bin/env python3
"""Fetch every site in feeds.txt and write the latest posts to dist/news. Run from the repo's top folder:
python3 -m news.build"""

import gzip
import html
import json
import os
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from shared import site as shared

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
OUT_DIR = ROOT.parent / "dist" / "news"
REPO_URL = "https://github.com/grahamhagenah/news"
POSTS_PER_FEED = 15  # Per site; override with limit=N in feeds.txt.
PAGE_SIZE = 30  # Posts per page of the list.
DAYS_TO_KEEP = 3  # Older posts are dropped; override with days=N in feeds.txt.
PREVIEW_CHARS = 600  # Roughly how much text the hover preview shows.
# Each build publishes its feeds' posts beside the page; a feed that fails next time falls back to its copy
# there, if it's no older than this.
FEEDS_URL = "https://news.grahamhagenah.com/feeds.json"
FALLBACK_LIMIT = timedelta(days=2)
# What sites see when the build fetches them. Keep it: some sites' bot filters (Marginal Revolution,
# InsideEVs) block a user agent containing "newsfeed" but allow this one.
USER_AGENT = "Mozilla/5.0 (compatible; rss-reader/1.0)"

# The endpoints Pocket Casts' own web player uses; they're undocumented, so failures fall back to web links.
POCKET_CASTS_FIND_URL = "https://refresh.pocketcasts.com/author/add_feed_url"
POCKET_CASTS_EPISODES_URL = "https://podcast-api.pocketcasts.com/podcast/full/{uuid}"

FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
COMMON_FEED_PATHS = ["/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml"]


def is_site_line(line):
    line = line.strip()
    return bool(line) and not line.startswith("#")


def parse_site(line):
    """A feeds.txt line: a URL, then an optional name and options like limit=5 or days=14."""
    url, *words = line.split()
    site = {"url": url, "name": "", "limit": POSTS_PER_FEED, "days": DAYS_TO_KEEP, "pocketcasts": ""}
    name = []
    for word in words:
        option = re.fullmatch(r"(limit|days)=(\d+)", word)
        show = re.fullmatch(r"pocketcasts=([0-9a-f-]{36})", word)
        if option:
            site[option.group(1)] = int(option.group(2))
        elif show:
            site["pocketcasts"] = show.group(1)
        else:
            name.append(word)
    site["name"] = " ".join(name)
    return site


def read_sites():
    return [parse_site(line) for line in FEEDS_FILE.read_text().splitlines() if is_site_line(line)]


def fetch(url, attempts=3, timeout=20):
    """The address the request ended at, and the body."""
    return shared.fetch(url, USER_AGENT, attempts, timeout)


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


clean = shared.clean


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


def entry_audio(entry):
    """The episode's audio file, when the post is a podcast episode."""
    # RSS <enclosure>, Media RSS <media:content medium="audio">, or Atom <link rel="enclosure">.
    for child in children(entry, "enclosure") + children(entry, "content") + children(entry, "link"):
        if local_name(child.tag) == "link" and child.get("rel") != "enclosure":
            continue
        kind = child.get("type") or child.get("medium") or ""
        url = child.get("url") or child.get("href")
        if kind.startswith("audio") and url:
            return url
    return None


# A YouTube video's address, and its id. Shorts, at youtube.com/shorts/…, aren't among them: they're left out.
YOUTUBE_VIDEO = re.compile(r"^https://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})")


def media_description(entry):
    """The description YouTube's feeds (and other Media RSS feeds) keep in <media:group>, as plain text with
    a line to each paragraph."""
    for group in children(entry, "group"):
        text = child_text(group, "description")
        if text:
            return html.escape(text).replace("\n", "<br>")
    return ""


# hnrss describes link posts with these lines instead of any article text, and Tiny Desk's videos start
# with a byline, "Ashley Pointer | September 10, 2026".
BOILERPLATE = re.compile(r"^(Article URL|Comments URL|Points|# Comments):|^[^|]{1,60} \| [A-Z][a-z]+ \d{1,2}, \d{4}$")


def excerpt(markup, max_chars=PREVIEW_CHARS):
    """The first few paragraphs of an HTML snippet, as plain text, cut to about max_chars."""
    return shared.excerpt(markup, max_chars, skip=BOILERPLATE)


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
        audio = entry_audio(entry)
        # Many podcast feeds give an episode no web page of its own, just the audio file.
        link = entry_link(entry) or audio
        date = parse_date(child_text(entry, "pubDate", "published", "updated", "date"))
        if "youtube.com/shorts/" in link:
            continue  # Shorts are the endless-scroll kind of video this page is meant to be free of.
        if title and link and (date is None or date >= cutoff):
            link = without_tracking(urljoin(feed_url, link))
            video = YOUTUBE_VIDEO.match(link)
            summary = excerpt(child_text(entry, "encoded", "content", "description", "summary") or media_description(entry))
            comments, comment_count = entry_comments(entry, link, feed_url)
            posts.append({
                "title": title,
                "link": link,
                "date": date,
                "summary": summary,
                "comments": comments,
                "comment_count": comment_count,
                "audio": audio,
                "podcast": audio is not None,
                "video": video.group(1) if video else None,  # A YouTube video's id, for playing it here.
            })

    return {
        "name": site["name"] or clean(child_text(meta, "title")) or site["url"],
        "url": site["url"],
        "feed_url": feed_url,
        "pocketcasts": site["pocketcasts"],
        "posts": posts[: site["limit"]],
    }


# A YouTube channel's feed, by the channel's id, and the YouTube Data API's list of a playlist's videos.
YOUTUBE_CHANNEL_FEED = re.compile(r"youtube\.com/feeds/videos\.xml\?channel_id=UC([\w-]{22})")
YOUTUBE_PLAYLIST_API = "https://www.googleapis.com/youtube/v3/playlistItems"


def read_youtube_api(site, channel, key):
    """A YouTube channel's latest videos from the YouTube Data API, for when its feed won't load: YouTube's
    feeds answer 404 now and then, for a while, for a channel that's fine. The channel's UULF playlist is its
    uploads without Shorts or live streams, as its feed is once Shorts are left out. One request costs one
    unit of the key's 10,000 a day."""
    query = urlencode({"part": "snippet,contentDetails", "playlistId": f"UULF{channel}", "maxResults": 15, "key": key})
    items = json.loads(fetch(f"{YOUTUBE_PLAYLIST_API}?{query}")[1]).get("items", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=site["days"])
    posts = []
    for item in items:
        snippet, details = item.get("snippet", {}), item.get("contentDetails", {})
        video = details.get("videoId") or snippet.get("resourceId", {}).get("videoId")
        date = parse_date(details.get("videoPublishedAt") or snippet.get("publishedAt"))
        title = clean(snippet.get("title", ""))
        # A video made private or deleted since stays in the playlist, under a stand-in title.
        if not video or title in ("Private video", "Deleted video") or (date and date < cutoff):
            continue
        posts.append({
            "title": title,
            "link": f"https://www.youtube.com/watch?v={video}",
            "date": date,
            "summary": excerpt(html.escape(snippet.get("description", "")).replace("\n", "<br>")),
            "comments": None,
            "comment_count": None,
            "audio": None,
            "podcast": False,
            "video": video,
        })
    name = site["name"] or (items[0]["snippet"].get("channelTitle", "") if items else "") or site["url"]
    return {"name": name, "url": site["url"], "feed_url": site["url"], "pocketcasts": site["pocketcasts"],
            "posts": posts[: site["limit"]]}


def load(site):
    try:
        return read_feed(site)
    except Exception as error:
        # A YouTube channel whose feed won't load comes from the YouTube Data API instead, given a key (the
        # YOUTUBE_KEY secret). The key is kept out of any error, which the page publishes.
        channel, key = YOUTUBE_CHANNEL_FEED.search(site["url"]), os.environ.get("YOUTUBE_KEY", "").strip()
        if channel and key:
            try:
                feed = read_youtube_api(site, channel.group(1), key)
                print(f"  {feed['name']}: its feed failed ({error}), so its videos came from the YouTube API", file=sys.stderr)
                return feed
            except Exception as api_error:
                error = f"{error}; the YouTube API: {str(api_error).replace(key, '…')}"
        return {"name": site["name"] or site["url"], "url": site["url"], "error": str(error), "posts": []}


def pocket_casts_json(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as response:
        body = response.read()
    # Episode lists are stored gzipped and served that way whatever the request accepts.
    return json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)


def comparable(text):
    return re.sub(r"\W+", " ", html.unescape(text or "")).strip().lower()


def pocket_casts_links(feed):
    """Pocket Casts links for a podcast's episodes, keyed by audio file and by title, and the show's own page."""
    uuid = feed["pocketcasts"]
    if not uuid:
        # Named "add feed", but for a feed Pocket Casts already has it just returns the show.
        found = pocket_casts_json(POCKET_CASTS_FIND_URL, {"url": feed["feed_url"]})
        uuid = ((found.get("result") or {}).get("podcast") or {}).get("uuid")
    if not uuid:
        return {}, None
    links = {}
    for episode in pocket_casts_json(POCKET_CASTS_EPISODES_URL.format(uuid=uuid))["podcast"]["episodes"]:
        url = f"https://pca.st/episode/{episode['uuid']}"
        links[episode.get("url")] = url
        links[comparable(episode.get("title"))] = url
    return links, f"https://pca.st/podcast/{uuid}"


def link_to_pocket_casts(feed):
    """Point podcast episodes at Pocket Casts, whose pca.st links open in the Pocket Casts app."""
    episodes = [post for post in feed["posts"] if post["podcast"]]
    # Only whole podcast feeds: the lookup adds feeds Pocket Casts doesn't have yet, and a newsletter
    # feed with the odd episode in it shouldn't become a podcast there. Those episodes keep web links.
    if len(episodes) < len(feed["posts"]):
        return
    try:
        links, show_url = pocket_casts_links(feed)
    except Exception as error:
        print(f"  {feed['name']}: Pocket Casts lookup failed, keeping web links ({error})", file=sys.stderr)
        return
    linked = 0
    for post in episodes:
        episode_url = links.get(post["audio"]) or links.get(comparable(post["title"]))
        linked += bool(episode_url)
        # An episode Pocket Casts hasn't picked up yet opens the show, where it will appear.
        post["link"] = episode_url or show_url or post["link"]
    print(f"  {feed['name']}: {linked} of {len(episodes)} episodes linked to Pocket Casts")


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
                    "podcast": post["podcast"],
                    "video": post.get("video"),
                }
                for post in posts
            ],
        },
        ensure_ascii=False,
        indent=1,
    )


# Each post's mark, before its source: a page of text for an article, headphones for a podcast episode, a
# screen with a play button for a video.
# Drawn once in the page; rows point to the drawing. Unread, it takes its kind's color; read, it goes gray.
ICON_DRAWINGS = {
    "article": '<rect x="3" y="1.75" width="10" height="12.5" rx="1.5"/><path d="M5.75 5.5h4.5M5.75 8h4.5M5.75 10.5h2.75"/>',
    "podcast": '<path d="M2.75 10.5V8a5.25 5.25 0 0 1 10.5 0v2.5"/>'
               '<rect x="1.5" y="9.5" width="3.25" height="5" rx="1.25" fill="currentColor" stroke="none"/>'
               '<rect x="11.25" y="9.5" width="3.25" height="5" rx="1.25" fill="currentColor" stroke="none"/>',
    "video": '<rect x="1.5" y="2.75" width="13" height="10.5" rx="2"/><path d="M6.5 5.75v4.5L10.25 8z" fill="currentColor"/>',
}
ICON_SYMBOLS = shared.icon_symbols(ICON_DRAWINGS)


def icon(kind, decorative=False):
    """A post's mark. Beside a label that already says it (the filter), it's hidden from screen readers."""
    return shared.icon(kind, None if decorative else kind.capitalize())


def render_index(feeds, posts, failed, stale, built_at):
    items = []
    for post in posts:
        when = render_time(post["date"]) if post["date"] else ""
        kind = "podcast" if post["podcast"] else "video" if post.get("video") else "article"
        mark = icon(kind)
        marked = " data-podcast" if post["podcast"] else f' data-video="{post["video"]}"' if post.get("video") else ""
        preview = shared.preview(post["title"], post["summary"])
        comments = ""
        if post["comments"]:
            count = post["comment_count"]
            label = "comments" if count is None else "1 comment" if count == 1 else f"{count} comments"
            comments = f'<a class="comments" href="{html.escape(post["comments"])}">{label}</a>'
        items.append(
            f'<li class="row"{marked}><span class="source">{mark}<span>{html.escape(post["source"])}</span></span>'
            f'<div class="headline"><a class="title" href="{html.escape(post["link"])}">{html.escape(post["title"])}</a>'
            f"{when}{comments}{preview}</div></li>"
        )

    # Feeds that failed; a feed that just hasn't posted lately isn't one.
    failed_note = f"<p>Couldn’t load {html.escape(', '.join(failed))}.</p>\n" if failed else ""
    failed_note += "".join(
        f'<p>Couldn’t reach {html.escape(name)}; its posts are from {render_time(fetched, "updated")}.</p>\n'
        for name, fetched in stale
    )

    # Only worth offering when there's something to choose between, and only the kinds there are.
    kinds = [("podcasts", "podcast", "Podcasts", any(post["podcast"] for post in posts)),
             ("videos", "video", "Videos", any(post.get("video") for post in posts))]
    show_filter = (
        '<nav class="filter" aria-label="Show"><button data-show="all">All</button>'
        f'<button data-show="articles">{icon("article", decorative=True)}Articles</button>'
        + "".join(f'<button data-show="{key}">{icon(mark, decorative=True)}{label}</button>' for key, mark, label, there in kinds if there)
        + shared.SEARCH + "</nav>\n"
        if any(there for *_, there in kinds)
        else ""
    )

    body = (
        show_filter + '<button class="new-posts" hidden></button>\n'
        + PLAYER +
        f'<ul class="posts" data-page-size="{PAGE_SIZE}">\n' + "\n".join(items) + "\n</ul>\n"
        '<p class="empty" hidden></p>\n'
        '<nav class="pager"></nav>\n'
        f"<footer>\n{failed_note}"
        '<p><a href="sources.html">Add or remove sites</a></p>\n'
        "</footer>\n"
        f"<script>{INDEX_JS}</script>"
    )
    return page("Newsfeed", body, updated=built_at)


# Where a video plays: over the list, with a way to close it, and nothing else. (The player shows its name.)
PLAYER = """<dialog class="player" aria-label="Video">
<button class="player-close" aria-label="Close"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9"/></svg></button>
<div class="player-frame"></div>
<p class="player-note" hidden>This video can’t be played here. <a href="">Watch it on YouTube</a></p>
</dialog>
"""

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

  // Videos play here, in a plain window over the list: no comments, no suggestions, no next video. It's
  // YouTube's own player, its privacy-enhanced version, loaded the first time a video is opened; when the
  // video ends the window closes, before YouTube can offer another. A click with a modifier key still opens
  // YouTube, in a new tab.
  const dialog = document.querySelector(".player");
  const note = dialog.querySelector(".player-note");
  let player = null, youtube = null;
  const loadYouTube = () => youtube ??= new Promise(resolve => {
    window.onYouTubeIframeAPIReady = resolve;
    document.head.append(Object.assign(document.createElement("script"), { src: "https://www.youtube.com/iframe_api" }));
  });
  async function play(a) {
    dialog.setAttribute("aria-label", a.textContent);
    note.querySelector("a").href = a.href;
    note.hidden = true;
    const holder = document.createElement("div");
    dialog.querySelector(".player-frame").replaceChildren(holder);
    dialog.showModal();
    await loadYouTube();
    if (!dialog.open) return; // Closed while the player loaded.
    let started = false;
    player = new YT.Player(holder, {
      host: "https://www.youtube-nocookie.com",
      videoId: a.closest("li").dataset.video,
      width: "100%",
      height: "100%",
      // Related videos from the same channel only, no annotations, and inline on phones until made full screen.
      playerVars: { autoplay: 1, rel: 0, iv_load_policy: 3, playsinline: 1 },
      events: {
        onStateChange: event => {
          // The player takes the keyboard when it starts; give it back once, so Esc closes the window.
          if (event.data === YT.PlayerState.PLAYING && !started) {
            started = true;
            dialog.querySelector(".player-close").focus();
          }
          if (event.data === YT.PlayerState.ENDED) dialog.close();
        },
        onError: () => { note.hidden = false; }, // Its channel doesn't allow it to play elsewhere, most often.
      },
    });
  }
  dialog.addEventListener("close", () => {
    if (player) player.destroy();
    player = null;
    dialog.querySelector(".player-frame").replaceChildren();
  });
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); }); // Outside the video.
  dialog.querySelector(".player-close").addEventListener("click", () => dialog.close());
  // Esc, which the dialog closes on by itself in most browsers. Once the video has been clicked, keys go to
  // YouTube's player instead, and the × or a click outside the video closes it.
  document.addEventListener("keydown", event => { if (event.key === "Escape" && dialog.open) dialog.close(); });
  for (const a of links) {
    if (!a.closest("li").dataset.video) continue;
    a.addEventListener("click", event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      play(a);
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
  const pager = document.querySelector(".pager");
  const empty = document.querySelector(".empty");

  // The filter shows every post, or only articles, podcast episodes or videos. The choice is remembered in
  // this browser, and paging counts only the posts it shows. The search keeps the posts with every word typed
  // somewhere in their headline, source or preview (accents aside), and is kept in the address (?q=); it's
  // hidden on phones, and ignored there.
  const filter = document.querySelector(".filter");
  const search = document.querySelector(".search");
  let show = "all";
  try { show = (filter && localStorage.getItem("reader-show")) || "all"; } catch (error) {}
  const plain = text => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const searchable = new Map(items.map(li => [li, plain(li.textContent)]));
  const query = () => (search && search.offsetParent ? search.value.trim() : "");
  if (search) search.value = new URLSearchParams(location.search).get("q") || "";
  const address = page => {
    const params = new URLSearchParams();
    if (query()) params.set("q", query());
    if (page > 1) params.set("page", page);
    return params.toString() ? "?" + params : location.pathname;
  };

  const kind = li => li.hasAttribute("data-podcast") ? "podcasts" : li.dataset.video ? "videos" : "articles";

  function showPosts() {
    const words = plain(query()).split(/\\s+/).filter(Boolean);
    const shown = items.filter(li => (show === "all" || kind(li) === show) && words.every(word => searchable.get(li).includes(word)));
    const pages = Math.max(1, Math.ceil(shown.length / pageSize));
    const page = Math.min(pages, Math.max(1, parseInt(new URLSearchParams(location.search).get("page")) || 1));
    items.forEach(li => { li.hidden = true; });
    shown.forEach((li, i) => { li.hidden = i < (page - 1) * pageSize || i >= page * pageSize; });
    list.classList.add("paged");
    const link = (n, text) => `<a href="${address(n)}">${text}</a>`;
    pager.innerHTML = pages < 2 ? "" :
      (page > 1 ? link(page - 1, "← Newer") : "<span></span>") +
      `<span>Page ${page} of ${pages}</span>` +
      (page < pages ? link(page + 1, "Older →") : "<span></span>");
    empty.textContent = words.length ? `Nothing here matches “${query()}”.`
      : { podcasts: "No podcast episodes right now.", videos: "No videos right now." }[show] || "No articles right now.";
    empty.hidden = shown.length > 0;
    if (filter) for (const b of filter.querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.show === show);
  }
  showPosts();

  if (filter) filter.addEventListener("click", event => {
    const b = event.target.closest("button");
    if (!b) return;
    show = b.dataset.show;
    try { localStorage.setItem("reader-show", show); } catch (error) {}
    history.replaceState(null, "", address(1)); // Back to page one.
    showPosts();
  });
  if (search) search.addEventListener("input", () => {
    history.replaceState(null, "", address(1));
    showPosts();
  });
"""


ISSUE_NOTE = (
    "Press Create to make this change. Within a minute a bot updates feeds.txt, replies here, "
    "and closes this issue."
)


def render_sources(feeds, built_at):
    rows = []
    for feed in feeds:
        count = len(feed["posts"])
        status = (
            f"{count} posts, from earlier" if feed.get("restored")
            else "couldn’t load" if "error" in feed
            else f"{count} posts" if count
            else "no recent posts"
        )
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
<a href="{REPO_URL}/edit/main/news/feeds.txt">feeds.txt on GitHub</a>. If a site doesn’t show up, the
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


HEAD = """<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="manifest.webmanifest">"""

# The newsfeed's own styles, on top of the ones it shares with the events page (shared/site.py).
CSS = """
  /* Unread marks, and the filter's matching marks: green for articles, violet for podcast episodes, coral for
     videos. */
  :root { --article: #34d399; --podcast: #a78bfa; --video: #fb7185; }
  h1 { margin: 3rem 0 .75rem; color: #777; font-size: .75rem; font-weight: 600;
       letter-spacing: .08em; text-transform: uppercase; }
  h1:first-child, header + h1 { margin-top: 0; }
  .filter .icon { color: var(--article); }
  .filter [data-show="podcasts"] .icon { color: var(--podcast); }
  .filter [data-show="videos"] .icon { color: var(--video); }
  li { padding: .4rem 0; }
  ol { padding-left: 1.25rem; }
  ol li { padding: .3rem 0; }
  /* Until the script picks the page, show the first one, so the whole list never flashes up. */
  .posts:not(.paged) li:nth-child(n+PAGE_START) { display: none; }
  .note, time { color: #666; font-size: .8em; }
  .headline time, .headline .comments { flex: none; }
  /* Each post's icon: in its kind's color while the post is unread, gray once it's read. */
  .source .icon { color: #555; }
  .unread .source .icon { color: var(--article); }
  .row[data-podcast].unread .source .icon { color: var(--podcast); }
  .row[data-video].unread .source .icon { color: var(--video); }
  /* The video player: the video at the width of the window, up to a large laptop's, alone on black. */
  .player { width: min(64rem, 100vw - 2.5rem); max-width: none; max-height: none; padding: 0; border: 0;
            background: none; color: #fff; overflow: visible; }
  .player::backdrop { background: #000; }
  /* Closing it: a thin × in the window's corner, in a circle that lights up faintly on hover. */
  .player-close { position: fixed; top: 1rem; right: 1rem; display: grid; place-items: center; width: 2.25rem;
                  height: 2.25rem; padding: 0; border: 0; border-radius: 50%; background: none; color: #777; cursor: pointer;
                  transition: background-color .15s, color .15s; }
  .player-close svg { width: 1rem; height: 1rem; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; }
  .player-close:hover, .player-close:focus-visible { background: #1a1a1a; color: #fff; }
  .player-close:focus { outline: none; }
  .player-frame { aspect-ratio: 16 / 9; background: #111; }
  .player-frame iframe { display: block; width: 100%; height: 100%; border: 0; }
  .player-note { margin: .75rem 0 0; color: #999; font-size: .85rem; }
  .player-note a { color: #fff; text-decoration: underline; text-underline-offset: .2em; }
  @media (max-width: 34rem) { .player { width: 100vw; } }
  .new-posts { position: fixed; z-index: 2; top: calc(env(safe-area-inset-top) + .75rem); left: 50%;
               transform: translateX(-50%); padding: .45rem 1.1rem; border: 0; border-radius: 999px;
               background: #fff; color: #000; font-family: inherit; font-size: .8rem; font-weight: 600; cursor: pointer; }
  a:visited { color: #666; }
  .comments, .comments:visited { margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }
  p a, ol a { text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  time, .note { margin-left: .6em; white-space: nowrap; }
  footer time { margin: 0; font-size: inherit; }
  /* The sources page. */
  .remove, .remove:visited { margin-left: .8em; color: #666; font-size: .8em; }
  .sources { columns: 2; column-gap: 2.5rem; }
  .sources li { break-inside: avoid; }
  @media (max-width: 34rem) { .sources { columns: 1; } }
  .add-site { display: flex; flex-wrap: wrap; gap: .5rem; }
  .add-site input { flex: 1 1 12rem; min-width: 0; padding: .55rem .75rem; border: 1px solid #333; border-radius: 6px;
                    background: #000; color: #fff; font: inherit; font-size: .9rem; }
  .add-site input::placeholder { color: #555; }
  .add-site input:focus { outline: none; border-color: #888; }
  .add-site button { padding: .55rem 1.3rem; border: 0; border-radius: 999px; background: #fff; color: #000;
                     font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer; }
  .help { color: #777; font-size: .85em; }
  code { color: #ccc; font-size: .9em; }
""".replace("PAGE_START", str(PAGE_SIZE + 1))


def page(title, body, updated=None):
    return shared.page("news", title, body, css=CSS, head=HEAD, symbols=ICON_SYMBOLS, updated=updated)


def saved(post):
    """A post as feeds.json keeps it."""
    return dict(post, date=post["date"].isoformat() if post["date"] else None)


def restored(kept):
    return dict(kept, date=datetime.fromisoformat(kept["date"]) if kept["date"] else None)


def previous_build():
    """The live page's feeds.json: each feed's posts from the last build, and which feeds were failing."""
    return shared.previous_build(FEEDS_URL, USER_AGENT)


def gather(sites, feeds, previous, built_at):
    """Each feed as it loaded, or, when it failed, its last good posts from the previous build if they're
    recent enough. Returns the feeds; the ones that failed with nothing to fall back on; the ones shown from
    before, with when; and why each failing one failed. Saved copies are keyed by the feed's feeds.txt URL,
    which stays the same when the feed is down."""
    kept_feeds = previous.get("feeds", {})
    gathered, failed, stale, errors = [], [], [], {}
    for site, feed in zip(sites, feeds):
        cutoff = built_at - timedelta(days=site["days"])
        kept = kept_feeds.get(site["url"])
        error = feed.get("error")
        recent = [post for post in (kept or {}).get("posts", []) if not post["date"] or datetime.fromisoformat(post["date"]) >= cutoff]
        # A feed that had posts in its window last time and has none now is more likely broken for the
        # moment (served empty) than suddenly quiet, so it gets the same fallback.
        if not error and not feed["posts"] and recent:
            error = "returned no posts"
        if not error:
            feed["fetched"] = built_at
            gathered.append(feed)
            continue
        name = (kept or {}).get("name") or feed["name"]
        errors[name] = str(error)
        print(f"✗ {name}: {error}", file=sys.stderr)
        if not kept or built_at - datetime.fromisoformat(kept["fetched"]) > FALLBACK_LIMIT:
            failed.append(name)
            gathered.append(feed)
            continue
        # Keep its last good posts, and when they were fetched, so they still age out. Their links are
        # already resolved (to Pocket Casts, for episodes), so the feed isn't looked up again.
        fetched = datetime.fromisoformat(kept["fetched"])
        stale.append((name, fetched))
        print(f"  {name}: showing its posts from {kept['fetched']} instead", file=sys.stderr)
        gathered.append(dict(feed, name=name, posts=[restored(post) for post in recent][: site["limit"]],
                             fetched=fetched, restored=True))
    return gathered, failed, stale, errors


still_failing = shared.still_failing


def main():
    sites = read_sites()
    with ThreadPoolExecutor(max_workers=8) as pool:
        feeds = list(pool.map(load, sites))

    missing = [post for feed in feeds for post in feed["posts"] if not post["summary"]]
    with ThreadPoolExecutor(max_workers=16) as pool:
        for post, summary in zip(missing, pool.map(lambda post: page_summary(post["link"]), missing)):
            post["summary"] = summary

    for feed in feeds:
        if "error" not in feed:
            previews = sum(1 for post in feed["posts"] if post["summary"])
            print(f"✓ {feed['name']}: {len(feed['posts'])} posts, {previews} with previews, from {feed['feed_url']}")

    built_at = datetime.now(timezone.utc)
    previous = previous_build()
    feeds, failed, stale, errors = gather(sites, feeds, previous, built_at)
    if len(failed) + len(stale) == len(sites) or not any(feed["posts"] for feed in feeds):
        sys.exit("No feeds loaded — not writing the page.")

    # One podcast at a time, to go easy on an API that isn't meant for public use.
    for feed in feeds:
        if not feed.get("restored") and any(post["podcast"] for post in feed["posts"]):
            link_to_pocket_casts(feed)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    posts = all_posts(feeds)
    (OUT_DIR / "index.html").write_text(render_index(feeds, posts, failed, stale, built_at))
    (OUT_DIR / "posts.json").write_text(render_json(posts, built_at))
    (OUT_DIR / "sources.html").write_text(render_sources(feeds, built_at))
    (OUT_DIR / "feeds.opml").write_text(render_opml(feeds, built_at))
    record = {
        "built": built_at.isoformat(),
        "feeds": {
            feed["url"]: {"name": feed["name"], "fetched": feed["fetched"].isoformat(), "posts": [saved(post) for post in feed["posts"]]}
            for feed in feeds if "fetched" in feed
        },
        "failing": still_failing(errors, previous, built_at),
    }
    (OUT_DIR / "feeds.json").write_text(json.dumps(record, ensure_ascii=False))
    print(f"Wrote {OUT_DIR.relative_to(ROOT.parent)}/index.html, posts.json, sources.html, feeds.opml and feeds.json")


if __name__ == "__main__":
    main()
