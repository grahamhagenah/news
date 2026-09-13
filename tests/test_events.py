"""Tests for build.py, against saved samples of each source (tests/fixtures), so they run offline and a change
that breaks a reader fails here instead of on the live page. Run from the repo's top folder with: python3 -m unittest

The samples are real responses, trimmed to a few items, except ticketmaster.json: the Discovery API needs a
key, so that one follows its documented format. When a site changes and its reader is updated, save a fresh
sample alongside the fix.
"""

import os
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
    ("sinclaircambridge.com", "sinclair.xml"),
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
    ("bostonfilmhub.com", "bostonfilmhub.html"),
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

    def test_rss_dates_from_titles(self):
        found = self.read("rss", "https://www.sinclaircambridge.com/events/rss")
        self.assertFalse(any(" on " in item["title"][-15:] for item in found), "the date should come off the title")

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

    def test_jsonld_kept_to_one_venue(self):
        found = self.read("jsonld", "https://bostonfilmhub.com/#venue=Somerville+Theatre", "film", "Somerville Theatre via Boston Film Hub")
        self.assertEqual([(item["title"], item["venue"]) for item in found], [("Harold and Maude", "Somerville Theatre")])
        self.assertTrue(found[0]["link"].startswith("https://www.somervilletheatre.com/"))
        self.assertTrue(found[0]["about"])

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


class Fetching(unittest.TestCase):
    def test_a_page_two_sources_read_is_downloaded_once(self):
        calls = []
        def download(url, *args):
            calls.append(url)
            return url, b"<html></html>"
        build._fetched.clear()
        self.addCleanup(build._fetched.clear)
        with mock.patch.object(build.shared, "fetch", download):
            build.fetch("https://bostonfilmhub.com/#venue=Somerville+Theatre")
            build.fetch("https://bostonfilmhub.com/#venue=Capitol+Theatre")
        self.assertEqual(calls, ["https://bostonfilmhub.com/"])

    def test_mit_asks_for_exhibits_and_lectures_only(self):
        asked = []
        with mock.patch.object(build, "fetch", lambda url: asked.append(url) or sample(url)):
            build.read_mit(source("mit", "https://calendar.mit.edu/api/2/events", "art", "MIT"))
        self.assertIn("type%5B%5D=102763&type%5B%5D=102764", asked[0])

    def test_ica_reads_event_pages_only_for_events_in_the_window(self):
        asked = []
        with mock.patch.object(build, "fetch", lambda url, **options: asked.append(url) or sample(url)), \
                mock.patch.object(build, "today", lambda: date(2026, 8, 1)):  # Its sample's events are all past the window then.
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


class Page(unittest.TestCase):
    def test_renders_rows_and_filters(self):
        with mock.patch.object(build, "fetch", sample):
            found = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json"))
        page = build.render_index(found, [source("aeg", "x", name="Roadrunner")], [], [], datetime.now(timezone.utc))
        self.assertEqual(page.count('<li class="row" data-category="music">'), len(found))
        self.assertEqual(page.count('<div class="preview">'), sum(bool(item["about"]) for item in found))
        for key in build.CATEGORIES:
            self.assertIn(f'data-show="{key}"', page)
        self.assertIn("[hidden] {{ display: none !important; }}".replace("{{", "{").replace("}}", "}"), page)

    def test_public_page(self):
        with mock.patch.object(build, "fetch", sample):
            found = build.read_aeg(source("aeg", "https://aegwebprod.blob.core.windows.net/json/events/219/events.json", name="Roadrunner"))
            kept = build.read_jsonld(source("jsonld", "https://bostonfilmhub.com/#venue=Somerville+Theatre", "film", "Theatre via Hub"))
        sources = [dict(source("aeg", "x", name="Roadrunner"), public=True),
                   dict(source("jsonld", "y", "film", "Theatre via Hub"), public=False)]
        built = datetime.now(timezone.utc)
        public = build.render_index(found + kept, sources, [], [], built, public=True)
        mine = build.render_index(found + kept, sources, [], [], built)
        self.assertNotIn("Newsfeed", public)
        self.assertIn('<a href="/about.html">About</a> · <a href="/contact.html">Contact</a>', public.split("<footer>")[1])
        self.assertNotIn('href="/about.html"', public.split("<footer>")[0], "not in the header")
        self.assertNotIn("Add a source", public)
        self.assertIn('content="index, follow"', public)
        self.assertIn('content="noindex, nofollow"', mine)
        self.assertNotIn("Theatre via Hub", public, "a source marked public=no stays off it")
        self.assertIn("Theatre via Hub", mine)
        listings = lambda page: page.split("</style>")[1].split("<footer>")[0]  # Not the styles, which differ.
        self.assertLess(len(listings(public)), len(listings(mine)), "shorter previews, and no Hub listings")
        about = build.render_about(sources, built)
        self.assertIn("Roadrunner", about)
        self.assertNotIn("Theatre via Hub", about)
        self.assertIn("https://formsubmit.co/", build.render_contact())
        self.assertIn('<a href="/contact.html">Contact</a>', about.split("<footer>")[1])

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
        self.assertIn('<a data-show="film" href="/film/">', home)
        self.assertIn('<a data-show="all" href="/" aria-current="page">All</a>', home)
        self.assertIn('data-current="film"', film)
        self.assertIn(f'<link rel="canonical" href="{build.PUBLIC_URL}film/">', film)
        self.assertIn(f"<title>{build.PUBLIC_PAGES['film'][1]}</title>", film)
        # The film page has only films, and says which sources they're from.
        self.assertEqual(film.count('class="row" data-category="music"'), 0)
        self.assertGreater(film.count('data-category="film"><'), 0)
        self.assertNotIn("Roadrunner", film.split("<footer>")[1])
        # Your own page keeps its buttons.
        self.assertIn('<button data-show="film">', mine)
        self.assertIn('<nav class="filter" aria-label="Show"><button', mine)
        self.assertIn(f"<loc>{build.PUBLIC_URL}talks/</loc>", build.render_sitemap(built))

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

    def test_sitemap(self):
        sitemap = build.render_sitemap(datetime(2026, 9, 13, tzinfo=timezone.utc))
        for path in ("", "about.html", "contact.html"):
            self.assertIn(f"<loc>{build.PUBLIC_URL}{path}</loc>", sitemap)

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
