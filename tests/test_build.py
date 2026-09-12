"""Tests for build.py, against saved samples of real feeds (tests/fixtures), so they run offline and a change
that breaks the feed reader fails here instead of on the live page. Run with:
python3 -m unittest discover -s tests

The samples are real feeds trimmed to a few posts. When a feed changes and the reader is updated for it,
save a fresh sample alongside the fix.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# Which sample stands in for which address. The homepage names its feed, for feed discovery.
ROUTES = {
    "https://www.nytimes.com/": "nyt.xml",
    "https://kottke.org/": "kottke.xml",
    "https://hnrss.org/frontpage?points=150": "hackernews.xml",
    "https://feeds.megaphone.fm/the-big-picture": "bigpicture.xml",
    "https://www.slowboring.com/": "slowboring.xml",
    "https://waxy.org/": "waxy_home.html",
    "https://waxy.org/feed/": "waxy.xml",
}


def sample(url, **kwargs):
    if url not in ROUTES:
        raise OSError(f"no sample for {url}")  # Like a site with no feed at that address.
    return url, (FIXTURES / ROUTES[url]).read_bytes()


def site(url, days=36500, **options):
    """A feeds.txt line. Days reach far back, so the samples' posts don't age out of the tests."""
    return dict(build.parse_site(url), days=days, **options)


class Reading(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(build, "fetch", sample)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, url):
        feed = build.read_feed(site(url))
        self.assertTrue(feed["posts"], f"no posts read from the sample for {url}")
        for post in feed["posts"]:
            self.assertTrue(post["title"].strip())
            self.assertTrue(post["link"].startswith("http"), post["link"])
            self.assertTrue(post["date"] is None or post["date"].tzinfo is not None)
            self.assertIsInstance(post["summary"], list)
        return feed

    def test_rss(self):
        feed = self.read("https://www.nytimes.com/")
        self.assertTrue(feed["name"])
        self.assertFalse(any(post["podcast"] for post in feed["posts"]))

    def test_atom(self):
        self.read("https://kottke.org/")

    def test_hacker_news_comments(self):
        feed = self.read("https://hnrss.org/frontpage?points=150")
        self.assertTrue(all("news.ycombinator.com" in post["comments"] for post in feed["posts"]))
        self.assertTrue(all(isinstance(post["comment_count"], int) for post in feed["posts"]))
        self.assertFalse(any(line.startswith(("Points:", "Article URL")) for post in feed["posts"] for line in post["summary"]))

    def test_podcast_episodes_without_web_pages(self):
        feed = self.read("https://feeds.megaphone.fm/the-big-picture")
        self.assertTrue(all(post["podcast"] and post["audio"] for post in feed["posts"]))
        self.assertTrue(any(post["link"] == post["audio"] for post in feed["posts"]),
                        "an episode with no page of its own links to its audio")

    def test_newsletter_previews(self):
        feed = self.read("https://www.slowboring.com/")
        self.assertTrue(all(post["summary"] for post in feed["posts"]))

    def test_finds_a_homepages_feed(self):
        feed = self.read("https://waxy.org/")
        self.assertEqual(feed["feed_url"], "https://waxy.org/feed/")

    def test_a_site_without_a_feed_is_an_error(self):
        feed = build.load(site("https://example.com/"))
        self.assertIn("error", feed)
        self.assertEqual(feed["posts"], [])

    def test_limit_and_days(self):
        self.assertEqual(len(build.read_feed(site("https://www.nytimes.com/", limit=2))["posts"]), 2)
        self.assertEqual(build.read_feed(site("https://www.nytimes.com/", days=0))["posts"], [])


class PocketCasts(unittest.TestCase):
    def pocket_casts(self, url, payload=None):
        return json.loads((FIXTURES / ("pocketcasts_find.json" if payload else "pocketcasts_episodes.json")).read_text())

    def test_links_episodes_to_pocket_casts(self):
        with mock.patch.object(build, "fetch", sample):
            feed = build.read_feed(site("https://feeds.megaphone.fm/the-big-picture"))
        episodes = {episode["title"] for episode in self.pocket_casts("x")["podcast"]["episodes"]}
        with mock.patch.object(build, "pocket_casts_json", self.pocket_casts):
            build.link_to_pocket_casts(feed)
        for post in feed["posts"]:
            expected = "https://pca.st/episode/" if post["title"] in episodes else "https://pca.st/podcast/"
            self.assertTrue(post["link"].startswith(expected), (post["title"], post["link"]))

    def test_leaves_a_newsletter_with_episodes_alone(self):
        feed = {"name": "Mixed", "feed_url": "x", "pocketcasts": "", "posts": [
            {"title": "Episode", "link": "https://a", "audio": "https://a.mp3", "podcast": True},
            {"title": "Essay", "link": "https://b", "audio": None, "podcast": False},
        ]}
        with mock.patch.object(build, "pocket_casts_json", side_effect=AssertionError("looked up")):
            build.link_to_pocket_casts(feed)
        self.assertEqual(feed["posts"][0]["link"], "https://a")


class Fallback(unittest.TestCase):
    built = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    blog = site("https://blog.example/", days=3, name="A Blog")

    def post(self, hours_ago, title="Post"):
        return {"title": title, "link": f"https://blog.example/{title}", "date": self.built - timedelta(hours=hours_ago),
                "summary": [], "comments": None, "comment_count": None, "audio": None, "podcast": False}

    def feed(self, posts=(), error=None):
        feed = {"name": "A Blog", "url": self.blog["url"], "feed_url": "https://blog.example/feed", "pocketcasts": "", "posts": list(posts)}
        return dict(feed, error=error) if error else feed

    def previous(self, hours_old, post_hours_ago=10):
        kept = build.saved(dict(self.post(post_hours_ago, "Kept")))
        return {"feeds": {self.blog["url"]: {"name": "A Blog", "fetched": (self.built - timedelta(hours=hours_old)).isoformat(), "posts": [kept]}}}

    def gather(self, feed, previous):
        return build.gather([self.blog], [feed], previous, self.built)

    def test_fresh(self):
        feeds, failed, stale, errors = self.gather(self.feed([self.post(1)]), self.previous(1))
        self.assertEqual(([p["title"] for p in feeds[0]["posts"]], failed, stale, errors), (["Post"], [], [], {}))
        self.assertEqual(feeds[0]["fetched"], self.built)

    def test_error_uses_recent_posts(self):
        feeds, failed, stale, errors = self.gather(self.feed(error="HTTP Error 520"), self.previous(6))
        self.assertEqual([p["title"] for p in feeds[0]["posts"]], ["Kept"])
        self.assertTrue(feeds[0]["restored"])
        self.assertEqual(feeds[0]["fetched"], self.built - timedelta(hours=6), "the copy keeps its age")
        self.assertEqual([name for name, _ in stale], ["A Blog"])
        self.assertEqual(errors, {"A Blog": "HTTP Error 520"})

    def test_error_with_old_copy_fails(self):
        feeds, failed, stale, _ = self.gather(self.feed(error="timed out"), self.previous(72))
        self.assertEqual((feeds[0]["posts"], failed, stale), ([], ["A Blog"], []))

    def test_empty_after_having_posts_uses_them(self):
        feeds, failed, stale, errors = self.gather(self.feed(), self.previous(1))
        self.assertEqual([p["title"] for p in feeds[0]["posts"]], ["Kept"])
        self.assertEqual(errors["A Blog"], "returned no posts")

    def test_quiet_blog_is_not_a_failure(self):
        # Its last post has aged out of its three-day window, so nothing is missing.
        feeds, failed, stale, errors = self.gather(self.feed(), self.previous(1, post_hours_ago=100))
        self.assertEqual((feeds[0]["posts"], failed, stale, errors), ([], [], [], {}))

    def test_failing_since_carries_over(self):
        earlier = {"failing": {"A Blog": {"since": "2026-09-20T01:00:00+00:00", "error": "520"}}}
        failing = build.still_failing({"A Blog": "timed out"}, earlier, self.built)
        self.assertEqual(failing["A Blog"], {"since": "2026-09-20T01:00:00+00:00", "error": "timed out"})

    def test_saved_posts_survive_the_round_trip(self):
        post = self.post(2)
        self.assertEqual(build.restored(json.loads(json.dumps(build.saved(post)))), post)


class Page(unittest.TestCase):
    def test_renders_posts_filter_and_failures(self):
        with mock.patch.object(build, "fetch", sample):
            feeds = [build.read_feed(site(url)) for url in ("https://www.nytimes.com/", "https://feeds.megaphone.fm/the-big-picture")]
        posts = build.all_posts(feeds)
        page = build.render_index(feeds, posts, ["Broken Blog"], [("Slow Site", datetime.now(timezone.utc))], datetime.now(timezone.utc))
        self.assertEqual(page.count('<span class="source">'), len(posts))
        self.assertEqual(page.count("<li data-podcast>"), sum(post["podcast"] for post in posts))
        self.assertIn('data-show="podcasts"', page)
        self.assertIn("Couldn’t load Broken Blog", page)
        self.assertIn("Couldn’t reach Slow Site", page)


class Alerts(unittest.TestCase):
    def test_opens_for_outages_and_closes_on_recovery(self):
        import alerts

        now = datetime.now(timezone.utc)
        failing = {
            "Kottke": {"since": (now - timedelta(hours=7)).isoformat(), "error": "HTTP Error 520"},
            "Waxy.org": {"since": (now - timedelta(hours=1)).isoformat(), "error": "timed out"},
        }
        calls = []

        def gh(*args):
            calls.append(args)
            return json.dumps([{"number": 3, "title": "Source failing: Volts"}]) if args[:2] == ("issue", "list") else ""

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as record:
            json.dump({"failing": failing}, record)
        with mock.patch.object(alerts, "gh", gh), mock.patch.object(sys, "argv", ["alerts.py", record.name]):
            alerts.main()
        os.unlink(record.name)

        self.assertEqual([args[3] for args in calls if args[:2] == ("issue", "create")], ["Source failing: Kottke"])
        self.assertEqual([args[2] for args in calls if args[:2] == ("issue", "close")], ["3"])


if __name__ == "__main__":
    unittest.main()
