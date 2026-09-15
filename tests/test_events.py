"""Tests for build.py, against saved samples of each source (tests/fixtures), so they run offline and a change
that breaks a reader fails here instead of on the live page. Run from the repo's top folder with: python3 -m unittest

The samples are real responses, trimmed to a few items, except ticketmaster.json: the Discovery API needs a
key, so that one follows its documented format. When a site changes and its reader is updated, save a fresh
sample alongside the fix.
"""

import json
import os
import re
import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from events import build  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "events"
# Which sample stands in for which address.
ROUTES = [
    ("aegwebprod", "roadrunner.json"),
    ("sinclaircambridge.com/events/detail/1460677", "sinclair_show.html"),
    ("sinclaircambridge.com", "sinclair.html"),
    ("artsatthearmory.org/events.ics", "armory.ics"),
    ("lilypadinman.com/home?format=json", "lilypad.json"),
    ("passim.org/live-music", "passim.html"),  # Its other shows' pages: no show time on them, so door times.
    ("mideastclub.com", "mideast.html"),
    ("brattlefilm.org", "brattle.html"),
    ("houseofblues.com", "houseofblues.html"),
    ("coolidge.org/showtimes", "coolidge.html"),
    ("drafthouse.com", "alamo.json"),
    ("boxofficeapi/schedule", "landmark_schedule.json"),
    ("boxofficeapi/movies", "landmark_movies.json"),
    ("app.ticketmaster.com", "ticketmaster.json"),
    ("cambridgema.gov", "cambridge.ics"),
    ("dice.fm", "dice.html"),
    ("tockify.com", "midway.ics"),
    ("lizardloungeclub.com", "lizardlounge.json"),
    ("calendar.mit.edu", "mit.json"),
    ("rss/events?types=test&page=1", "bpl.xml"),
    ("gateway.bibliocommons.com", "bpl_end.xml"),  # Every later page: the feed with nothing left in it.
    ("mfa.org/programs/lectures", "mfa_lectures.html"),
    ("mfa.org/programs/special-event", "mfa_special-event.html"),
    ("mfa.org/programs/film", "mfa_film.html"),
    ("mfa.org/programs/music", "mfa_music.html"),
    ("harvardfilmarchive.org/calendar", "hfa.html"),
    ("frenchlibrary.org", "frenchlibrary.html"),
    ("icaboston.org/calendar", "ica.html"),
    ("icaboston.org/events/colin-stetson", "ica_event.html"),
    ("westnewtoncinema.com/api/movie/playing-now", "veezi_now.json"),
    ("westnewtoncinema.com/api/movie/coming-soon", "veezi_soon.json"),
]


def sample(url, **kwargs):
    for pattern, name in ROUTES:
        if pattern in url:
            return (FIXTURES / name).read_text()
    raise AssertionError(f"no sample for {url}")


def source(kind, url, category="music", name="Somewhere"):
    return {"kind": kind, "url": url, "category": category, "name": name}


