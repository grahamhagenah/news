"""Tests for jobs/build.py, against saved samples of the real boards (tests/fixtures/jobs), so they run
offline and a change that breaks a reader fails here instead of on the live page. Run from the repo's top
folder with: python3 -m unittest

The samples are real pages and feeds trimmed to a few jobs. When a board changes and its reader is updated
for it, save a fresh sample alongside the fix.
"""

import json
import re
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobs import build  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "jobs"
ROUTES = [
    ("builtinboston.com/jobs/", "builtin.html"),
    ("weworkremotely.com", "wwr.xml"),
    ("remoteok.com/api", "remoteok.json"),
    ("remotive.com/api", "remotive.json"),
    ("jobsrss", "madgex.xml"),
    ("techjobsforgood.com", "techjobs.html"),
    ("jobs.tufts.edu", "tufts.json"),
    ("jobs.silkroad.com", "bu.html"),
    ("/wday/cxs/", "workday.json"),
]


def sample(url, **kwargs):
    for pattern, name in ROUTES:
        if pattern in url:
            return (FIXTURES / name).read_text()
    raise AssertionError(f"no sample for {url}")


def source(kind, url, city="boston", name="A Board"):
    return {"kind": kind, "url": url, "city": city, "name": name}


class Readers(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(build, "fetch", sample)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, kind, url, city="boston"):
        found = build.READERS[kind](source(kind, url, city))
        self.assertTrue(found, f"{kind} read nothing from its sample")
        for listing in found:
            self.assertTrue(listing["title"].strip())
            self.assertTrue(listing["link"].startswith("http"), listing["link"])
            self.assertTrue(listing["company"].strip())
            self.assertTrue(listing["posted"] is None or isinstance(listing["posted"], date))
        return found

    def test_builtin_reads_a_card(self):
        # Both its listings pages come from the one sample, so each job is read twice here.
        found = self.read("builtin", "https://www.builtinboston.com/")
        job = found[0]
        self.assertTrue(job["link"].startswith("https://www.builtinboston.com/job/"))
        self.assertRegex(job["place"], r"^[A-Z][\w .'’-]+, [A-Z]{2}$")
        self.assertIn(job["arrangement"], ("Remote", "Hybrid", "In-Office", ""))
        self.assertTrue(any(listing["salary"] for listing in found), "a card says what it pays")
        self.assertTrue(any(listing["about"] for listing in found), "the page's own summary of the job")

    def test_builtin_ages(self):
        today = date(2026, 10, 16)
        self.assertEqual(build.builtin_age("Yesterday", today), date(2026, 10, 15))
        self.assertEqual(build.builtin_age("8 Days Ago", today), date(2026, 10, 8))
        # A posting put back up says so, and is as old as it says.
        self.assertEqual(build.builtin_age("Reposted 2 Days Ago", today), date(2026, 10, 14))
        self.assertEqual(build.builtin_age("3 Hours Ago", today), today)
        self.assertIsNone(build.builtin_age("Featured", today))

    def test_wwr_splits_company_from_role(self):
        found = self.read("wwr", "https://weworkremotely.com/categories/remote-programming-jobs.rss", "remote")
        # Its titles are "Company: Role".
        self.assertNotIn(":", found[0]["title"].split(" - ")[0][:30])
        self.assertTrue(all(listing["arrangement"] == "Remote" for listing in found))
        self.assertTrue(all(listing["place"] for listing in found))

    def test_remoteok_skips_its_terms(self):
        found = self.read("remoteok", "https://remoteok.com/api", "remote")
        self.assertTrue(all(listing["title"] for listing in found))
        self.assertTrue(all("remoteok.com" in listing["link"].lower() for listing in found))

    def test_remotive(self):
        found = self.read("remotive", "https://remotive.com/api/remote-jobs", "remote")
        self.assertTrue(all(listing["arrangement"] == "Remote" for listing in found))

    def test_madgex_splits_the_institution_from_the_role(self):
        found = self.read("madgex", "https://careers.insidehighered.com/", "anywhere")
        # Its titles are "Institution: Role", and the pay and tracking it adds stay out of both.
        self.assertNotIn(":", found[0]["title"])
        self.assertNotIn("?", found[0]["link"])
        self.assertTrue(all(listing["place"] for listing in found), "the place its description ends with")
        self.assertTrue(any(listing["about"] for listing in found), "what the posting says")
        # Each job is filed by the place it names, not by the board.
        self.assertTrue(all(listing["city"] in build.CITIES for listing in found))

    def test_techjobs_reads_a_card(self):
        found = self.read("techjobs", "https://techjobsforgood.com/", "anywhere")
        job = found[0]
        self.assertTrue(job["link"].startswith("https://techjobsforgood.com/jobs/"))
        self.assertNotIn("?", job["link"])
        self.assertTrue(job["place"])
        self.assertIn(job["arrangement"], ("Remote", "Hybrid", ""))
        self.assertTrue(any(listing["salary"] for listing in found), "a card says what it pays")

    def test_techjobs_ages(self):
        today = date(2026, 10, 16)
        self.assertEqual(build.techjobs_age("1 day ago", today), date(2026, 10, 15))
        self.assertEqual(build.techjobs_age("3 weeks ago", today), date(2026, 9, 25))
        self.assertEqual(build.techjobs_age("2 hours ago", today), today)
        self.assertEqual(build.techjobs_age("today", today), today)
        self.assertIsNone(build.techjobs_age("", today))

    def test_jibe_reads_a_universitys_own_json(self):
        found = self.read("jibe", "https://jobs.tufts.edu/", "anywhere")
        job = found[0]
        self.assertTrue(job["link"].startswith("https://jobs.tufts.edu/jobs/"))
        self.assertEqual(job["company"], "Tufts University")
        self.assertEqual(job["city"], "boston", "Boston, Massachusetts, by the place it names")
        self.assertIn(job["arrangement"], ("Remote", "Hybrid", ""))
        self.assertTrue(job["posted"], "its date, whose offset this Python can't read in full")
        self.assertTrue(any(listing["about"] for listing in found))

    def test_silkroad_reads_a_panel(self):
        found = self.read("silkroad", "https://jobs.silkroad.com/BU/External", "anywhere")
        job = found[0]
        self.assertTrue(job["link"].startswith("https://jobs.silkroad.com/BU/External/jobs/"))
        self.assertEqual(job["company"], "A Board", "it never says, so the board's own name stands in")
        self.assertEqual(job["city"], "boston")
        # It shouts some of its towns; a row shouldn't.
        self.assertNotIn("BOSTON", " ".join(listing["place"] for listing in found))
        self.assertTrue(all(listing["posted"] is None for listing in found), "it carries no dates")

    def test_workday_reads_a_posting(self):
        found = self.read("workday", "https://northeastern.wd1.myworkdayjobs.com/careers", "anywhere")
        job = found[0]
        self.assertTrue(job["link"].startswith("https://northeastern.wd1.myworkdayjobs.com/careers/job/"))
        self.assertEqual(job["city"], "boston")
        self.assertTrue(job["posted"], '"Posted 30+ Days Ago" and the rest, as a day')

    def test_workday_api_from_either_kind_of_address(self):
        self.assertEqual(build.workday_api("https://northeastern.wd1.myworkdayjobs.com/careers"),
                         "https://northeastern.wd1.myworkdayjobs.com/wday/cxs/northeastern/careers/jobs")
        self.assertEqual(build.workday_api("https://wd1.myworkdaysite.com/recruiting/wellesley/wellesley-staff"),
                         "https://wd1.myworkdaysite.com/wday/cxs/wellesley/wellesley-staff/jobs")

    def test_salary_range(self):
        self.assertEqual(build.salary_range(90000, 120000), "$90K–$120K")
        self.assertEqual(build.salary_range(None, 120000), "$120K")
        self.assertEqual(build.salary_range(None, None), "")


