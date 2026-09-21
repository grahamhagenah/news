"""Tests for housing/build.py, against saved samples of the cities' data (tests/fixtures/housing): a few real
projects from each, so they run offline."""

import sys
import unittest
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
        # 1135 was finished in 2007 and 140 has no homes.
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
        self.assertEqual(record["boston-1"], {"status": "approved", "since": "2026-09-21"})


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