class Readers(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(build, "fetch", sample)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, kind, url, category="music", name="Somewhere"):
        found, error = build.READERS[kind](source(kind, url, category, name)), None
        self.assertTrue(found, f"{kind} read nothing from its sample")
        for item in found:
            self.assertTrue(item["title"].strip())
            self.assertIsInstance(item["date"], date)
            self.assertTrue(all(isinstance(moment, time) for moment in item["times"]))
            self.assertTrue(item["link"].startswith("http"), item["link"])
            self.assertEqual(item["category"], category)
        return found

    def test_aeg(self):
        found = self.read("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json")
        self.assertEqual(len(found), 3)
        self.assertTrue(all(item["times"] for item in found))
        self.assertTrue(any(item["detail"].startswith("with ") for item in found))
        # Its picture about 678 pixels wide, not a thumbnail; its ages; no price, for the $0 it gives.
        self.assertTrue(all(item["image"].startswith("https://images.discovery-prod.axs.com/") for item in found))
        self.assertEqual({item["ages"] for item in found}, {"All ages", "18+"})
        self.assertFalse(any(item["price"] for item in found))

    def test_axs_show_times_without_cancelled_shows(self):
        with mock.patch.object(build, "today", lambda: date(2026, 9, 1)):
            found = self.read("axs", "https://www.sinclaircambridge.com/events/all")
        self.assertEqual([item["title"] for item in found], ["Chanel Beads", "DON WEST", "John Craigie", "The Glitter Boys"])
        first = found[0]
        # Its own page's show time, not the listing's door time, which its preview gives.
        self.assertEqual((first["date"], first["times"]), (date(2026, 9, 13), [time(20, 0)]))
        self.assertEqual(first["about"], ["Doors 7pm · All Ages"])
        self.assertEqual(first["ages"], "All ages")
        self.assertTrue(first["image"].startswith("https://images.discovery-prod.axs.com/"))
        self.assertEqual(first["detail"], "with Horse Vision, Ivy Knight")
        self.assertEqual(first["link"], "https://www.sinclaircambridge.com/events/detail/1460677")
        self.assertEqual(found[1]["times"], [time(19, 30)], "a page without a show time leaves the door time")
        self.assertEqual(found[2]["about"][0].split(" · ")[0], "Fall 2026", "the tour, first")
        self.assertEqual(found[3]["detail"], "", "no supporting acts")

    def test_axs_reads_show_pages_only_for_shows_in_the_window(self):
        asked = []
        with mock.patch.object(build, "fetch", lambda url, **options: asked.append(url) or sample(url)), \
                mock.patch.object(build, "today", lambda: date(2026, 6, 1)):  # Its sample's shows are all past the window then.
            build.read_axs(source("axs", "https://www.sinclaircambridge.com/events/all", "music", "The Sinclair"))
        self.assertEqual(asked, ["https://www.sinclaircambridge.com/events/all"])

    def test_armory_by_kind(self):
        found = build.read_armory(source("armory", "https://artsatthearmory.org/events.ics", "music", "Arts at the Armory"))
        self.assertEqual({item["title"]: item["category"] for item in found}, {
            "The Bowery Presents ear with Oral": "music",
            "Lovestruck Books Presents: Grim Tidings Release-Day Bash with B.K. Borison": "art",
            '"A Cell Phone Movie" Screening': "film",
            "Get to the Gig Presents Armand Hammer with Curly Castro": "music",
        })  # Not its comedy, or the show it moved to the Royale.
        self.assertEqual([item["title"] for item in found if item["sold_out"]], ["The Bowery Presents ear with Oral"], "SOLD OUT: out of its name")
        reading = next(item for item in found if item["category"] == "art")
        self.assertEqual((reading["date"], reading["times"]), (date(2026, 9, 15), [time(18, 30)]))
        self.assertFalse(any("$(" in line or "fbq(" in line for item in found for line in item["about"]), "not its ticket button's script")
        self.assertTrue(any("&" in line for item in found for line in item["about"]))
        self.assertFalse(any("&amp;" in line for item in found for line in item["about"]))

    def test_squarespace_shows_not_yoga_or_private_events(self):
        found = self.read("squarespace", "https://www.lilypadinman.com/home")
        self.assertEqual([item["title"] for item in found], ["2nd Annual Bach & Beer with Aaron Larget-Caplan", "Elan Mehler Trio"])
        first = found[0]
        self.assertEqual((first["date"], first["times"]), (date(2026, 9, 13), [time(13, 30)]))
        self.assertEqual(first["link"], "https://www.lilypadinman.com/home/2026/bach-and-beer-with-aaron-larget-caplan")
        self.assertTrue(first["about"][0].startswith("$25 admission"))
        self.assertFalse(any("--sqs" in line or "{" in line for item in found for line in item["about"]), "not its styles")

    def test_passim_club_shows_once_each(self):
        found = self.read("passim", "https://www.passim.org/live-music/")
        # Not the shows it presents elsewhere, or the cancelled one; The Clements Brothers only once.
        self.assertEqual([(item["date"], item["title"], item["times"]) for item in found], [
            (date(2026, 9, 16), "Ágora Cultural Architects present: Richard Peña Trío", [time(20, 0)]),
            (date(2026, 9, 27), "The Clements Brothers", [time(19, 0)]),
            (date(2026, 11, 22), "Bruce Molsky", [time(14, 0)]),
        ])
        self.assertTrue(found[0]["link"].startswith("https://www.passim.org/live-music/events/"))

    def test_ticketweb_venue_without_room(self):
        found = self.read("ticketweb", "https://mideastclub.com/")
        self.assertEqual(len(found), 3)
        self.assertTrue(all(item["times"] for item in found))
        self.assertFalse(any(" - " in item["venue"] or " – " in item["venue"] for item in found))

    def test_jsonld_brattle_titles_without_showtimes(self):
        found = self.read("jsonld", "https://brattlefilm.org/coming-soon/", "film")
        self.assertFalse(any("@" in item["title"] for item in found))

    def test_jsonld_house_of_blues(self):
        self.read("jsonld", "https://boston.houseofblues.com/shows")

    def test_coolidge_each_day(self):
        found = self.read("coolidge", "https://coolidge.org/showtimes", "film")
        self.assertTrue(all(item["times"] and "coolidge.org/films/" in item["link"] for item in found))

    def test_alamo_skips_past_showings(self):
        found = self.read("alamo", "https://drafthouse.com/s/mother/v2/schedule/market/boston", "film")
        sessions = __import__("json").loads(sample("drafthouse.com"))["data"]["sessions"]
        self.assertEqual(len(found), sum(session["status"] != "PAST" for session in sessions))
        self.assertTrue(all("drafthouse.com/boston/show/" in item["link"] for item in found))

    def test_landmark_titles(self):
        found = self.read("landmark", "X019B", "film")
        titles = {movie["title"] for movie in __import__("json").loads(sample("boxofficeapi/movies"))}
        self.assertTrue({item["title"] for item in found} <= titles)

    def test_ticketmaster_keeps_concerts_only(self):
        with mock.patch.dict(os.environ, {"TICKETMASTER_KEY": "test"}):
            found = self.read("ticketmaster", "KovZ917AJnQ")
        names = {item["title"]: item for item in found}
        self.assertIn("Oteil Burbridge with LaMP - Wish Benefit Tour", names)
        self.assertNotIn("Troy Hawke", names, "Arts & Theatre shows are left out")
        self.assertNotIn("Cancelled Show", names)
        self.assertEqual(names["Late Show, Time To Be Announced"]["times"], [])
        self.assertIn("Untyped Show", names, "a show without a type keeps the venue's category")

    def test_ticketmaster_without_key_is_skipped(self):
        with mock.patch.dict(os.environ, {"TICKETMASTER_KEY": ""}):
            self.assertEqual(build.read_ticketmaster(source("ticketmaster", "X")), [])

    def test_jsonld_dice(self):
        found = self.read("jsonld", "https://dice.fm/venue/deep-cuts-nd6q")
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0]["times"], [time(19, 0)])
        self.assertTrue(all(item["link"].startswith("https://dice.fm/event/") for item in found))

    def test_ics(self):
        self.read("ics", "https://www.cambridgema.gov/arts/Calendar.ics", "film")

    def test_ics_tockify_times_in_boston(self):
        found = self.read("ics", "https://tockify.com/api/feeds/ics/midwaycafejp")
        # 01:30 UTC on Sep 12 is 9:30pm on Sep 11 in Boston.
        self.assertEqual((found[0]["date"], found[0]["times"]), (date(2026, 9, 11), [time(21, 30)]))
        self.assertEqual(found[0]["title"], "CAVA (Germany) w/ Lupo Citta")

    def test_french_library_screenings_and_talks(self):
        found = build.read_french_library(source("frenchlibrary", "https://frenchlibrary.org/upcoming-events/", "art", "French Library"))
        kinds = {item["title"]: item["category"] for item in found}
        self.assertEqual(kinds["Ciné Club Series: Hugo by Pascal Bonitzer"], "film")
        self.assertEqual(kinds["Leïla Slimani: I’ll Take the Fire"], "art")
        # A screening known only by its description: "silent cinema as it was meant to be seen".
        self.assertEqual([kind for title, kind in kinds.items() if "House of Usher" in title], ["film"])
        # Not the children's story time, the cooking class, or the online conversation club.
        self.assertEqual(len(found), 3)
        hugo = next(item for item in found if item["title"].startswith("Ciné Club"))
        self.assertEqual((hugo["date"], hugo["times"]), (date(2026, 9, 15), [time(18, 0)]))

    def test_hfa_screenings_with_credits(self):
        found = self.read("hfa", "https://harvardfilmarchive.org/calendar", "film")
        self.assertEqual(len(found), 4)
        snow = next(item for item in found if item["title"] == "Snow White")
        self.assertEqual((snow["date"], snow["times"]), (date(2026, 9, 14), [time(19, 0)]))
        self.assertEqual(snow["about"][0], "Directed by João César Monteiro, 2000")
        self.assertTrue(snow["link"].startswith("https://harvardfilmarchive.org/calendar/snow-white"))
        self.assertTrue(any("Screening on Film" in line for item in found for line in item["about"]))

    def test_veezi_site_showtimes(self):
        found = self.read("veezi", "https://www.westnewtoncinema.com/", "film")
        rebel = next(item for item in found if item["title"] == "Rebel with a Clause")
        self.assertEqual((rebel["date"], rebel["times"]), (date(2026, 9, 24), [time(19, 0)]))
        self.assertEqual(rebel["link"], "https://www.westnewtoncinema.com/movie/rebel-with-a-clause")
        self.assertEqual(rebel["about"][-1], "Directed by Brandt Johnson · 1h 26m")

    def test_tribe_skips_what_isnt_a_show(self):
        found = self.read("tribe", "https://lizardloungeclub.com/wp-json/tribe/events/v1/events")
        titles = [item["title"] for item in found]
        self.assertEqual(len(titles), 4)
        self.assertIn("The Gravel Project/Lara Cwass", titles)
        self.assertFalse(any("No Event" in title or "Poetry Jam" in title for title in titles))
        self.assertTrue(all(item["times"] for item in found))


class Skipping(unittest.TestCase):
    def test_whole_words_in_any_case(self):
        with mock.patch.object(build, "SKIP_FILE", FIXTURES.parent.parent / "fixtures" / "events" / "skip.txt"):
            skip = build.skipping()
        for title in ["IMPROV COMEDY: an evening of", "Comedy night", "Strummerville Ukulele Club"]:
            self.assertTrue(skip.search(title), title)
        for title in ["A Comedic Evening", "Ukulele Clubhouse Orchestra", "Armand Hammer"]:
            self.assertFalse(skip.search(title), title)

    def test_nothing_to_skip(self):
        with mock.patch.object(build, "SKIP_FILE", FIXTURES / "no-such-file.txt"):
            self.assertIsNone(build.skipping())