class Keywords(unittest.TestCase):
    def setUp(self):
        self.keywords = build.read_keywords()

    def wanted(self, title):
        return build.wanted({"title": title}, self.keywords)

    def test_keeps_the_work_this_board_is_for(self):
        for title in ("Senior Frontend Engineer", "UX Designer", "Full-Stack Developer (React)",
                      "Web Producer", "Product Design Lead", "JavaScript Engineer II"):
            self.assertTrue(self.wanted(title), title)

    def test_keeps_the_work_next_to_it(self):
        # The wider keywords: titles none of the web words would have caught.
        for title in ("Group Product Manager", "Data Analyst, Marketplace", "Technical Writer",
                      "Developer Relations Lead", "Art Director", "QA Analyst", "Motion Graphics Artist",
                      "Webflow Specialist", "Digital Accessibility Coordinator", "SEO Manager"):
            self.assertTrue(self.wanted(title), title)

    def test_leaves_out_the_rest(self):
        # Including the titles that the wider keywords would have pulled in had they been single broad words.
        for title in ("Registered Nurse", "Warehouse Associate", "Account Executive, Enterprise Sales",
                      "Staff Accountant", "Operations Manager", "Technical Recruiter", "Content Moderator"):
            self.assertFalse(self.wanted(title), title)


