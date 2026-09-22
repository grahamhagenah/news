"""Tests for housing/build.py, against saved samples of the cities' data (tests/fixtures/housing): a few real
projects from each, so they run offline."""

import sys
import unittest
import unittest.mock
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from housing import build, upstream  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "housing"


class Readers(unittest.TestCase):
    def test_boston(self):
        projects = {p["id"]: p for p in build.boston((FIXTURES / "boston.json").read_bytes())}
        self.assertEqual(projects["boston-4930"]["status"], "approved")
        self.assertEqual(projects["boston-3200"]["status"], "under construction")
        self.assertEqual(projects["boston-3398"]["status"], "proposed")
        self.assertEqual(projects["boston-3195"]["dated"], date(2024, 4, 11))
        self.assertEqual(projects["boston-3195"]["units"], 55)
        facts = dict(projects["boston-3195"]["facts"])
        self.assertEqual(facts["Approved"], "Apr 11, 2024")
        self.assertEqual(facts["Cost"], "$25M")
        self.assertEqual(facts["Address"], "267 Old Colony Avenue")
        # Finished before COMPLETE_SINCE: history, not news.
        self.assertNotIn("boston-114", projects)

    def test_cambridge(self):
        projects = {p["id"]: p for p in build.cambridge((FIXTURES / "cambridge.json").read_bytes())}
        self.assertEqual(set(projects), {"cambridge-844", "cambridge-802", "cambridge-823"})  # 800 is labs, no homes.
        self.assertEqual(projects["cambridge-802"]["status"], "under construction")
        self.assertEqual(projects["cambridge-823"]["status"], "proposed")
        self.assertNotRegex(projects["cambridge-802"]["neighborhood"], r"^\d")

    def test_massbuilds(self):
        projects = {p["id"]: p for p in build.massbuilds((FIXTURES / "massbuilds.json").read_bytes())}
        # 1135 was finished in 2007, 140 has no homes, and 99999 ended with two fewer than it began with.
        self.assertEqual(set(projects), {"massbuilds-1549", "massbuilds-6582", "massbuilds-135", "massbuilds-6126",
                                         "massbuilds-3476"})
        self.assertEqual(projects["massbuilds-1549"]["status"], "proposed")
        self.assertEqual(projects["massbuilds-6582"]["status"], "under construction")
        self.assertEqual(projects["massbuilds-6126"]["status"], "complete")
        self.assertEqual(projects["massbuilds-3476"]["status"], "stalled")
        self.assertEqual(projects["massbuilds-1549"]["town"], "Somerville")
        self.assertEqual(projects["massbuilds-1549"]["link"], "https://www.massbuilds.com/map/developments/1549")
        self.assertIsInstance(projects["massbuilds-1549"]["dated"], date)


def project(ident="boston-1", status="approved", **fields):
    return dict({"id": ident, "town": "Boston", "neighborhood": "Roxbury", "name": "1 Main Street", "units": 40,
                 "status": status, "stage": "", "lat": 42.3, "lon": -71.1, "link": "", "description": "",
                 "dated": date(2025, 1, 1)}, **fields)


class Edits(unittest.TestCase):
    def test_changes_and_additions(self):
        edits = build.read_edits(
            "# a comment\n\nboston-1\nstatus: Stalled\nnote: Financing fell through.\ndate: 2026-09-01\n\n"
            "somerville-1\nname: Union Square D2\ntown: Somerville\nunits: 450\nstatus: approved\nlat: 42.38\nlon: -71.09\n\n"
            "boston-2\nhide: yes\n\n"
            "somerville-2\nname: Missing where it is\ntown: Somerville\nunits: 10\nstatus: proposed\n"
        )
        projects = {p["id"]: p for p in build.apply_edits([project(), project("boston-2")], edits)}
        self.assertEqual(set(projects), {"boston-1", "somerville-1"})
        self.assertEqual(projects["boston-1"]["status"], "stalled")
        self.assertEqual(projects["boston-1"]["note"], "Financing fell through.")
        self.assertEqual(build.updated(projects["boston-1"]), date(2026, 9, 1))
        self.assertEqual(projects["somerville-1"]["units"], 450)