class Calendars(unittest.TestCase):
    def test_a_feed_one_event_per_listing_in_utc(self):
        sinclair = dict(source("axs", "x", "music", "The Sinclair"), public=True)
        show = build.event(sinclair, "Chanel Beads; a show, live", date(2026, 9, 13), time(20, 0), link="https://example.com/1",
                           detail="with Horse Vision", about=["Doors at seven " * 10])
        show["times"].append(time(22, 0))
        untimed = build.event(sinclair, "An Exhibition", date(2026, 9, 14), link="https://example.com/2")
        feed = build.render_calendar("Pushpin Boston · The Sinclair", "What's on", [untimed, show], datetime(2026, 9, 13, tzinfo=timezone.utc))
        self.assertTrue(feed.startswith("BEGIN:VCALENDAR\r\n") and feed.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", feed.replace("\r\n", ""), "every line ends CRLF")
        self.assertTrue(all(len(line.encode()) <= 75 for line in feed.split("\r\n")), "folded at 75 bytes")
        self.assertEqual(feed.count("BEGIN:VEVENT"), 2, "one event a listing, however many times it has")
        self.assertIn("DTSTART:20260914T000000Z", feed, "8pm in Boston, in UTC")
        self.assertIn("DTSTART;VALUE=DATE:20260914", feed, "all day, without a time")
        unfolded = feed.replace("\r\n ", "")
        self.assertIn("SUMMARY:Chanel Beads\\; a show\\, live", unfolded)
        self.assertIn("Times: 8pm\\, 10pm", unfolded)
        self.assertIn("LOCATION:The Sinclair\\, 52 Church St\\, Cambridge\\, MA 02138", unfolded)
        uid = re.findall(r"UID:(\S+)", unfolded)
        again = build.render_calendar("x", "y", [untimed, show], datetime(2026, 9, 14, tzinfo=timezone.utc))
        self.assertEqual(uid, re.findall(r"UID:(\S+)", again.replace("\r\n ", "")), "the same ids from build to build")

    def test_a_feed_for_each_kind_and_venue(self):
        sinclair = dict(source("axs", "x", "music", "The Sinclair"), public=True)
        hidden = dict(source("ics", "y", "music", "Not Ours"), public=False)
        items = [build.event(sinclair, "A", date(2026, 9, 13), time(20, 0)), build.event(hidden, "B", date(2026, 9, 13))]
        feeds = build.calendar_feeds([sinclair, hidden], items, datetime(2026, 9, 13, tzinfo=timezone.utc))
        self.assertEqual(set(feeds), {"calendar/music.ics", "calendar/film.ics", "calendar/talks.ics", "calendar/the-sinclair.ics"})
        self.assertNotIn("SUMMARY:B", feeds["calendar/music.ics"], "not a source kept off the public site")
        self.assertEqual(build.calendar_address("calendar/music.ics"), "webcal://pushpin.city/boston/calendar/music.ics")
        self.assertIn("calendar.google.com", build.calendar_address("calendar/music.ics", google=True))

    def test_each_listing_opens_its_own_view(self):
        sinclair = dict(source("axs", "x", "music", "The Sinclair"), public=True)
        items = [build.event(sinclair, "Akira (4K Restoration)", day, time(20, 0)) for day in (date(2026, 9, 13), date(2026, 9, 14))]
        page = build.render_index(items, [sinclair], [], [], datetime(2026, 9, 13, tzinfo=timezone.utc), public=True)
        # Each its own address, which the same show on other days shares the rest of.
        self.assertIn('data-id="2026-09-13-akira-4k-restoration-the-sinclair" data-series="akira-4k-restoration-the-sinclair"', page)
        self.assertIn('data-id="2026-09-14-akira-4k-restoration-the-sinclair"', page)
        self.assertEqual(page.count('<dialog class="event"'), 1)
        self.assertIn('<button class="event-copy" type="button">', page)
        self.assertNotIn("navigator.share", page, "a link to copy, not the share sheet")
        self.assertIn(f"SHARE_URL = {json.dumps(build.PUBLIC_URL)}", page)
        self.assertNotIn('class="share"', page, "sharing is in the view, not on each row")


class Facts(unittest.TestCase):
    def test_price_from_a_line_of_facts(self):
        for line, price in [("$15 / $10 students", "$10–$15"), ("21+ · $10 cover", "$10"), ("Doors 8pm | 21+ | $5-20 cover", "$5–$20"),
                            ("$10-21+", "$10"), ("$13.50", "$13.50"), ("NO COVER! · 21+", "Free"), ("Free (cash only bar)", "Free"),
                            ("Free admission", "Free"), ("$0", ""), ("18 and under free", ""), ("Free pizza", ""),
                            ("Free with museum admission", ""), ("Raised $5 million", ""),
                            ("The cinema will be donating $4 from every ticket", "")]:
            self.assertEqual(build.price_from(line), price, line)
        self.assertEqual(build.price_of("18", 20.0), "$18–$20")
        self.assertEqual(build.price_of(0, None), "")

    def test_ages(self):
        for line, ages in [("All Ages", "All ages"), ("21+ · $10 cover", "21+"), ("18 and over", "18+"), ("18 and under free", "")]:
            self.assertEqual(build.ages_from(line), ages, line)

    def test_an_event_takes_price_and_ages_from_its_facts_not_its_prose(self):
        club = source("ics", "x")
        show = build.event(club, "A show", date(2026, 9, 13), about=["Doors at 8 · 21+ · $10 cover"])
        self.assertEqual((show["price"], show["ages"]), ("$10", "21+"))
        prose = "A long paragraph about the band, who raised $5,000 for charity and have played for all ages, " * 3
        self.assertEqual(build.event(club, "B", date(2026, 9, 13), about=[prose])[("price")], "")
        stated = "Presented by Get To The Gig Ages: This event is 21+ Valid ID required for Entry Doors 7:00 PM " * 2
        self.assertEqual(build.event(club, "C", date(2026, 9, 13), about=[stated])["ages"], "21+", "ages as a venue states them")
        given = build.event(club, "D", date(2026, 9, 13), about=["$10 cover"], price="$12", ages="All Ages")
        self.assertEqual((given["price"], given["ages"]), ("$12", "All ages"), "the source's own, when it gives them")

    def test_lighter_pictures(self):
        self.assertEqual(build.lighter("https://s1.ticketm.net/dam/a/1/x_TABLET_LANDSCAPE_LARGE_16_9.jpg"),
                         "https://s1.ticketm.net/dam/a/1/x_TABLET_LANDSCAPE_16_9.jpg")
        self.assertEqual(build.lighter("https://dice-media.imgix.net/a.jpg?rect=0"), "https://dice-media.imgix.net/a.jpg?rect=0&w=800")
        page = 'srcset="https://s3/p-2-203x300.jpg 203w, https://s3/p-2-768x1138.jpg 768w, https://s3/p-2-1382x2048.jpg 1382w"'
        self.assertEqual(build.lighter("https://s3/p-2-scaled.jpg", page), "https://s3/p-2-768x1138.jpg")
        self.assertEqual(build.lighter("https://s3/q.jpg", page), "https://s3/q.jpg")

    def test_the_view_shows_them(self):
        sinclair = dict(source("axs", "x", "music", "The Sinclair"), public=True)
        show = build.event(sinclair, "A show", date(2026, 9, 13), time(20, 0), image="https://example.com/a.jpg?w=1&h=2",
                           price="$18–$20", ages="21+")
        page = build.render_index([show], [sinclair], [], [], datetime(2026, 9, 13, tzinfo=timezone.utc), public=True)
        self.assertIn('data-image="https://example.com/a.jpg?w=1&amp;h=2" data-price="$18–$20" data-ages="21+"', page)
        self.assertIn('<div class="event-image" hidden><img alt=""', page)
        self.assertIn('<p class="event-facts"></p>', page)
        films = [build.event(dict(sinclair, name=name), "Hope", date(2026, 9, 13), image=image) for name, image in (("A", ""), ("B", "https://b/1.jpg"))]
        self.assertEqual(build.combine_films([dict(film, category="film") for film in films])[0]["image"], "https://b/1.jpg")


class Descriptions(unittest.TestCase):
    def test_all_of_it_kept_and_a_preview_clipped(self):
        long = "\n\n".join(f"Paragraph {n} says a good deal about the show, the band, and the night ahead of us all." for n in range(8))
        kept = build.about(long)
        self.assertEqual(len(kept), 8, "all of it, for the view")
        self.assertEqual(build.clip(["a" * 10, "b b b " * 100], 100), ["a" * 10, ("b b b " * 100)[:90].rsplit(" ", 1)[0] + "…"])
        self.assertEqual(build.clip(["one", "two", "three", "four"], 400), ["one", "two", "three"])
        self.assertEqual(build.clip(["x" * 300, "y y y " * 50], 320, least=60), ["x" * 300], "not a stub of a paragraph")

    def test_the_public_row_has_an_excerpt_and_its_page_all_of_it(self):
        sinclair = dict(source("axs", "x", "music", "The Sinclair"), public=True)
        words = [f"Paragraph {n} says a good deal about the show, the band, and the night ahead of us all." for n in range(6)]
        show = build.event(sinclair, "A show", date(2026, 9, 13), time(20, 0), about=words)
        brief = build.event(sinclair, "Another", date(2026, 9, 13), time(21, 0), about=words[:1])
        page = build.render_index([show, brief], [sinclair], [], [], datetime(2026, 9, 13, tzinfo=timezone.utc), public=True)
        self.assertNotIn("Paragraph 5", page)
        self.assertEqual(page.count('data-more=""'), 1, "only where there's more")
        own = build.event_pages([sinclair], [show])["e/2026-09-13-a-show-the-sinclair/index.html"]
        self.assertIn('<div class="about"><p>Paragraph 0', own)
        self.assertIn("Paragraph 5 says", own)
        self.assertIn('<button class="event-more" type="button" hidden', page)


class SoldOut(unittest.TestCase):
    def test_marked_in_a_name(self):
        club = source("ics", "x")
        for name, clean in [("Yana – SOLD OUT!", "Yana"), ("SOLD OUT: The Bowery Presents ear", "The Bowery Presents ear"),
                            ("Jackie Evans (Sold Out)", "Jackie Evans")]:
            item = build.event(club, name, date(2026, 9, 13))
            self.assertEqual((item["title"], item["sold_out"]), (clean, True), name)
        self.assertFalse(build.event(club, "The Sold Out Crowd", date(2026, 9, 13))["sold_out"])

    def test_some_showings_or_all(self):
        coolidge = dict(source("coolidge", "x", "film", "Coolidge Corner"), public=True)
        day = date(2026, 9, 15)
        first, second = build.event(coolidge, "Union County", day, time(19, 15)), build.event(coolidge, "Union County", day, time(19, 45))
        second["sold_out"] = True
        merged = build.merge_showings([first, second])[0]
        self.assertEqual((merged["times"], merged["sold_out_times"], build.is_sold_out(merged)), ([time(19, 15), time(19, 45)], ["19:45"], False))
        page = build.render_index([merged], [coolidge], [], [], datetime(2026, 9, 13, tzinfo=timezone.utc), public=True)
        self.assertIn('<time data-time="19:15">7:15pm</time><span class="sep">, </span><time data-time="19:45" class="sold">7:45pm</time>', page)
        merged["sold_out_times"] = ["19:15", "19:45"]
        self.assertTrue(build.is_sold_out(merged))
        own = build.event_pages([coolidge], [merged])["e/2026-09-15-union-county-coolidge-corner/index.html"]
        self.assertIn("Coolidge Corner · Tue, Sep 15 · 7:15pm, 7:45pm · Sold out", own)

    def test_coolidge_film_pages_are_fresher(self):
        page = ('<div class="film-showtime-list"><div class="datepicker "><span class="datepicker__day">Tue</span>'
                '<span class="datepicker__date">9/15</span></div><div class="views-row-inactive availability--individual '
                'sales-state--DisplayCustomMessage"><a href="x"><span class="showtime-ticket"><span class="showtime-ticket__time">7:15pm'
                '</span></span></a></div><div class="views-row-active-agiletix availability--individual sales-state--DuringSales">'
                '<a href="y"><span class="showtime-ticket"><span class="showtime-ticket__time">9:30pm</span></span></a></div></div>')
        with mock.patch.object(build, "fetch", lambda *args, **kwargs: page), mock.patch.object(build, "today", lambda: date(2026, 9, 14)):
            states = build.coolidge_film_states("https://coolidge.org/films/union-county")
        self.assertEqual(states, {(date(2026, 9, 15), "19:15"): "DisplayCustomMessage", (date(2026, 9, 15), "21:30"): "DuringSales"})


class SharedLinks(unittest.TestCase):
    def test_each_listing_has_a_page_with_its_own_preview(self):
        coolidge = dict(source("coolidge", "x", "film", "Coolidge Corner"), public=True)
        alamo = dict(source("alamo", "y", "film", "Alamo Drafthouse"), public=True)
        hidden = dict(source("ics", "z", "music", "Not Ours"), public=False)
        day = date(2026, 9, 15)
        items = [build.event(coolidge, "Akira (4K Restoration)", day, time(15, 30)),
                 build.event(coolidge, "Hope", day, time(12, 45)), build.event(alamo, "Hope", day, time(14, 15)),
                 build.event(hidden, "Theirs", day)]
        items[0]["times"] += [time(18, 45)]
        pages = build.event_pages([coolidge, alamo, hidden], items)
        self.assertEqual(set(pages), {"e/2026-09-15-akira-4k-restoration-coolidge-corner/index.html", "e/2026-09-15-hope/index.html"})
        akira = pages["e/2026-09-15-akira-4k-restoration-coolidge-corner/index.html"]
        self.assertIn('<meta property="og:title" content="Showtimes for Akira (4K Restoration) at Coolidge Corner · Tue, Sep 15">', akira)
        self.assertIn('<meta property="og:title" content="Showtimes for Hope · Tue, Sep 15">', pages["e/2026-09-15-hope/index.html"])
        self.assertIn('<meta property="og:description" content="Coolidge Corner · Tue, Sep 15 · 3:30pm, 6:45pm">', akira)
        self.assertIn(f'<meta property="og:image" content="{build.PUBLIC_URL}share/film.png?pin">', akira)
        self.assertIn(f'location.replace("{build.PUBLIC_URL}?event=2026-09-15-akira-4k-restoration-coolidge-corner")', akira)
        self.assertIn('<meta name="robots" content="noindex">', akira)
        self.assertIn("2 theaters (Alamo Drafthouse, Coolidge Corner) · Tue, Sep 15 · from 12:45pm", pages["e/2026-09-15-hope/index.html"])
        self.assertIn('<meta property="og:image:width" content="1200">', akira)

    def test_a_listings_own_picture_price_and_ages_in_its_preview(self):
        rockwell = dict(source("tribe", "x", "music", "The Rockwell"), public=True)
        show = build.event(rockwell, "Telescreens", date(2026, 9, 18), time(19, 0), image="https://therockwell.org/t.png",
                           price="$18–$20", ages="21+")
        page = build.event_pages([rockwell], [show])["e/2026-09-18-telescreens-the-rockwell/index.html"]
        self.assertIn('<meta property="og:image" content="https://therockwell.org/t.png">', page)
        self.assertNotIn("og:image:width", page, "its size isn't known")
        self.assertIn('content="The Rockwell · Fri, Sep 18 · 7pm · $18–$20 · 21+"', page)
        self.assertIn('<meta property="og:title" content="Telescreens at The Rockwell · Fri, Sep 18, 7pm">', page)
        armory = dict(source("armory", "x", "music", "Arts at the Armory"), public=True)
        spotlight = build.event(armory, "Arts at the Armory Spotlight Series Presents L. Shankar", date(2026, 9, 17), time(19, 0))
        self.assertEqual(build.share_title(spotlight, date(2026, 9, 17)), "Arts at the Armory Spotlight Series Presents L. Shankar · Thu, Sep 17, 7pm")

    def test_a_passed_listings_page_goes_to_its_view(self):
        missing = build.render_redirect(build.PUBLIC_URL, paths=True)
        self.assertIn(f'location.pathname.startsWith("{build.PUBLIC_ROOT}e/")', missing)
        self.assertIn('"?event=" + location.pathname', missing)


class Moving(unittest.TestCase):
    def test_redirects_keep_the_page_and_its_filters(self):
        page = build.render_redirect(build.PUBLIC_URL + "music/")
        self.assertIn(f'<meta http-equiv="refresh" content="0; url={build.PUBLIC_URL}music/">', page)
        self.assertIn(f'<link rel="canonical" href="{build.PUBLIC_URL}music/">', page)
        self.assertIn("location.search + location.hash", page, "?venue= and ?q= come along")
        missing = build.render_redirect(build.PUBLIC_URL, paths=True)
        self.assertIn("location.pathname.slice(1)", missing, "an old /music/ goes to /boston/music/")
        self.assertIn(f'location.pathname.startsWith("{build.PUBLIC_ROOT}")', missing, "and never /boston/boston/")


class Window(unittest.TestCase):
    def test_concerts_two_months_ahead_the_rest_one(self):
        with mock.patch.object(build, "today", lambda: date(2026, 9, 1)):
            self.assertEqual(build.window_end("music"), date(2026, 10, 31))
            self.assertEqual(build.window_end("film"), date(2026, 10, 1))
            self.assertEqual(build.window_end(), date(2026, 10, 1))


class Fetching(unittest.TestCase):
    def test_a_page_read_twice_is_downloaded_once(self):
        calls = []
        def download(url, *args):
            calls.append(url)
            return url, b"<html></html>"
        build._fetched.clear()
        self.addCleanup(build._fetched.clear)
        with mock.patch.object(build.shared, "fetch", download):
            build.fetch("https://www.icaboston.org/events/colin-stetson")
            build.fetch("https://www.icaboston.org/events/colin-stetson")
        self.assertEqual(calls, ["https://www.icaboston.org/events/colin-stetson"])

    def test_mit_asks_for_exhibits_and_lectures_only(self):
        asked = []
        with mock.patch.object(build, "fetch", lambda url: asked.append(url) or sample(url)):
            build.read_mit(source("mit", "https://calendar.mit.edu/api/2/events", "art", "MIT"))
        self.assertIn("type%5B%5D=102763&type%5B%5D=102764", asked[0])

    def test_ica_reads_event_pages_only_for_events_in_the_window(self):
        asked = []
        with mock.patch.object(build, "fetch", lambda url, **options: asked.append(url) or sample(url)), \
                mock.patch.object(build, "today", lambda: date(2026, 6, 1)):  # Its sample's events are all past the window then.
            build.read_ica(source("ica", "https://www.icaboston.org/calendar", "art", "ICA"))
        self.assertEqual(asked, ["https://www.icaboston.org/calendar"])


class About(unittest.TestCase):
    """What a source says about an event, for its preview."""

    def test_short_facts_run_together_and_links_go(self):
        self.assertEqual(
            build.about("<p>Doors 7pm</p><p>21+</p><p>$10 cover</p><p>Event page:</p><p>Buy tickets</p>"
                        "<p>Some Band</p><p>someband.bandcamp.com</p><p>https://example.com/show</p>"),
            ["Doors 7pm · 21+ · $10 cover"])

    def test_a_sentence_cut_by_a_line_break_is_whole(self):
        self.assertEqual(build.about("A folk singer redefining the genre through a<br>powerful blend of soul.<br>Doors 7"),
                         ["A folk singer redefining the genre through a powerful blend of soul.", "Doors 7"])

    def test_a_line_repeating_the_name_is_left_out(self):
        item = build.event(source("tribe", "x"), "The 4411", date(2026, 9, 13), about=["The 4411", "Ages: All Ages"])
        self.assertEqual(item["about"], ["Ages: All Ages"])

    def test_from_the_samples(self):
        with mock.patch.object(build, "fetch", sample):
            midway = build.read_ics(source("ics", "https://tockify.com/api/feeds/ics/midwaycafejp"))[0]
            brattle = build.read_jsonld(source("jsonld", "https://brattlefilm.org/coming-soon/", "film"))[0]
            coolidge = build.read_coolidge(source("coolidge", "https://coolidge.org/showtimes", "film"))[0]
            roadrunner = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json"))[0]
        self.assertEqual(midway["about"], ["Doors at 9:30pm · Show at 10:15 pm · 21+ · $10 cover · cash only venue"])
        # The Brattle's own description is just the dates, so its film's credits stand in.
        self.assertEqual(brattle["about"], ["Directed by Rafael Manuel. With Carlos Siguion-Reyna, Jorrybell Agoto, Nour Hooshmand.",
                                            "Drama, Mystery · 1h 40m · Tagalog with English subtitles"])
        self.assertEqual(coolidge["about"][-1], "1hr 38mins")
        self.assertTrue(roadrunner["about"][0].startswith("For a while there, Charles Wesley Godwin"), "the bio, not the ticket note")


class ArtAndTalks(unittest.TestCase):
    """The art & talks readers, on a fixed day, since their samples' exhibitions open and close on real dates."""

    def setUp(self):
        for name, value in [("fetch", sample), ("today", lambda: date(2026, 9, 12))]:
            patcher = mock.patch.object(build, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def titles(self, found):
        return [item["title"] for item in found]

    def test_mit_keeps_public_art_and_humanities(self):
        found = Readers.read(self, "mit", "https://calendar.mit.edu/api/2/events", "art")
        titles = self.titles(found)
        self.assertEqual(titles, ["Overburden with Gregg Mitman", "TAKK—Mireia Luzárraga", "Art21 Screening with Josh Kline"])
        self.assertEqual(found[0]["times"], [time(16, 0)])
        # Left out: a virtual conference, a talk in New York, and a talk and an exhibition for MIT people only.

    def test_mit_exhibition_listed_once_when_it_opens(self):
        # The sample's running exhibition, which MIT lists for its own community, made public.
        data = __import__("json").loads(sample("calendar.mit.edu"))
        for entry in data["events"]:
            entry["event"]["filters"]["event_audience"] = [{"name": "Public", "id": 98747}]
        public = __import__("json").dumps(data)
        with mock.patch.object(build, "today", lambda: date(2026, 8, 1)), mock.patch.object(build, "fetch", lambda url: public):
            found = build.read_mit(source("mit", "https://calendar.mit.edu/api/2/events", "art", "MIT"))
        opening = [item for item in found if item["title"].startswith("Howe, Manning & Almy")]
        self.assertEqual(len(opening), 1)
        self.assertEqual((opening[0]["date"], opening[0]["times"], opening[0]["detail"]), (date(2026, 8, 19), [], "through May 27"))

    def test_library_talks_by_branch(self):
        found = Readers.read(self, "bibliocommons", "https://gateway.bibliocommons.com/v2/libraries/bpl/rss/events?types=test",
                             "art", "Boston Public Library")
        venues = {item["title"]: item["venue"] for item in found}
        self.assertEqual(venues["Author Talk: N.K. Jemisin on Revolutionary Ideas in Fantasy Fiction"], "Boston Public Library")
        self.assertEqual(venues["Author Talk: Ric Calleja - Making Dinner con Amor"], "Faneuil Library")
        self.assertIn("Great Decisions — Multilateral Institutions", venues)
        # Left out: a virtual business class, a teens' workshop, and an exhibition open since last year.
        self.assertFalse([title for title in venues if "Tech Talk" in title or "Adulting" in title or "Becoming Boston" in title])

    def test_mfa_programs_by_kind(self):
        # Each kind from its own page, which says what it is: no guided tours or studio classes to skip.
        found = build.read_mfa(source("mfa", "https://www.mfa.org/programs", "art", "MFA"))
        self.assertEqual({item["title"]: item["category"] for item in found}, {
            "The Road to Livres d’Artiste": "art",
            "Dalí and the Book": "art",
            "$5 Third Thursday": "art",
            "First Fridays": "art",
            "Deep Quiet Room (深度安靜)": "film",
            "Dust in the Wind (戀戀風塵)": "film",
            "Lucky Lu (幸福之路)": "film",
            "Camelia Latin-Jazz Trio": "music",
        })
        self.assertEqual(found[0]["times"], [time(13, 0)])

    def test_ica_by_kind(self):
        found = build.read_ica(source("ica", "https://www.icaboston.org/calendar", "art", "ICA"))
        self.assertEqual({item["title"]: item["category"] for item in found}, {
            "Gallery Talk: Erika Umali on To My Best Friend": "art",
            "Colin Stetson & Brìghde Chaimbeul": "music",
            "TCB – The Toni Cade Bambara School of Organizing": "film",
        })  # Not the kids' workshop, the members' opening, or the online series.
        talk = found[0]
        self.assertEqual((talk["date"], talk["times"]), (date(2026, 9, 20), [time(14, 0)]))
        # Its concert's own page gives what it's about and what tickets cost; the talk's, not saved here, nothing.
        concert = next(item for item in found if item["category"] == "music")
        self.assertTrue(concert["about"][0].startswith("Colin Stetson and Brìghde Chaimbeul come together"))
        self.assertEqual(concert["about"][-1], "$30 ICA members / $35 students / $40 nonmembers")
        self.assertEqual(talk["about"], [])

    def test_times_from_a_range(self):
        for when, start in [("12–4 PM", time(12, 0)), ("10 AM–5 PM", time(10, 0)), ("5:30–9:30 PM", time(17, 30)),
                            ("7 PM", time(19, 0)), ("11–1 PM", time(11, 0)), ("8 PM–12 AM", time(20, 0))]:
            self.assertEqual(build.clock_range_start(when), start, when)

    def test_harvard_art_museums(self):
        listings = __import__("json").loads((FIXTURES / "harvardart.json").read_text())
        with mock.patch.object(build, "harvard_art_listings", lambda url, months: listings):
            found = build.read_harvard_art(source("harvardart", "https://harvardartmuseums.org/calendar", "art", "Harvard Art Museums"))
        titles = self.titles(found)
        self.assertIn("Decompress with Rocky Mountains, “Lander’s Peak”", titles)
        self.assertIn("Exhibition Opening Program for The Way We Never Were: Barbara Norfleet and Photography at Harvard", titles)
        self.assertFalse([title for title in titles if "Spotlight Tour" in title or "Online" in title])
        conserving = next(item for item in found if item["title"].startswith("Art + Science"))
        self.assertEqual((conserving["date"], conserving["times"]), (date(2026, 9, 15), [time(12, 0)]))


class Films(unittest.TestCase):
    def test_film_key_matches_spellings(self):
        year = datetime.now(build.BOSTON).year
        self.assertEqual(build.film_key("Coyote vs. ACME"), build.film_key("Coyote vs. Acme"))
        self.assertEqual(build.film_key("The Odyssey (Digital)"), build.film_key("The Odyssey"))
        self.assertEqual(build.film_key("Akira (4K Restoration)"), build.film_key("Akira"))
        self.assertEqual(build.film_key(f"Tony ({year})"), build.film_key("Tony"))
        self.assertNotEqual(build.film_key("Hope (1987)"), build.film_key(f"Hope ({year})"))

    def test_combine_films(self):
        day = date(2026, 9, 20)
        showing = lambda title, venue, hour: {  # noqa: E731
            "title": title, "date": day, "times": [time(hour)], "link": "https://x", "detail": "",
            "venue": venue, "category": "film", "source": venue,
        }
        rows = build.combine_films([
            showing("Coyote vs. ACME", "Alamo Drafthouse", 15),
            showing("Coyote vs. Acme", "Coolidge Corner", 14),
            showing("Paris, Texas", "Brattle", 19),
        ])
        combined = [row for row in rows if "showings" in row]
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["title"], "Coyote vs. Acme", "the plainer spelling")
        self.assertEqual(combined[0]["times"], [time(14)], "from its earliest showing")
        self.assertEqual(len(rows), 2)


class Fallback(unittest.TestCase):
    built = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    venue = source("aeg", "https://example.com", name="Roadrunner")

    def listing(self, days_ahead, title="Show"):
        return build.event(self.venue, title, date(2026, 9, 20) + timedelta(days=days_ahead), time(20))

    def previous(self, hours_old, days_ahead=2):
        fetched = (self.built - timedelta(hours=hours_old)).isoformat()
        return {"sources": {"Roadrunner": {"fetched": fetched, "events": [build.saved(self.listing(days_ahead, "Kept"))]}}}

    def test_fresh(self):
        events, failed, stale, listings, errors = build.gather([(self.venue, [self.listing(1)], None)], {}, self.built)
        self.assertEqual(([e["title"] for e in events], failed, stale, errors), (["Show"], [], [], {}))
        self.assertEqual(listings["Roadrunner"]["fetched"], self.built.isoformat())

    def test_error_uses_recent_listings(self):
        events, failed, stale, listings, errors = build.gather([(self.venue, [], OSError("520"))], self.previous(6), self.built)
        self.assertEqual([e["title"] for e in events], ["Kept"])
        self.assertEqual([name for name, _ in stale], ["Roadrunner"])
        self.assertEqual(listings["Roadrunner"]["fetched"], (self.built - timedelta(hours=6)).isoformat(),
                         "the copy keeps its age, so it still expires")
        self.assertIn("Roadrunner", errors)

    def test_error_with_old_listings_fails(self):
        events, failed, stale, *_ = build.gather([(self.venue, [], OSError("520"))], self.previous(72), self.built)
        self.assertEqual((events, failed, stale), ([], ["Roadrunner"], []))

    def test_empty_after_having_shows_uses_listings(self):
        events, failed, stale, _, errors = build.gather([(self.venue, [], None)], self.previous(3), self.built)
        self.assertEqual([e["title"] for e in events], ["Kept"])
        self.assertEqual(errors["Roadrunner"], "returned no listings")

    def test_empty_and_nothing_before_is_just_empty(self):
        events, failed, stale, _, errors = build.gather([(self.venue, [], None)], {}, self.built)
        self.assertEqual((events, failed, stale, errors), ([], [], [], {}))

    def test_past_listings_are_not_restored(self):
        events, *_ = build.gather([(self.venue, [], OSError("520"))], self.previous(6, days_ahead=-1), self.built)
        self.assertEqual(events, [])

    def test_failing_since_carries_over(self):
        earlier = {"failing": {"Roadrunner": {"since": "2026-09-20T01:00:00+00:00", "error": "520"}}}
        failing = build.still_failing({"Roadrunner": "timed out", "Paradise": "403"}, earlier, self.built)
        self.assertEqual(failing["Roadrunner"], {"since": "2026-09-20T01:00:00+00:00", "error": "timed out"})
        self.assertEqual(failing["Paradise"]["since"], self.built.isoformat())


R = build.PUBLIC_ROOT  # The public site's root, which its links start from.


class Page(unittest.TestCase):
    def test_renders_rows_and_filters(self):
        with mock.patch.object(build, "fetch", sample):
            found = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json"))
        page = build.render_index(found, [source("aeg", "x", name="Roadrunner")], [], [], datetime.now(timezone.utc))
        self.assertEqual(len(re.findall(r'<li class="row" data-id="[^"]+" data-series="[^"]+" data-category="music" data-sources="Somewhere"[^>]*>', page)), len(found))
        self.assertEqual(page.count('<div class="preview">'), sum(bool(item["about"]) for item in found))
        for key in build.CATEGORIES:
            self.assertIn(f'data-show="{key}"', page)
        self.assertIn("[hidden] {{ display: none !important; }}".replace("{{", "{").replace("}}", "}"), page)

    def test_public_page(self):
        with mock.patch.object(build, "fetch", sample):
            found = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json", name="Roadrunner"))
            kept = build.read_jsonld(source("jsonld", "https://brattlefilm.org/coming-soon/", "film", "Private Theater"))
        sources = [dict(source("aeg", "x", name="Roadrunner"), public=True),
                   dict(source("jsonld", "y", "film", "Private Theater"), public=False)]
        built = datetime.now(timezone.utc)
        public = build.render_index(found + kept, sources, [], [], built, public=True)
        mine = build.render_index(found + kept, sources, [], [], built)
        self.assertNotIn("Newsfeed", public)
        footer = public.split("<footer>")[1]
        for label, href in [("All events", R), ("Music", R + "music/"), ("Film", R + "film/"), ("Art &amp; talks", R + "talks/"),
                            ("This weekend", R + "weekend/"), ("Next weekend", R + "weekend/next/"), ("About", R + "about/"), ("Contact", R + "contact/")]:
            self.assertIn(f'<a href="{href}"', footer, label)
        self.assertIn(f'<a href="{R}" aria-current="page">All events</a>', footer, "this page marked")
        self.assertIn("Listings from Roadrunner, aggregated", footer)
        self.assertNotIn(f'href="{R}about/"', public.split("<footer>")[0], "not in the header")
        self.assertNotIn("Add a source", public)
        self.assertIn('content="index, follow"', public)
        self.assertIn('content="noindex, nofollow"', mine)
        self.assertNotIn("Private Theater", public, "a source marked public=no stays off it")
        self.assertIn("Private Theater", mine)
        listings = lambda page: page.split("</style>")[1].split("<footer>")[0]  # Not the styles, which differ.
        self.assertLess(len(listings(public)), len(listings(mine)), "shorter previews, and not the private listings")
        about = build.render_about(sources, built)
        self.assertIn("Roadrunner", about)
        self.assertNotIn("Private Theater", about)
        self.assertIn("https://formsubmit.co/", build.render_contact())
        self.assertIn(f'<a href="{R}contact/">Contact</a>', about.split("<footer>")[1])
        self.assertIn(f'<a href="{R}contact/">Get in touch</a>', about, "from the About page's folder too")
        self.assertIn(f'content="{build.PUBLIC_URL}share/home.png?pin"', about, "the home page's card: About has none")

    def test_public_page_tells_search_engines_what_it_is(self):
        with mock.patch.object(build, "fetch", sample):
            found = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json", name="Roadrunner"))
        page = build.render_index(found, [dict(source("aeg", "x", name="Roadrunner"), public=True)], [], [],
                                  datetime.now(timezone.utc), public=True)
        self.assertIn(f"<title>{build.PUBLIC_TITLE}</title>", page)
        self.assertIn(f'<link rel="canonical" href="{build.PUBLIC_URL}">', page)
        self.assertIn('<meta property="og:title"', page)
        self.assertIn('<h1 class="tagline">', page)
        data = __import__("json").loads(page.split('<script type="application/ld+json">')[1].split("</script>")[0])["@graph"]
        self.assertEqual(data[0]["@type"], "WebSite")
        show = data[1]
        self.assertEqual(show["@type"], "MusicEvent")
        self.assertEqual(show["location"]["address"]["streetAddress"], "89 Guest St")  # Roadrunner's.
        self.assertRegex(show["startDate"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00-0[45]:00$")
        self.assertEqual(len(data), len(found) + 1)

    def test_category_pages(self):
        with mock.patch.object(build, "fetch", sample):
            music = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json", name="Roadrunner"))
            films = build.read_jsonld(source("jsonld", "https://brattlefilm.org/coming-soon/", "film", "Brattle"))
        sources = [dict(source("aeg", "x", name="Roadrunner"), public=True), dict(source("jsonld", "y", "film", "Brattle"), public=True)]
        built = datetime.now(timezone.utc)
        home = build.render_index(music + films, sources, [], [], built, public=True)
        film = build.render_index(music + films, sources, [], [], built, public=True, category="film")
        mine = build.render_index(music + films, sources, [], [], built)
        # The home page's filter links to each kind's page, and marks All; the film page marks Film.
        self.assertIn('data-pages data-current="all"', home)
        self.assertIn(f'<a data-show="film" href="{R}film/">', home)
        self.assertIn(f'<a data-show="all" href="{R}" aria-current="page">All</a>', home)
        self.assertIn('data-current="film"', film)
        self.assertIn(f'<link rel="canonical" href="{build.PUBLIC_URL}film/">', film)
        self.assertIn('<span class="here">Film</span></nav>', film, "the page's name after the site's, in the header")
        self.assertIn('<span class="here" hidden></span></nav>', home, "none on the home page, until a kind is shown")
        self.assertIn(f'<meta property="og:image" content="{build.PUBLIC_URL}share/film.png?pin">', film)
        self.assertIn(f'<meta property="og:image" content="{build.PUBLIC_URL}share/home.png?pin">', home)
        for card in ("home", "music", "film", "talks"):
            self.assertTrue((build.ROOT / "share" / f"{card}.png").exists(), f"share/{card}.png")
        self.assertIn(f"<title>{build.PUBLIC_PAGES['film'][1]}</title>", film)
        # The film page has only films, and says which sources they're from.
        self.assertEqual(film.count('class="row" data-category="music"'), 0)
        self.assertGreater(film.count('data-category="film" data-sources='), 0)
        # Its venue menu has every venue, whatever its kind: a choice goes to the home page, which has them all.
        menu = film.split('<select class="pick" aria-label="Venue">')[1].split("</select>")[0]
        self.assertIn('<option value="Roadrunner">', menu)
        self.assertNotIn("Roadrunner", film.split("<footer>")[1])
        # Your own page keeps its buttons.
        self.assertIn('<button data-show="film">', mine)
        self.assertIn('<nav class="filter" aria-label="Show"><button', mine)
        self.assertIn(f"<loc>{build.PUBLIC_URL}talks/</loc>", build.render_sitemap(built))

    def test_weekend_days(self):
        # Monday to Thursday, the weekend coming; Friday to Sunday, the one it's in.
        for day, friday in [(date(2026, 9, 14), date(2026, 9, 18)), (date(2026, 9, 17), date(2026, 9, 18)),
                            (date(2026, 9, 18), date(2026, 9, 18)), (date(2026, 9, 20), date(2026, 9, 18))]:
            self.assertEqual(build.weekend_days(day), (friday, friday + timedelta(days=2)), day)
        self.assertEqual(build.weekend_days(date(2026, 9, 13), 1), (date(2026, 9, 18), date(2026, 9, 20)))
        self.assertEqual(build.weekend_span(date(2026, 10, 30), date(2026, 11, 1)), "Oct 30–Nov 1")

    def test_weekend_page(self):
        venue = dict(source("tribe", "x", name="Somewhere"), public=True)
        events = [build.event(venue, f"On the {day.day}th", day, time(20, 0)) for day in
                  (date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 20), date(2026, 9, 21))]
        built = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)  # A Monday.
        page = build.render_index(events, [venue], [], [], built, public=True, weekend=0)
        self.assertEqual([title for title in ("On the 17th", "On the 18th", "On the 20th", "On the 21th") if title in page],
                         ["On the 18th", "On the 20th"])
        self.assertIn("Friday, September 18 to Sunday, September 20", page)
        self.assertIn(f'<link rel="canonical" href="{build.PUBLIC_URL}weekend/">', page)
        self.assertIn('<nav class="filter" aria-label="Show" data-here data-one-page><button', page)
        self.assertIn(f'<meta property="og:image" content="{build.PUBLIC_URL}share/weekend.png?pin">', page)
        self.assertIn(f'<a href="{R}about/" aria-current="page">About</a>', build.render_about([venue], built))
        self.assertIn(f'<a href="{R}weekend/" aria-current="page">This weekend</a>', page)
        self.assertIn(f"<loc>{build.PUBLIC_URL}weekend/</loc>", build.render_sitemap(built))
        self.assertIn(f'<a href="{R}weekend/next/">Next weekend, Sep 25–27 →</a>', page)
        following = build.render_index(events, [venue], [], [], built, public=True, weekend=1)
        self.assertNotIn("On the 18th", following)
        self.assertIn("Next weekend around Boston, Cambridge, and Somerville: Friday, September 25", following)
        self.assertIn(f'<a href="{R}weekend/">← This weekend, Sep 18–20</a>', following)
        self.assertIn(f'<link rel="canonical" href="{build.PUBLIC_URL}weekend/next/">', following)
        self.assertIn(f'<meta property="og:image" content="{build.PUBLIC_URL}share/weekend.png?pin">', following)
        self.assertIn(f"<loc>{build.PUBLIC_URL}weekend/next/</loc>", build.render_sitemap(built))

    def test_tonight_page(self):
        venue = dict(source("tribe", "x", name="Somewhere"), public=True)
        events = [build.event(venue, f"On the {day.day}th", day, time(21, 0)) for day in
                  (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16))]
        built = datetime(2026, 9, 14, 20, tzinfo=timezone.utc)  # 4pm on Monday, Sep 14, in Boston.
        page = build.render_index(events, [venue], [], [], built, public=True, tonight=True)
        # Today's, and tomorrow's for after midnight (the script shows only the day it is), not later.
        self.assertIn("On the 14th", page)
        self.assertIn("On the 15th", page)
        self.assertNotIn("On the 16th", page)
        self.assertIn('<nav class="filter" aria-label="Show" data-here data-one-page data-today><button', page)
        self.assertIn(f"<title>{build.PUBLIC_TONIGHT[1]}</title>", page)
        self.assertIn(f'<link rel="canonical" href="{build.PUBLIC_URL}tonight/">', page)
        self.assertIn(f'<meta property="og:image" content="{build.PUBLIC_URL}share/tonight.png?pin">', page)
        self.assertIn(f'<a href="{R}tonight/" aria-current="page">Tonight</a>', page)
        self.assertIn(f"<loc>{build.PUBLIC_URL}tonight/</loc>", build.render_sitemap(built))

    def test_event_data_uses_an_events_own_address(self):
        item = build.event(source("bibliocommons", "x", "art", "Boston Public Library"), "A Talk", date(2026, 9, 20),
                           time(18, 0), venue="Faneuil Library", address=("419 Faneuil Street", "Brighton", "02135"))
        address = build.event_data(item)["location"]["address"]
        self.assertEqual((address["streetAddress"], address["addressLocality"], address["postalCode"]),
                         ("419 Faneuil Street", "Brighton", "02135"))

    def test_postal(self):
        self.assertEqual(build.postal("134 MEMORIAL DR, Cambridge, MA 02139"), ("134 MEMORIAL DR", "Cambridge", "02139"))
        self.assertEqual(build.postal(" 3496 Washington St, Boston, MA 02130, USA"), ("3496 Washington St", "Boston", "02130"))
        self.assertIsNone(build.postal("355 W 16th St, Manhattan, New York, New York, 10011"))
        self.assertIsNone(build.postal(""))

    def test_about_lists_venues_by_kind_with_links_and_counts(self):
        mfa = dict(source("mfa", "x", "art", "MFA"), public=True)
        sinclair = dict(source("axs", "y", "music", "The Sinclair"), public=True)
        quiet = dict(source("ics", "z", "music", "Lizard Lounge"), public=True)  # Nothing coming up.
        hidden = dict(source("jsonld", "w", "film", "Not Ours"), public=False)
        day = date(2026, 9, 20)
        items = [build.event(sinclair, "A", day), build.event(sinclair, "B", day), build.event(mfa, "Talk", day),
                 dict(build.event(mfa, "A Film", day), category="film"), build.event(hidden, "Theirs", day)]
        about = build.render_about([mfa, sinclair, quiet, hidden], datetime(2026, 9, 13, tzinfo=timezone.utc), items)
        sections = {h: body for h, body in re.findall(r"<h2>([^<]+)</h2>\n<ul class=\"venues\">(.*?)</ul>", about)}
        self.assertIn(f'<a href="{R}?venue=The+Sinclair">The Sinclair</a> <span class="count">2</span>', sections["Music"])
        self.assertLess(sections["Music"].index("Lizard Lounge"), sections["Music"].index("The Sinclair"), "The Sinclair among the S's")
        self.assertIn(f'<a href="{R}?venue=Lizard+Lounge">Lizard Lounge</a></li>', sections["Music"], "listed, with no count")
        self.assertIn("MFA</a> <span class=\"count\">1</span>", sections["Film"], "under each kind it has")
        self.assertIn("MFA</a> <span class=\"count\">1</span>", sections["Art &amp; talks"])
        self.assertNotIn("Not Ours", about)
        self.assertIn("How to use it", about)
        # The questions, each opening to its answer, and told to search engines as an FAQ.
        self.assertEqual(about.count("<details><summary>"), len(build.FAQS))
        self.assertIn("<summary>Why isn’t the Somerville Theatre on here?</summary>", about)
        data = json.loads(about.split('<script type="application/ld+json">')[1].split("</script>")[0])
        self.assertEqual(data["@type"], "FAQPage")
        self.assertEqual([q["name"] for q in data["mainEntity"]], [q for q, _ in build.FAQS])
        self.assertNotIn("<a", json.dumps(data), "answers as plain text there")

    def test_sitemap(self):
        sitemap = build.render_sitemap(datetime(2026, 9, 13, tzinfo=timezone.utc))
        for path in ("", "about/", "contact/"):
            self.assertIn(f"<loc>{build.PUBLIC_URL}{path}</loc>", sitemap)

    def test_notices_come_last_with_their_mark(self):
        venue = dict(source("tribe", "x", name="Lizard Lounge"), public=True)
        items = [build.event(venue, "A Show", date(2026, 9, 20), time(20, 0))]
        stale = [("Lizard Lounge", datetime(2026, 9, 13, 12, tzinfo=timezone.utc))]
        built = datetime(2026, 9, 13, 13, tzinfo=timezone.utc)
        for page in (build.render_index(items, [venue], [], stale, built, public=True),
                     build.render_index(items, [venue], [], stale, built)):
            footer = page.split("<footer>")[1]
            self.assertIn('<p class="notice"><svg class="icon" aria-hidden="true"><use href="#icon-notice"/></svg><span>Couldn’t reach Lizard Lounge', footer)
            self.assertLess(footer.index("rom Lizard Lounge"), footer.index('class="notice"'), "under the list of venues")

    def test_public_previews_are_shorter(self):
        long = ["A" * 150 + " " + "b" * 150, "Doors 7 · 21+"]
        self.assertLessEqual(len(" ".join(build.shorter(long))), build.PUBLIC_ABOUT_CHARS + 1)
        self.assertEqual(build.shorter(["Short.", "Doors 7"]), ["Short.", "Doors 7"])

    def test_public_no_in_sources(self):
        with mock.patch.object(build, "SOURCES_FILE", mock.Mock(read_text=lambda: "tribe  https://a  art  Somewhere Nice  public=no\n"
                                                                                  "tribe  https://b  art  Elsewhere\n")):
            sources = build.read_sources()
        self.assertEqual([(s["name"], s["public"]) for s in sources], [("Somewhere Nice", False), ("Elsewhere", True)])




if __name__ == "__main__":
    unittest.main()