class Metros(unittest.TestCase):
    """A nationwide board's own locations, filed under the metro they're in."""

    def test_a_state_written_out_in_full_is_the_same_state(self):
        # METROS gives an ambiguous town its state's short name; a board may write the state out.
        self.assertEqual(build.metro_of("Medford, Massachusetts"), "boston")
        self.assertEqual(build.metro_of("Cambridge, Massachusetts"), "boston")
        self.assertEqual(build.metro_of("New York, New York"), "newyork")
        # Not a state that only follows the town's own name: Washington, DC isn't Seattle.
        self.assertEqual(build.metro_of("Washington, DC"), "elsewhere")

    def test_a_town_in_a_metro_is_that_metro(self):
        self.assertEqual(build.metro_of("Cambridge, MA"), "boston")
        self.assertEqual(build.metro_of("Stanford, CA"), "bayarea")
        self.assertEqual(build.metro_of("Chicago, IL"), "chicago")
        self.assertEqual(build.metro_of("Brooklyn, NY"), "newyork")
        # Several places, the first metro among them winning; and a metro named beside remote work.
        self.assertEqual(build.metro_of("Medford, MA / Boston, MA / Remote (Select US States)"), "boston")
        self.assertEqual(build.metro_of("Remote (US)/Oakland, CA"), "bayarea")

    def test_nowhere_in_particular_is_remote(self):
        self.assertEqual(build.metro_of("Remote (US)"), "remote")
        self.assertEqual(build.metro_of("", "Remote"), "remote")
        self.assertEqual(build.metro_of("Anywhere in the World"), "remote")

    def test_everywhere_else_is_elsewhere(self):
        for place in ("Atlanta, Georgia", "Madison, WI", "Oxford", "", "Amherst, MA"):
            self.assertEqual(build.metro_of(place), "elsewhere", place)

    def test_a_row_names_the_place_rather_than_elsewhere(self):
        source = {"kind": "madgex", "url": "https://x/", "city": "anywhere", "name": "A Board"}
        listing = build.job(source, "Web Developer", "Georgia Tech", "https://x/1", date(2026, 10, 10),
                            place="Atlanta, Georgia")
        self.assertEqual(listing["city"], "elsewhere")
        self.assertEqual(build.where_of(listing), "Atlanta, Georgia")
        # A place that isn't one falls back to the city's own name.
        self.assertEqual(build.where_of(dict(listing, place="Main Campus")), "Elsewhere")
        # The country, how it's worked and every place after the first are more than the column can hold.
        self.assertEqual(build.where_of(dict(listing, place="Madison, Wisconsin, United States")),
                         "Madison, Wisconsin")
        self.assertEqual(build.where_of(dict(listing, place="San Diego, CA (Hybrid)")), "San Diego, CA")
        self.assertEqual(build.where_of(dict(listing, place="Durham, NC/Washington, DC")), "Durham, NC")


class Listing(unittest.TestCase):
    def job(self, title, company="Acme", city="boston", posted=date(2026, 10, 10), **rest):
        return dict(build.job(source("builtin", "https://x/", city), title, company, "https://x/job/1", posted),
                    **rest)

    def test_a_job_posted_to_every_metro_is_one_row_in_all_of_them(self):
        listings = [self.job("Staff Engineer", city=city) for city in ("boston", "newyork", "denver")]
        listings.append(self.job("Visual Designer", city="austin"))
        kept = build.without_repeats(listings)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0]["cities"], ["boston", "newyork", "denver"])
        self.assertEqual(kept[0]["place"], "3 cities", "where it is, rather than the first copy's city")
        self.assertEqual(kept[1]["cities"], ["austin"])

    def test_merging_keeps_what_one_copy_says_and_the_other_doesnt(self):
        listings = [self.job("Staff Engineer", city="boston", salary="", level=""),
                    self.job("Staff Engineer", city="denver", salary="$150K–$190K", level="Senior",
                             posted=date(2026, 10, 12))]
        kept = build.without_repeats(listings)
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["salary"], kept[0]["level"]), ("$150K–$190K", "Senior"))
        self.assertEqual(kept[0]["posted"], date(2026, 10, 12), "the newer sighting's day")

    def test_newest_first_with_undated_last(self):
        listings = [self.job("A", posted=None), self.job("B", posted=date(2026, 10, 1)),
                    self.job("C", posted=date(2026, 10, 9))]
        self.assertEqual([listing["title"] for listing in build.newest_first(listings)], ["C", "B", "A"])

    def test_design_and_engineering_are_told_apart(self):
        self.assertEqual(build.kind_of(self.job("Senior UX Researcher")), "design")
        self.assertEqual(build.kind_of(self.job("Staff Software Engineer")), "engineering")
        # Anything remote is filed as remote, whatever the work is.
        self.assertEqual(build.kind_of(self.job("Product Designer", city="remote")), "remote")

    def test_the_work_around_the_thing_is_its_own_kind(self):
        for title in ("Group Product Manager", "Data Analyst", "Technical Writer", "Developer Advocate",
                      "SEO Manager"):
            self.assertEqual(build.kind_of(self.job(title)), "product", title)
        # Design still wins over product, and testing is building it.
        self.assertEqual(build.kind_of(self.job("Principal Product Designer")), "design")
        self.assertEqual(build.kind_of(self.job("Art Director, Brand")), "design")
        self.assertEqual(build.kind_of(self.job("QA Engineer")), "engineering")