class Pictures(unittest.TestCase):
    def test_boston_rendering_from_its_page(self):
        page = """<aside class="bpdaInteriorHeaderImg">
    <img src='/getattachment/0d1fcada-2bb9-4dcf-8d3d-e4e93c581492/' alt='' />"""
        found = build.BOSTON_IMAGE.search(page)
        self.assertEqual(found.group(1), "/getattachment/0d1fcada-2bb9-4dcf-8d3d-e4e93c581492/")

    def test_only_new_projects_are_looked_up(self):
        projects = [project("boston-1", link="https://example.com/1"), project("boston-2", link="https://example.com/2"),
                    project("cambridge-1", link="https://example.com/c")]
        looked = []
        with unittest.mock.patch.object(build, "boston_image", lambda link: looked.append(link) or "https://img/2"):
            images = build.add_images(projects, {"boston-1": "https://img/1"})
        self.assertEqual(looked, ["https://example.com/2"])
        self.assertEqual(images, {"boston-1": "https://img/1", "boston-2": "https://img/2"})
        self.assertEqual([p["image"] for p in projects], ["https://img/1", "https://img/2", ""])


class Tracking(unittest.TestCase):
    def test_first_build_dates_nothing(self):
        projects = [project()]
        build.track(projects, {}, date(2026, 9, 21))
        self.assertIsNone(projects[0]["since"])

    def test_a_change_or_a_new_project_is_dated_today(self):
        previous = {"projects": {"boston-1": {"status": "proposed", "since": None},
                                 "boston-2": {"status": "approved", "since": "2026-01-05"}}}
        projects = [project(), project("boston-2"), project("boston-3")]
        record = build.track(projects, previous, date(2026, 9, 21))
        self.assertEqual([p["since"] for p in projects], [date(2026, 9, 21), date(2026, 1, 5), date(2026, 9, 21)])
        self.assertEqual(record["boston-1"], {"status": "approved", "since": "2026-09-21", "was": "proposed"})
        self.assertEqual(record["boston-3"]["was"], "")  # New since the last build.
        self.assertIsNone(record["boston-2"]["was"])


class Recent(unittest.TestCase):
    TODAY = date(2026, 9, 21)

    def changed(self, **fields):
        return build.change(dict(project(), **dict({"since": None, "was": None}, **fields)), self.TODAY)

    def test_a_status_seen_to_change_says_from_what(self):
        self.assertEqual(self.changed(since=date(2026, 9, 20), was="proposed"), (date(2026, 9, 20), "Proposed → Approved"))
        self.assertEqual(self.changed(since=date(2026, 9, 20), was="")[1], "Newly listed")

    def test_boston_dates_and_notes(self):
        self.assertEqual(self.changed(approved="2026-09-01", filed="2026-07-01")[1], "Board approved")
        self.assertEqual(self.changed(edited=date(2026, 9, 10), note="Broke ground.")[1], "Broke ground.")

    def test_nothing_older_than_the_window(self):
        self.assertIsNone(self.changed(approved="2026-01-01", dated=date(2026, 1, 1)))

    def test_recent_page_lists_only_whats_changed_newest_first(self):
        projects = [dict(project("boston-1"), since=None, was=None, approved="2026-09-01"),
                    dict(project("boston-2"), since=date(2026, 9, 20), was="proposed"),
                    dict(project("boston-3"), since=None, was=None)]
        built = datetime(2026, 9, 21, tzinfo=timezone.utc)
        page = build.render(projects, built, [], page="recent")
        self.assertLess(page.index('id="boston-2"'), page.index('id="boston-1"'))
        self.assertNotIn('id="boston-3"', page)
        self.assertIn("Proposed → Approved", page)
        home = build.render(projects, built, [])
        self.assertIn('class="fresh"', home)
        self.assertIn('href="recent/"', home)

    def test_large_projects_biggest_first_with_their_steps(self):
        built = datetime(2026, 9, 21, tzinfo=timezone.utc)
        projects = [dict(project("boston-1", units=300, status="under construction"), since=None, was=None),
                    dict(project("boston-2", units=1200, status="stalled"), since=None, was=None),
                    dict(project("boston-3", units=40), since=None, was=None)]
        page = build.render(projects, built, [], page="large")
        self.assertLess(page.index('id="boston-2"'), page.index('id="boston-1"'))
        self.assertNotIn('id="boston-3"', page)
        self.assertIn('aria-label="Under construction, step 3 of 4"', page)
        self.assertIn('aria-label="Stalled, step 0 of 4"', page)

    def test_footer_links_each_town_and_page(self):
        built = datetime(2026, 9, 21, tzinfo=timezone.utc)
        projects = [dict(project("boston-1"), since=None, was=None),
                    dict(project("massbuilds-1", town="Somerville"), since=None, was=None)]
        home = build.render(projects, built, [])
        self.assertIn('id="town-somerville"', home)
        self.assertIn('href="somerville/"', home)
        large = build.render(projects, built, [], page="large")
        self.assertIn('href="../somerville/"', large)
        self.assertIn('href="../large/" aria-current="page"', large)

    def test_town_page_has_only_its_towns_projects(self):
        built = datetime(2026, 9, 21, tzinfo=timezone.utc)
        projects = [dict(project("boston-1"), since=None, was=None),
                    dict(project("massbuilds-1", town="Somerville"), since=None, was=None)]
        page = build.render(projects, built, [], page="town", town="Somerville")
        self.assertIn('id="massbuilds-1"', page)
        self.assertNotIn('id="boston-1"', page)
        self.assertIn("<title>New housing in Somerville", page)
        self.assertIn('href="?project=massbuilds-1"', page)  # Its panel, on this page.
        self.assertIn('<link rel="canonical" href="https://housing.grahamhagenah.com/somerville/">', page)
        self.assertIn('content="index, follow"', page)


