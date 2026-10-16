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

    def test_leaves_out_the_rest(self):
        for title in ("Registered Nurse", "Warehouse Associate", "Account Executive, Enterprise Sales",
                      "Staff Accountant"):
            self.assertFalse(self.wanted(title), title)


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
        self.assertIn("$150K–$190K", page)
        # The menu names only the cities with jobs on the page, in the order CITIES gives.
        menu = page.split('<select class="pick" aria-label="City">')[1].split("</select>")[0]
        self.assertEqual(re.findall(r'<option value="([^"]*)">', menu), ["", "boston", "newyork", "remote"])
        self.assertIn('data-show="design"', page)
        self.assertIn('<nav class="pager">', page)

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