class Dates(unittest.TestCase):
    """Boards that don't date their openings, and boards whose openings stay up for months."""

    def source(self, kind, city="anywhere"):
        return {"kind": kind, "url": "https://x/", "city": city, "name": "A School"}

    def test_an_undated_job_is_dated_the_day_it_was_first_seen(self):
        built_at = datetime(2026, 10, 16, tzinfo=timezone.utc)
        listings = [build.job(self.source("silkroad"), "Web Developer", "", "https://x/jobs/1", None,
                              place="Boston, MA"),
                    build.job(self.source("silkroad"), "UX Designer", "", "https://x/jobs/2", None,
                              place="Boston, MA")]
        previous = {"sources": {"A School": {"fetched": built_at.isoformat(),
                                             "jobs": [{"link": "https://x/jobs/1", "posted": "2026-10-02"}]}}}
        dated = build.first_seen(listings, previous, built_at)
        self.assertEqual(dated[0]["posted"], date(2026, 10, 2), "the day the board first saw it")
        self.assertEqual(dated[1]["posted"], date(2026, 10, 16), "new to the board today")

    def test_a_university_board_is_read_further_back(self):
        built_at = datetime(2026, 10, 16, tzinfo=timezone.utc)
        long_ago = date(2026, 7, 20)

        def reader(source):
            return [build.job(source, "Web Developer", "A School", "https://x/1", long_ago)]

        keywords = build.read_keywords()
        with mock.patch.dict(build.READERS, {"madgex": reader, "builtin": reader}):
            slow, _, _ = build.collect([self.source("madgex")], keywords, built_at)
            quick, _, _ = build.collect([self.source("builtin", "boston")], keywords, built_at)
        self.assertEqual(len(slow[0][1]), 1, "a university's posting is usually still open months later")
        self.assertEqual(quick[0][1], [], "a metro board's isn't")