class Sitemap(unittest.TestCase):
    def test_sitemap_lists_every_page(self):
        built = datetime(2026, 9, 21, tzinfo=timezone.utc)
        projects = [dict(project("boston-1"), since=None, was=None)]
        xml = build.sitemap(projects, ["Boston"], built)
        for path in ("", "recent/", "large/", "boston/"):
            self.assertIn(f"<loc>https://housing.grahamhagenah.com/{path}</loc>", xml)
        self.assertNotIn("project", xml)  # A project's panel has no page of its own.


class Page(unittest.TestCase):
    def test_rows_sorted_newest_first_within_their_town(self):
        projects = [project("boston-1", dated=date(2024, 1, 1)), project("boston-2", dated=date(2026, 8, 1)),
                    project("cambridge-1", town="Cambridge")]
        for p in projects:
            p["since"] = None
        page = build.render(projects, datetime(2026, 9, 21, tzinfo=timezone.utc), [])
        self.assertLess(page.index('id="boston-2"'), page.index('id="boston-1"'))
        self.assertLess(page.index("<span class=\"town\">Boston"), page.index("<span class=\"town\">Cambridge"))
        self.assertIn("Aug 1", page)
        self.assertIn("Jan 2024", page)

    def test_panel_data_is_on_the_page_and_cant_close_its_script(self):
        projects = [project("boston-1", description="Ends </script> here", facts=[["Cost", "$5M"]])]
        projects[0]["since"] = None
        page = build.render(projects, datetime(2026, 9, 21, tzinfo=timezone.utc), [])
        data = page.split('<script type="application/json" id="projects">')[1].split("</script>")[0]
        self.assertIn('"Cost"', data)
        self.assertIn("<\\/script>", data)
        self.assertIn('href="?project=boston-1"', page)  # Its panel's address.


class Upstream(unittest.TestCase):
    RECORDS = {
        "1": {"name": "3 Hawkins St", "municipal": "Somerville", "status": "in_construction", "stalled": False,
              "latitude": 42.4, "longitude": -71.1, "year_compl": 2025, "hu": 59, "commsf": 0, "descr": "Homes."},
        "2": {"name": "Albion St", "municipal": "Somerville", "status": "planning", "stalled": False,
              "latitude": 42.4, "longitude": -71.1, "year_compl": None, "hu": 40, "commsf": 0, "descr": "Homes."},
        "3": {"name": "Matches", "municipal": "Medford", "status": "planning", "stalled": False},
    }

    def report(self, text):
        return "\n".join(upstream.report(build.read_edits(text), fetch_record=self.RECORDS.__getitem__))

    def test_finished_project_is_an_edit_with_its_year(self):
        text = self.report("massbuilds-1\nstatus: complete\ndate: 2026-08-01\nnote: Opened in August.")
        self.assertIn("status: 'in_construction' → 'completed'", text)
        self.assertIn("year_compl: 2025 → 2026", text)
        self.assertIn("As an edit", text)
        self.assertIn("Flag: Opened in August.", text)

    def test_a_record_an_edit_would_fail_on_goes_in_a_flag(self):
        text = self.report("massbuilds-2\nstatus: stalled")
        self.assertIn("stalled: False → True", text)
        self.assertIn("As a flag", text)
        self.assertIn("year_compl", text)

    def test_nothing_to_send_when_they_agree(self):
        self.assertIn("Nothing to send upstream", self.report("massbuilds-3\nstatus: proposed"))

    def test_our_own_project_in_a_massbuilds_town(self):
        text = self.report("somerville-1\nname: D2\ntown: Somerville\nunits: 450\nstatus: approved\nlat: 42.38\nlon: -71.09")
        self.assertIn("New: D2 (Somerville)", text)
        self.assertIn("Also needs: year_compl, commsf, descr", text)


if __name__ == "__main__":
    unittest.main()