class Page(unittest.TestCase):
    def listings(self):
        make = lambda title, city, **rest: dict(
            build.job(source("builtin", "https://x/", city), title, "Acme", "https://x/job/1", date(2026, 10, 10)),
            **rest)
        return build.without_repeats([
            make("Senior Frontend Engineer", "boston", place="Boston, MA", salary="$150K–$190K",
                 arrangement="Hybrid", level="Senior"),
            make("Senior Frontend Engineer", "newyork"),
            make("Product Designer", "remote", place="Anywhere", arrangement="Remote"),
        ])

    def test_rows_carry_every_city_the_job_is_in(self):
        page = build.render_index(self.listings(), [], [], datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertIn('data-city="boston newyork"', page)
        self.assertIn('data-city="remote"', page)
        # What it pays and how it's worked stay out of the row, though the export keeps them.
        self.assertNotIn("$150K–$190K", page)
        self.assertNotIn("Hybrid", page)
        self.assertIn("Senior", page)
        # The company sits beside the title; the city is the row's last column.
        self.assertIn('<span class="company">Acme</span>', page)
        self.assertIn('<span class="source"><span>2 cities</span></span>', page)
        self.assertIn('<span class="source"><span>Remote</span></span>', page)
        self.assertNotIn("Boston, MA", page, "the city column already says where it is")
        # The menu names only the cities with jobs on the page, in the order CITIES gives.
        menu = page.split('<select class="pick" aria-label="City">')[1].split("</select>")[0]
        self.assertEqual(re.findall(r'<option value="([^"]*)">', menu), ["", "boston", "newyork", "remote"])
        self.assertIn('data-show="design"', page)
        self.assertIn('<nav class="pager">', page)

    def test_the_product_filter_shows_only_when_such_a_job_is_on_the_page(self):
        page = build.render_index(self.listings(), [], [], datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertNotIn('<button data-show="product">', page)  # The CSS names every kind; the filter doesn't.
        listings = self.listings() + [build.job(source("builtin", "https://x/", "boston"), "Group Product Manager",
                                                "Acme", "https://x/job/2", date(2026, 10, 9))]
        page = build.render_index(build.without_repeats(listings), [], [],
                                  datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertIn('<button data-show="product">', page)
        self.assertIn('data-kind="product"', page)

    def test_a_row_that_cant_name_a_town_says_only_elsewhere(self):
        source = {"kind": "madgex", "url": "https://x/", "city": "anywhere", "name": "A Board"}
        vague = build.job(source, "Web Developer", "A School", "https://x/1", date(2026, 10, 10),
                          place="Texas, United States")
        named = build.job(source, "UX Designer", "A School", "https://x/2", date(2026, 10, 10),
                          place="Conway, SC")
        page = build.render_index(build.without_repeats([vague, named]), [], [],
                                  datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertNotIn("Texas, United States", page, "the column already says it isn't a town we cover")
        self.assertIn('<span class="source"><span>Elsewhere</span></span>', page)
        # A place that is a town still names itself, at the end of the row and not beside the title.
        self.assertIn('<span class="source"><span>Conway, SC</span></span>', page)
        self.assertNotIn('<span class="facts">Conway, SC</span>', page)

    def test_a_place_beside_the_title_is_shortened_too(self):
        source = {"kind": "madgex", "url": "https://x/", "city": "anywhere", "name": "A Board"}
        # Boulder is in the Denver metro, so the row says where it is — but not what country.
        listing = build.job(source, "Graphic Designer", "A School", "https://x/1", date(2026, 10, 10),
                            place="Boulder, Colorado, United States")
        page = build.render_index(build.without_repeats([listing]), [], [],
                                  datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertIn('<span class="facts">Boulder, Colorado</span>', page)
        self.assertIn('<span class="source"><span>Denver</span></span>', page)

    def test_a_board_that_failed_says_so(self):
        page = build.render_index(self.listings(), ["Built In Boston"], [("Remote OK", date(2026, 10, 15))],
                                  datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertIn("Couldn’t load Built In Boston", page)
        self.assertIn("Couldn’t reach Remote OK", page)

    def test_json_export(self):
        data = json.loads(build.render_json(self.listings(), datetime(2026, 10, 16, tzinfo=timezone.utc)))
        self.assertEqual(len(data["jobs"]), 2)
        self.assertEqual(data["jobs"][0]["posted"], "2026-10-10")


class Fallback(unittest.TestCase):
    def test_a_board_that_fails_shows_its_last_good_jobs(self):
        built_at = datetime(2026, 10, 16, tzinfo=timezone.utc)
        listing = build.job(source("remoteok", "https://remoteok.com/api", "remote", "Remote OK"),
                            "Frontend Engineer", "Acme", "https://remoteok.com/1", date(2026, 10, 15))
        previous = {"sources": {"Remote OK": {"fetched": (built_at - timedelta(hours=3)).isoformat(),
                                              "jobs": [build.saved(listing)]}}}
        listings, failed, stale = build.gather([], ["Remote OK"], previous, built_at)
        self.assertEqual([item["title"] for item in listings], ["Frontend Engineer"])
        self.assertEqual(listings[0]["posted"], date(2026, 10, 15), "its day survives the round trip")
        self.assertEqual(failed, [])
        self.assertEqual([name for name, _ in stale], ["Remote OK"])

    def test_an_old_copy_is_no_use(self):
        built_at = datetime(2026, 10, 16, tzinfo=timezone.utc)
        previous = {"sources": {"Remote OK": {"fetched": (built_at - timedelta(days=5)).isoformat(), "jobs": []}}}
        listings, failed, stale = build.gather([], ["Remote OK"], previous, built_at)
        self.assertEqual((listings, failed, stale), ([], ["Remote OK"], []))


if __name__ == "__main__":
    unittest.main()
