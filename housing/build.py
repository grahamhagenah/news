"""The housing tracker: new housing being proposed, approved and built around Boston, from the cities' own lists
of development projects, with our own changes and additions on top (edits.txt). Each build compares the cities'
statuses with the last build's, so a project moving on (approved, under construction) comes to the top of its
list on the day it's seen to.

Run from the repo's top folder: python3 -m housing.build, which writes dist/housing."""

import html
import json
import os
import re
import shutil
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import shared.site as shared

ROOT = Path(__file__).parent
OUT_DIR = ROOT.parent / "dist" / "housing"
EDITS = ROOT / "edits.txt"
USER_AGENT = "Mozilla/5.0 (compatible; housing-tracker/1.0)"
SITE_URL = "https://buildhousing.org/"
REPO_URL = "https://github.com/grahamhagenah/news"  # Where a correction's issue is opened, and where this lives.
NAME = "Build Housing"
# The city the site covers, which the header names after it as it names a page ("Build Housing / Boston").
PLACE = "Boston"
# The site's mark, before its name in the header and in its icons: Tabler Icons' "building-community" (MIT).
MARK = ('<svg class="mark" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M8 9l5 5v7h-5v-4m0 4h-5v-7l5 -5m1 1v-6a1 1 0 0 1 1 -1h10a1 1 0 0 1 1 1v17h-8"/>'
        '<path d="M13 7l0 .01"/><path d="M17 7l0 .01"/><path d="M17 11l0 .01"/><path d="M17 15l0 .01"/></g></svg>')

TAGLINE = "A tracker for new housing projects around Boston, from proposal to move-in."
# The last build's record, kept beside the site: each project's status, and where to find its picture.
PROJECTS_URL = SITE_URL + "projects.json"

# Boston's projects under Article 80 review, the city's review of anything over about 20,000 square feet or
# 15 homes, with their status, homes and dates: the Planning Department's own map of them, which the city's
# open data (data.boston.gov) is a copy of. It answers up to 2,000 at a time; there are about 1,000.
BOSTON_URL = ("https://gis.bostonplans.org/hosting/rest/services/Hosted/A80_project_points_display/FeatureServer/0/"
              "query?where=total_residential_units%3E0&outFields=*&returnGeometry=false&f=json")
# Cambridge's development log, published each quarter: projects of 50,000 square feet or 10 homes or more,
# from the time they're proposed until they're finished. It has no dates of its own besides the year one was.
CAMBRIDGE_URL = "https://data.cambridgema.gov/resource/wjwg-93qh.json?$limit=5000"
CAMBRIDGE_PAGE = "https://www.cambridgema.gov/CDD/factsandmaps/developmentlog"

# Large projects: those with at least this many homes, across every town.
LARGE_HOMES = 300
# A project's way from proposal to move-in, for Large projects' bar of four steps. Stalled isn't a step.
STAGES = ["proposed", "approved", "under construction", "complete"]

# How far back Recent updates goes, and how many the home page's banner turns over. The cities refresh their
# lists every few weeks or months, so a month can go by with nothing new in them.
RECENT_DAYS = 90
BANNER_LINES = 3  # How many each banner shows, of either kind.

# Finished projects are kept for a few years, as what's recently been built; before that they're history.
COMPLETE_SINCE = 2020

# Where each project is along the way, in order, with the dot it's shown by on the map.
STATUSES = {
    "proposed": ("Proposed", "#adadad"),
    "approved": ("Approved", "#e0a93b"),
    "under construction": ("Under construction", "#4c9be8"),
    "complete": ("Complete", "#55b86a"),
    "stalled": ("Stalled", "#d65a50"),
}

# Each filter's mark, drawn in 16×16 strokes like the other pages' icons, and in its status's color where it has
# one: a half-filled circle for everything in progress, a filed page for proposed, a check for approved, a traffic
# cone for under construction, a house for complete, a pause for stalled, and four squares for all.
ICON_DRAWINGS = {
    "active": '<circle cx="8" cy="8" r="6.25"/><path d="M8 1.75a6.25 6.25 0 0 1 0 12.5z" fill="currentColor" stroke="none"/>',
    "proposed": '<path d="M9.25 1.75H4.5A1.5 1.5 0 0 0 3 3.25v9.5a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5V5.5z"/>'
                '<path d="M9.25 1.75V5.5H13M5.75 8.5h4.5M5.75 11h2.75"/>',
    "approved": '<circle cx="8" cy="8" r="6.25"/><path d="M5.25 8.25l2 2 3.5-4"/>',
    "under-construction": '<path d="M6.5 2.25h3l3.25 10.5h-9.5z"/><path d="M5.35 6.5h5.3M4.55 9.25h6.9"/>'
                          '<path d="M1.75 13.75h12.5"/>',
    "complete": '<path d="M2.25 7.5L8 2.5l5.75 5"/><path d="M3.75 6.25v7.5h8.5v-7.5"/><path d="M6.75 13.75v-3.5h2.5v3.5"/>',
    "stalled": '<circle cx="8" cy="8" r="6.25"/><path d="M6.5 5.75v4.5M9.5 5.75v4.5"/>',
    "all": '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/>'
           '<rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
    # Have your say: a comment period is something to write into, a meeting a day to turn up on.
    "comment": '<path d="M13.75 8.25c0 2.62-2.58 4.75-5.75 4.75a7 7 0 0 1-1.63-.19l-3.37 1.44 1-2.66'
               'a4.4 4.4 0 0 1-1.75-3.34c0-2.62 2.58-4.75 5.75-4.75s5.75 2.13 5.75 4.75z"/>',
    "meeting": '<rect x="2.25" y="3.5" width="11.5" height="10.25" rx="1.5"/>'
               '<path d="M2.25 6.75h11.5M5.5 1.75v3M10.5 1.75v3"/>',
}
ICON_SYMBOLS = shared.icon_symbols(ICON_DRAWINGS)

BOSTON_STATUSES = {
    "Prefile (Default)": "proposed",
    "Letter of Intent": "proposed",
    "Under Review": "proposed",
    "Board Approved": "approved",
    "Permitted / Under Construction": "under construction",
    "Construction Complete": "complete",
}
CAMBRIDGE_STATUSES = {
    "Pre-Permitting": "proposed",
    "Permitting": "proposed",
    "Design Review": "proposed",
    "Zoning Permit Granted or As of Right": "approved",
    "Approved PUD/Master Plan Development Remaining": "approved",
    "Building Permit Granted": "under construction",
    "Complete": "complete",
}


def number(text):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def iso_date(text):
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def long_date(day):
    return f"{day:%b} {day.day}, {day.year}" if day else ""


def money(amount):
    """Dollars as a panel shows them: $25M, $1.2B."""
    if not amount:
        return ""
    if amount >= 1e9:
        return f"${amount / 1e9:.1f}B".replace(".0B", "B")
    return f"${amount / 1e6:.1f}M".replace(".0M", "M") if amount >= 1e6 else f"${amount:,.0f}"


def facts(*pairs):
    """A project's details for its panel, as [label, value], leaving out the ones it doesn't have."""
    return [[label, str(value)] for label, value in pairs if value not in (None, "", 0, "0")]


def boston(body):
    """Boston's projects with homes in them."""
    projects = []
    for feature in json.loads(body)["features"]:
        row = {key: "" if value is None else value for key, value in feature["attributes"].items()}
        units, status = number(row["total_residential_units"]), BOSTON_STATUSES.get(row["project_status"])
        if units <= 0 or not status:  # MassBuilds counts homes lost as negative; so might anyone.
            continue
        dates = [d for d in (iso_date(row["last_filed_date"]), iso_date(row["last_board_approved_date"])) if d]
        dated = max(dates, default=None)
        if status == "complete" and (not dated or dated.year < COMPLETE_SINCE):
            continue
        projects.append({
            "id": f"boston-{row['project_id']}",
            "town": "Boston",
            "neighborhood": str(row["neighborhood"]).strip(),
            "name": str(row["project__project_name"]).strip(),
            "units": units,
            "status": status,
            "stage": row["project_status"],
            "lat": float(row["latitude"]), "lon": float(row["longitude"]),
            "link": str(row["website_url"]).strip(),
            "origin": "Boston Planning",
            "filed": str(row["last_filed_date"])[:10], "approved": str(row["last_board_approved_date"])[:10],
            "description": str(row["description"]).replace("\r", "").strip(),
            "dated": dated,
            "facts": facts(
                ("Address", " ".join(str(row[k]).strip() for k in ("project_street_number", "project_street_name",
                                                                   "project_street_suffix") if str(row[k]).strip())),
                ("Filed", long_date(iso_date(row["last_filed_date"]))),
                ("Approved", long_date(iso_date(row["last_board_approved_date"]))),
                ("Review", str(row["project__record_type"]).replace("Project", "project review")),
                ("Floor area", f'{number(row["gross_square_footage"]):,} sq ft' if number(row["gross_square_footage"]) else ""),
                ("Cost", money(number(row["total_development_cost"]))),
            ),
        })
    return projects


def cambridge(body):
    """Cambridge's projects with homes in them."""
    projects = []
    for row in json.loads(body):
        units, status = number(row.get("residential_units")), CAMBRIDGE_STATUSES.get(row.get("status"))
        if units <= 0 or not status or not row.get("latitude"):
            continue
        finished = number(row.get("year_complete"))
        if status == "complete" and finished and finished < COMPLETE_SINCE:
            continue
        projects.append({
            "id": f"cambridge-{row['project_id']}",
            "town": "Cambridge",
            # "11 - North Cambridge"
            "neighborhood": re.sub(r"^\d+\s*-\s*", "", row.get("neighborhood") or "").strip(),
            "name": (row.get("project_name") or row.get("address") or "").strip(),
            "units": units,
            "status": status,
            "stage": row["status"],
            "lat": float(row["latitude"]), "lon": float(row["longitude"]),
            "link": CAMBRIDGE_PAGE,
            "origin": "Cambridge log",
            "case": re.sub(r"[^A-Z0-9]", "", (row.get("planning_board_special_permit") or "").upper()),
            "description": (row.get("project_description") or "").strip(),
            "dated": date(finished, 12, 31) if status == "complete" and finished else None,
            "facts": facts(
                ("Address", (row.get("address") or "").strip()),
                ("Developer", (row.get("developer") or "").strip()),
                ("Income-restricted", number(row.get("affordable_units"))),
                ("Floor area", f'{number(row.get("total_gfa")):,} sq ft' if number(row.get("total_gfa")) else ""),
                ("Permit", (row.get("permit_type") or "").strip()),
                ("Stage", row["status"]),
                ("Finished", finished if status == "complete" else ""),
            ),
        })
    return projects


# MassBuilds, MAPC's database of development around the region, for the towns that don't publish a list of
# their own. MAPC's staff and the towns' planners keep it up, some more often than others, so each town's list
# says when its data was last touched. Boston's and Cambridge's own lists are fresher than their MassBuilds
# entries, which are copied from them.
MASSBUILDS_URL = "https://api.massbuilds.com/developments.jsonapi?municipal={}"
MASSBUILDS_PAGE = "https://www.massbuilds.com/map/developments/{}"
INNER_RING = ["Somerville", "Brookline", "Medford", "Malden", "Everett", "Chelsea", "Revere", "Arlington",
              "Watertown", "Newton", "Quincy"]
# "planning" is anything from its first filing until it breaks ground, approved or not.
MASSBUILDS_STATUSES = {
    "projected": "proposed",
    "planning": "proposed",
    "in_construction": "under construction",
    "completed": "complete",
}


def massbuilds(body):
    """A town's projects with homes in them, from MassBuilds."""
    projects = []
    for item in json.loads(body)["data"]:
        row = item["attributes"]
        units, status = number(row.get("hu")), MASSBUILDS_STATUSES.get(row.get("status"))
        # A project that ends with fewer homes than it began with (two flats made one) has a negative count.
        if units <= 0 or not status or row.get("latitude") is None:
            continue
        finished = number(row.get("year_compl"))
        if status == "complete" and (not finished or finished < COMPLETE_SINCE):
            continue
        if row.get("stalled") and status != "complete":
            status = "stalled"
        estimated = " (estimated)" if row.get("yrcomp_est") else ""
        projects.append({
            "id": f"massbuilds-{row['id']}",
            "town": row["municipal"],
            "neighborhood": (row.get("nhood") or "").strip(),
            "name": (row.get("name") or row.get("address") or "").strip(),
            "units": units,
            "status": status,
            "stage": row["status"],
            "lat": float(row["latitude"]), "lon": float(row["longitude"]),
            "link": MASSBUILDS_PAGE.format(row["id"]),
            "origin": "MassBuilds",
            "site": (row.get("prj_url") or "").strip(),
            "description": (row.get("descr") or "").strip(),
            "dated": iso_date(row.get("updated_at")),
            "facts": facts(
                ("Address", (row.get("address") or "").strip()),
                ("Developer", (row.get("devlper") or "").strip()),
                ("Income-restricted", number(row.get("affrd_unit"))),
                ("For 55 and over", "Yes" if row.get("ovr55") else ""),
                ("Commercial space", f'{number(row.get("commsf")):,} sq ft' if number(row.get("commsf")) else ""),
                ("Stories", number(row.get("stories"))),
                ("Cost", money(number(row.get("total_cost")))),
                ("Finished" if status == "complete" else "Expected", f"{finished}{estimated}" if finished else ""),
                ("Near", ", ".join((row.get("n_transit") or [])[:4])),
            ),
        })
    return projects


# Each town: its name, where its data is, and the reader for it.
SOURCES = [("Boston", BOSTON_URL, boston), ("Cambridge", CAMBRIDGE_URL, cambridge)] + [
    (town, MASSBUILDS_URL.format(town), massbuilds) for town in INNER_RING
]
FROM = {"Boston": "Boston’s Planning Department", "Cambridge": "Cambridge’s development log"}
# What each city's list takes in, which is also what it leaves out: a reader looking for a small building on
# their own street should be told why it isn't here, beside the list that hasn't got it.
LISTS = {"Boston": "projects of about 15 homes or 20,000 square feet and up",
         "Cambridge": "projects of 10 homes or 50,000 square feet and up"}


def read_edits(text):
    """edits.txt as {id: {field: value}}, in the order written."""
    edits = {}
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip() and not line.strip().startswith("#")]
        if not lines:
            continue
        fields = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
        edits[lines[0]] = fields
    return edits


def apply_edits(projects, edits):
    """The cities' projects with our changes made, and ours added; an edit that can't be used is reported and
    left out rather than stopping the build."""
    by_id = {project["id"]: project for project in projects}
    for ident, fields in edits.items():
        status = fields.get("status", "").lower()
        if status and status not in STATUSES:
            print(f"✗ edits.txt, {ident}: no status called {status!r}", file=sys.stderr)
            continue
        try:
            when = date.fromisoformat(fields["date"]) if fields.get("date") else None
        except ValueError:
            print(f"✗ edits.txt, {ident}: {fields['date']!r} isn't a date like 2026-09-01", file=sys.stderr)
            continue
        project = by_id.get(ident)
        if project is None:
            missing = [key for key in ("name", "town", "units", "status", "lat", "lon") if not fields.get(key)]
            if missing:
                print(f"✗ edits.txt, {ident}: not in the cities' data, and has no {', '.join(missing)}", file=sys.stderr)
                continue
            try:
                lat, lon = float(fields["lat"]), float(fields["lon"])
            except ValueError:
                print(f"✗ edits.txt, {ident}: lat and lon should be numbers", file=sys.stderr)
                continue
            project = by_id[ident] = {
                "id": ident, "town": fields["town"], "neighborhood": fields.get("neighborhood", ""),
                "name": fields["name"], "units": number(fields["units"]), "status": status, "stage": "",
                "lat": lat, "lon": lon, "link": fields.get("link", ""), "description": fields.get("description", ""),
                "dated": None, "origin": "", "facts": facts(("Address", fields.get("address", ""))),
            }
        if fields.get("hide", "").lower() in ("yes", "true"):
            project["hidden"] = True
        if status:
            project["status"] = status
        for key in ("note", "link", "description", "image", "credit"):
            if fields.get(key):
                project[key] = fields[key]
        if when:
            project["edited"] = when
    return [project for project in by_id.values() if not project.get("hidden")]


def track(projects, previous, today):
    """When each project was last seen to change status, and from what, from the previous build's record:
    today, for one whose status has changed or that's new since then. The first build has nothing to compare
    with, so it dates nothing. Returns the record to keep for next time."""
    before = previous.get("projects")
    record = {}
    for project in projects:
        kept = (before or {}).get(project["id"])
        if before is None:
            since, was = None, None
        elif kept and kept["status"] == project["status"]:
            since, was = kept["since"], kept.get("was")
        else:
            # "" for a project that's new since the last build, which had nothing to say it was.
            since, was = today.isoformat(), kept["status"] if kept else ""
        project["since"] = date.fromisoformat(since) if since else None
        project["was"] = was
        record[project["id"]] = {"status": project["status"], "since": since, "was": was}
    return record


def change(project, today):
    """What last happened to it in the last RECENT_DAYS days, as (the day, what), for Recent updates; None if
    nothing did. A status seen to change says from what; Boston's filings and approvals and MassBuilds' updates
    come from their own dates; our own notes from the date on them."""
    events = []
    if project.get("since"):
        if project.get("was"):
            events.append((project["since"], f"{STATUSES[project['was']][0]} → {STATUSES[project['status']][0]}"))
        elif project.get("was") == "":
            events.append((project["since"], "Newly listed"))
    if iso_date(project.get("filed")):
        events.append((iso_date(project["filed"]), "Filed with the city"))
    if iso_date(project.get("approved")):
        events.append((iso_date(project["approved"]), "Board approved"))
    if project["id"].startswith("massbuilds-") and project["dated"]:
        events.append((project["dated"], "Updated in MassBuilds"))
    if project.get("edited"):
        events.append((project["edited"], project.get("note") or "Updated"))
    recent = [event for event in events if 0 <= (today - event[0]).days <= RECENT_DAYS]
    # The latest; on the same day, the one listed first above, a change of status before the rest.
    return max(recent, key=lambda event: event[0], default=None) if recent else None


def updated(project):
    """The latest thing known to have happened to it."""
    return max((d for d in (project["dated"], project.get("since"), project.get("edited")) if d), default=None)


def when(day, today):
    """A date as a row shows it: "Aug 14" this year, "Aug 2024" before."""
    if not day:
        return ""
    return f"{day:%b} {day.day}" if day.year == today.year else f"{day:%b %Y}"


def status_icon(status, label=None):
    """A status's mark, in its color: before each row, and in a project's panel."""
    return shared.icon(status.replace(" ", "-"), label).replace("<svg ", f'<svg style="color:{STATUSES[status][1]}" ', 1)


def steps(status):
    """How far along a project is, as four short bars, lit in its status's color up to its step; a stalled
    project's all unlit, its red icon saying why."""
    label, color = STATUSES[status]
    reached = STAGES.index(status) + 1 if status in STAGES else 0
    lit = f' style="background:{color}"'
    bars = "".join(f"<i{lit if n < reached else ''}></i>" for n in range(len(STAGES)))
    return f'<span class="steps" role="img" aria-label="{label}, step {reached} of {len(STAGES)}">{bars}</span>'


def row(project, today, changed=None, large=False):
    """A project's row. On Recent updates, changed is what happened and when: it takes the status's place after
    the name, and its day the row's date. On Large projects, a bar of four steps shows how far along it is.
    Both name its town, the page having projects from all of them in one list. Its name links to its panel's
    address on this page (?project=<id>)."""
    label, color = STATUSES[project["status"]]
    day = changed[0] if changed else updated(project)
    ident = html.escape(project["id"])
    note = f'<p class="note">{html.escape(project["note"])}</p>' if project.get("note") else ""
    # Its soonest comment deadline or meeting, as a tag after its details, where they don't already say it.
    say = project.get("say") or []
    tag = f' <span class="say-tag">{html.escape(say_text(say[0], today, short=True))}</span>' if say and not changed else ""
    where = project["town"] if changed or large else project["neighborhood"]
    place = " · ".join(html.escape(part) for part in (where, when(day, today)) if part)
    homes = f'{project["units"]:,} home{"s" if project["units"] != 1 else ""}'
    words = " ".join((project["name"], project["neighborhood"], project["town"], project["description"], project.get("note", "")))
    return (
        f'<li class="row" id="{ident}" data-status="{project["status"]}" data-units="{project["units"]}" '
        f'data-lat="{project["lat"]:.5f}" data-lon="{project["lon"]:.5f}" data-words="{html.escape(words.casefold())}">'
        f'{status_icon(project["status"], label)}'
        f'<span class="headline"><a class="title" href="?project={ident}">{html.escape(project["name"])}</a>'
        f' <span class="details">{homes} · {html.escape(changed[1]) if changed else label.lower()}</span>{tag}</span> '
        f'<span class="source">{steps(project["status"]) if large else ""}<span>{place}</span></span>{note}</li>'
    )


def panel_data(project, today):
    """What a project's panel shows, beyond its row: the page carries it as JSON, for the script to fill in."""
    return {
        "name": project["name"], "town": project["town"], "neighborhood": project["neighborhood"],
        "units": project["units"], "status": project["status"], "facts": project.get("facts", []),
        "description": project["description"], "note": project.get("note", ""), "link": project["link"],
        "origin": project.get("origin", ""), "site": project.get("site", ""), "image": project.get("image", ""),
        "credit": project.get("credit", ""),
        "updated": when(updated(project), today),
        # What last happened to it, where that was lately: the same the Recent updates page says of it.
        "change": [f"{changed[1]} · {long_date(changed[0])}"] if (changed := change(project, today)) else [],
        "say": [[say_text(item, today), item["link"], item["kind"], item.get("how", "")]
                for item in project.get("say", [])],
    }


CSS = """
  #map { height: 22rem; margin: 0 0 1.25rem; border: 1px solid #222; border-radius: 6px; background: #242426; }
  /* Full screen: the map the whole of it, without the corners and edge it has in the page. */
  #map:fullscreen { height: 100%; margin: 0; border: 0; border-radius: 0; }
  @media (max-width: 34rem) { #map { height: 16rem; } }
  /* Room to see: taller on a tall window, and in line with everything else on the page, whatever its width. */
  @media (min-height: 50rem) and (min-width: 46rem) { #map { height: 27rem; } }
  /* Under the map, the filter and the search: the filter's choices on the left, on two lines (the four steps of
     a project's way, then Complete, Stalled and All), the search on the right, level with the first; on a phone,
     the choices as they wrap, and the search on a line of its own under them, wide enough for a thumb. */
  .controls { display: flex; justify-content: space-between; align-items: flex-start; gap: .9rem 2rem; margin: 0 0 1.75rem; }
  .controls .filter { flex: 0 1 30rem; margin: 0; }
  .controls .search { flex: 0 0 11rem; margin-left: 0; }
  @media (max-width: 34rem) {
    .controls { flex-direction: column; align-items: stretch; }
    .controls .filter { flex: none; }
    .controls .search { flex: none; width: 100%; font-size: 1rem; }  /* Stacked, its flex size would be its height. */
  }
  /* Recently updated, a line over the map, as Pushpin's Just announced: one of the newest, and the way to the rest. */
  .fresh { display: grid; grid-template-columns: 1fr auto; gap: .45rem .6rem; margin: 0 0 1.25rem; padding: .7rem 0;
           font-size: .85rem; border-top: 1px solid #1c1c1c; border-bottom: 1px solid #1c1c1c; }
  .fresh-list { display: grid; gap: .35rem; min-width: 0; grid-column: 1 / -1; }
  .fresh-tag { color: #6e6e6e; font-size: .72rem; font-weight: 400; letter-spacing: .07em; text-transform: uppercase; }
  .spark-mark { width: 12px; height: 12px; margin-right: .45em; vertical-align: -1px; }
  /* One line, cut short with "…" if it must: as it turns over, a longer one never pushes the page down. */
  .fresh-one { min-width: 0; overflow: hidden; color: #999; text-overflow: ellipsis; white-space: nowrap; }
  .fresh-one .icon { margin-right: .5em; vertical-align: -1px; }
  .fresh.say .icon { color: #8a8a8a; }
  .fresh-one b { color: #fff; font-weight: 500; }
  .fresh-none { color: #666; }
  .fresh-more { color: #888; }
  .fresh-more:hover, .fresh-one:hover { color: #fff; text-decoration: none; }
  .fresh-one:hover b { text-decoration: underline; }

  /* The header: the site's name and the page's each kept whole, the page's going to a line of its own when both
     won't fit beside the time it was updated, which stays level with the first. */
  header { align-items: baseline; }
  .sites { flex-wrap: wrap; row-gap: .15rem; min-width: 0; }
  .sites a, .sites .here { white-space: nowrap; }
  /* A page's name for itself, after the site's in the header, a slash between them as on Pushpin; on a phone it
     takes the time it was updated's room. */
  .sites .here { color: #fff; font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; }
  .sites .here::before { content: "/"; margin-right: .5em; color: #444; font-weight: 400; }
  @media (max-width: 34rem) { header:has(.here:not([hidden])) .header-note { display: none; } }
  .sites a:not([aria-current]) .city { color: inherit; }
  /* The home page: the site's name is the page, so the city named after it is said quietly, not as a heading. */
  .sites a[aria-current="page"] + .here { color: #8a8a8a; font-weight: 500; }
  .tagline a { color: #bbb; }
  /* Large projects' bar of four steps, before its town: how far along the project is. */
  .steps { display: inline-flex; flex: none; gap: 2px; margin-right: .6rem; }
  .steps i { width: 9px; height: 4px; border-radius: 1px; background: #2a2a2a; }
  /* The footer, as Pushpin's: set off by a faint line, what the site is, then its pages, towns and sources in
     short lists under small, faint headings, the towns in two columns. */
  footer { margin-top: 4.5rem; padding-top: 2.75rem; border-top: 1px solid rgba(255, 255, 255, .09); }
  .foot-about { max-width: 38rem; margin: 0; color: #999; font-size: .9rem; line-height: 1.6; }
  .foot-about b { color: #ccc; font-weight: 600; }
  .site-links { display: grid; grid-template-columns: minmax(0, 10rem) minmax(0, 18rem) minmax(0, 10rem) minmax(0, 11rem);
                gap: 1.75rem 2.5rem; margin: 2.25rem 0 2rem; }
  .site-links h2 { margin: 0 0 .7rem; color: #555; font-size: .65rem; font-weight: 500; letter-spacing: .1em; text-transform: uppercase; }
  .site-links ul { display: grid; gap: .45rem; }
  /* The towns down one column, then the next, in the order they're listed on the home page. */
  .site-links .towns ul { display: block; columns: 2; column-gap: 1.5rem; }
  .site-links .towns li { margin-bottom: .45rem; break-inside: avoid; }
  footer .site-links a, footer .site-links a:visited { color: #999; font-size: .95rem; text-decoration: none; }
  footer .site-links a:hover { color: #fff; }
  footer .site-links a[aria-current="page"] { color: #fff; }
  @media (max-width: 34rem) {
    .site-links { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .site-links .towns { grid-column: 1 / -1; order: 3; }
  }
  .from .to-town { margin-left: .4em; color: #999; text-decoration: none; }
  .from .to-town:hover { color: #fff; }
  /* Have your say, under Recently updated: one band with it, the line between them shared. */
  .fresh + .fresh.say { margin-top: -1.25rem; border-top: 0; }
  /* A project's soonest comment deadline or meeting, a quiet tag after its details. */
  .say-tag { flex: none; align-self: center; margin-left: .6em; padding: .05rem .45rem; border: 1px solid #3a3a3a;
             border-radius: 999px; color: #bbb; font-size: .72rem; line-height: 1.4; white-space: nowrap; }
  /* In a project's panel: what last happened to it, and its open comment period and meetings, each a link to
     comment or join. */
  .panel-say, .panel-change { margin: 0 0 1.1rem; padding: .7rem .85rem; border: 1px solid #2a2a2a; border-radius: 6px; }
  .panel-say[hidden], .panel-change[hidden] { display: none; }
  .panel-change p { margin: 0; color: #ddd; font-size: .88rem; }
  .panel-say h3, .panel-change h3 { margin: 0 0 .4rem; color: #888; font-size: .72rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
  .panel-say ul { display: grid; gap: .3rem; }
  .panel-say a { color: #eee; font-size: .88rem; }
  /* How to be heard, under Have your say, and the lines that offer a calendar or a feed. */
  .how { max-width: 42rem; margin: 2.25rem 0 0; color: #b4b4b4; font-size: .9rem; line-height: 1.65; }
  .how h2 { margin: 0 0 .5rem; color: #eee; font-size: 1rem; }
  .how h3 { margin: 1.9rem 0 0; color: #eee; font-size: .95rem; }
  .how p, .how ul { margin: 0 0 .7rem; }
  .how ul { display: grid; gap: .45rem; padding-left: 1.1rem; list-style: disc; }
  .how b { color: #e2e2e2; font-weight: 600; }
  .how a, p.follow a { color: #cfcfcf; }
  .how a:hover, p.follow a:hover { color: #fff; }
  p.follow, .how-follow { margin: 1.75rem 0 .5rem; color: #999; font-size: .85rem; }
  /* The one thing to do on this page, said as a button, with the feed beside it. */
  .how p.how-do { display: flex; flex-wrap: wrap; gap: .75rem .5rem; margin: 1.6rem 0 1.3rem; }
  .how-do .do { display: inline-flex; align-items: center; gap: .45rem; padding: .4rem .9rem; border: 1px solid #333;
                border-radius: 999px; color: #ddd; font-size: .9rem; text-decoration: none; }
  .how-do .do:hover, .how-do .do:focus-visible { border-color: #888; color: #fff; outline: none; }
  .how-do .primary { border-color: #eee; background: #eee; color: #000; font-weight: 600; }
  .how-do .primary:hover, .how-do .primary:focus-visible { background: #fff; color: #000; }
  .how-do .primary svg { width: 1em; height: 1em; }
  .how p.how-follow { margin: 0; }
  .panel-say .say-how { margin: .15rem 0 .1rem; color: #999; font-size: .8rem; line-height: 1.5; }
  /* A page's heading: the header says the same in its own way, so this is for search engines and screen readers. */
  .page-heading { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden;
                  clip-path: inset(50%); white-space: nowrap; }
  /* What a town's page says about its town, under the line that says what it is. */
  /* What the site is, under its name. */
  .intro { margin: -1.25rem 0 1.25rem; }
  /* On as many lines as it takes, rather than cut short: the home page's fits on one. */
  .tagline { margin: 0; color: #888; font-size: .9rem; }
  .sites .city { color: #777; }
  /* The site's mark, before its name. */
  .sites a { display: inline-flex; align-items: center; }
  .sites .mark { width: 1.05em; height: 1.05em; margin-right: .42em; }
  @media (max-width: 34rem) {
    .tagline { font-size: .8rem; }
  }
  /* The map's controls, notes and credits, quiet and dark like the page. Each is written from #map, so they
     hold whether MapLibre's own stylesheet has arrived yet or not (it's fetched with the map, after these). */
  #map .maplibregl-map { font: inherit; }
  #map .maplibregl-map .map-hint { position: absolute; left: 0; bottom: 0; z-index: 2; }
  #map .maplibregl-popup-content { padding: .7rem .9rem; border: 1px solid #333; border-radius: 6px; background: #111; color: #ddd;
                              font-size: .85rem; line-height: 1.4; box-shadow: none; }
  #map .maplibregl-popup-content a { color: #fff; }
  #map .maplibregl-popup-content .muted { color: #888; }
  #map .maplibregl-popup-close-button { color: #666; font-size: 1rem; }
  /* The arrow to its dot, whichever side the note opens on (MapLibre picks the side with room), in the note's own
     color, so it reads as part of it. */
  #map .maplibregl-popup-anchor-bottom .maplibregl-popup-tip,
  #map .maplibregl-popup-anchor-bottom-left .maplibregl-popup-tip,
  #map .maplibregl-popup-anchor-bottom-right .maplibregl-popup-tip { border-top-color: #111; }
  #map .maplibregl-popup-anchor-top .maplibregl-popup-tip,
  #map .maplibregl-popup-anchor-top-left .maplibregl-popup-tip,
  #map .maplibregl-popup-anchor-top-right .maplibregl-popup-tip { border-bottom-color: #111; }
  #map .maplibregl-popup-anchor-left .maplibregl-popup-tip { border-right-color: #111; }
  #map .maplibregl-popup-anchor-right .maplibregl-popup-tip { border-left-color: #111; }
  /* The map's buttons: a dark pane over the map, its marks gray until they're pointed at. MapLibre draws them
     black on white, so each mark is turned about (invert) rather than replaced. */
  #map .maplibregl-ctrl-group { border: 1px solid #2c2c2e; border-radius: 6px; overflow: hidden;
                           background: rgba(16, 16, 17, .82); box-shadow: none !important;
                           -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px); }
  #map .maplibregl-ctrl-group button + button { border-top: 1px solid #2c2c2e; }
  #map .maplibregl-ctrl-group button .maplibregl-ctrl-icon { filter: invert(.58); transition: filter .15s; }
  #map .maplibregl-ctrl-group button:hover { background: rgba(255, 255, 255, .06); }
  #map .maplibregl-ctrl-group button:hover .maplibregl-ctrl-icon { filter: invert(.92); }
  #map .maplibregl-ctrl-group button:active { background: rgba(255, 255, 255, .1); }
  /* No blue ring (which the mark's invert would turn orange); a quiet one from the keyboard instead. */
  #map .maplibregl-ctrl-group button:focus { box-shadow: none; }
  #map .maplibregl-ctrl-group button:focus-visible { box-shadow: inset 0 0 0 1px #6a6a6a; }
  #map .maplibregl-ctrl-group button:disabled .maplibregl-ctrl-icon { filter: invert(.3); }
  /* The credits: folded, only a small, faint ⓘ with nothing behind it; opened, a dark band. Its icon is drawn here
     in gray rather than MapLibre's black inverted, which also turned the blue ring it's given when focused orange;
     from the keyboard it gets a quiet ring of its own instead. */
  #map .maplibregl-ctrl-attrib, #map .maplibregl-ctrl-attrib.maplibregl-compact { background: none; color: #555; font-size: 9px; }
  /* Opened, its line of credits centered in the band, level with the ⓘ at its end. */
  #map .maplibregl-ctrl-attrib.maplibregl-compact-show { display: flex; align-items: center; box-sizing: border-box; min-height: 24px;
                                                    padding-top: 0; padding-bottom: 0; background: rgba(0, 0, 0, .6); }
  #map .maplibregl-ctrl-attrib a { color: #666; }
  #map .maplibregl-ctrl-attrib-button, #map .maplibregl-ctrl-attrib.maplibregl-compact-show .maplibregl-ctrl-attrib-button {
    background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3E%3Cpath fill='%23888' fill-rule='evenodd' d='M4 10a6 6 0 1 0 12 0 6 6 0 1 0-12 0m5-3a1 1 0 1 0 2 0 1 1 0 1 0-2 0m0 3a1 1 0 1 1 2 0v3a1 1 0 1 1-2 0'/%3E%3C/svg%3E") center / 16px no-repeat;
    opacity: .45; transition: opacity .15s; }
  #map .maplibregl-ctrl-attrib-button:hover, #map .maplibregl-ctrl-attrib.maplibregl-compact-show .maplibregl-ctrl-attrib-button { opacity: .8; }
  #map .maplibregl-ctrl-attrib-button:focus { box-shadow: none; }
  #map .maplibregl-ctrl-attrib-button:focus-visible { box-shadow: 0 0 0 1px #555; opacity: .8; }
  /* While the map loads, a quiet note in its middle, fading in only if loading takes a moment. */
  .map-loading { position: absolute; inset: 0; z-index: 1; display: grid; place-items: center; color: #666; font-size: .85rem;
                 pointer-events: none; animation: appear .3s ease-out .4s both; }
  /* What the map is leaving out at this zoom, quiet in its corner. */
  .map-hint { margin: 0 0 .45rem .55rem !important; padding: .1rem .45rem; border-radius: 3px; background: rgba(0, 0, 0, .65);
              color: #888; font-size: .72rem; pointer-events: none; }
  .map-hint:empty { display: none; }
  .panel-pill .icon { width: 12px; height: 12px; }
  main > details { border-top: 1px solid #1c1c1c; }
  /* On a phone the search sits right above the first list, its own line and the list's reading as a pair. */
  @media (max-width: 34rem) { main > details:first-of-type { border-top: 0; } }
  /* A list's heading kept whole, and its count on the next line when both won't fit on one. */
  main > details > summary .town { white-space: nowrap; }
  main > details > summary .count { margin-left: auto; }
  main > details > summary { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: .1rem 1rem; padding: .8rem 0;
            cursor: pointer; list-style: none; font-weight: 700; }
  main > details > summary::-webkit-details-marker { display: none; }
  /* An open list's heading stays at the top of the window while its projects scroll under it, until the next
     list's pushes it up; black behind it, so they're hidden there, and a faint line under it once it's stuck. */
  main > details[open] > summary { position: sticky; top: 0; z-index: 1; background: #000; transition: box-shadow .15s; }
  main > details[open] > summary.stuck { box-shadow: 0 1px 0 #262626; }
  /* A chevron drawn to a square and turned about its middle, so open or shut it sits level with the town's name
     (a "›" turned with its line of text drops below it). */
  main > details > summary::before { content: ""; flex: none; align-self: center; width: 12px; height: 12px; margin-right: .1rem;
                    background: #666; transition: transform .15s;
                    -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M4.5 2.5L8 6l-3.5 3.5' fill='none' stroke='black' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center / contain no-repeat;
                    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M4.5 2.5L8 6l-3.5 3.5' fill='none' stroke='black' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center / contain no-repeat; }
  main > details > summary:hover::before { background: #999; }
  main > details[open] > summary::before { transform: rotate(90deg); }
  @media (prefers-reduced-motion: reduce) { summary::before { transition: none; } }
  main > details > summary .town { flex: 1; }
  main > details > summary .count { color: #666; font-size: .8rem; font-weight: normal; }
  main > details > ul { padding-bottom: 1rem; }
  main > details > summary:has(+ .from) { padding-bottom: .5rem; }
  .from { margin: 0 0 .6rem; color: #666; font-size: .8rem; }
  .from a { color: #999; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  /* A project's name keeps its room, up to most of the row; what follows it is cut short first. */
  .row .headline .title { flex-shrink: 0; max-width: 70%; }
  .row .details { flex: 0 1 auto; min-width: 0; margin-left: .6em; overflow: hidden; color: #666; font-size: .8em;
                  white-space: nowrap; text-overflow: ellipsis; }
  .row .note { grid-column: 2 / -1; margin: -.2rem 0 0; color: #999; font-size: .8em; }
  .row { cursor: pointer; }
  /* The row under the pointer, a faint band just darker than the one left on the project last opened; where a
     pointer hovers only, since on a touch screen the last row tapped would stay lit. */
  @media (hover: hover) and (pointer: fine) {
    .row { transition: background-color .12s, box-shadow .12s; }
    .row:hover { background: #0f0f0f; box-shadow: -.5rem 0 #0f0f0f, .5rem 0 #0f0f0f; }
    /* The row whose dot is under the pointer on the map, lit the same way. */
    .row.near { background: #0f0f0f; box-shadow: -.5rem 0 #0f0f0f, .5rem 0 #0f0f0f; }
  }
  /* The project last opened, a faint band behind its row, reaching a little past the text on either side. */
  .row.lit { background: #141414; box-shadow: -.5rem 0 #141414, .5rem 0 #141414; }
  /* A project's panel, in from the right, as Pushpin's listings open: a sheet up from the bottom on a phone. */
  body.viewing { overflow: hidden; }
  dialog.project { width: min(32rem, 100%); height: 100dvh; max-height: none; margin: 0 0 0 auto; box-sizing: border-box;
                   overflow: hidden; padding: 0; border: 0; border-left: 1px solid #262626; border-radius: 0;
                   background: #0b0b0b; color: #ddd; box-shadow: -1px 0 40px rgba(0, 0, 0, .5); font-size: .95rem; line-height: 1.5; }
  dialog.project::backdrop { background: rgba(0, 0, 0, .6); }
  dialog.project[open] { display: flex; flex-direction: column; animation: slide .22s ease-out; }
  @keyframes slide { from { transform: translateX(100%); } }
  @keyframes appear { from { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) { dialog.project[open] { animation: appear .15s ease-out; } }
  .panel-body { flex: 1; min-height: 0; overflow: auto; display: flex; flex-direction: column; padding: 3.4rem 1.5rem 1.5rem; }
  .panel-body > * { flex: none; }
  dialog.project.shows-picture .panel-body { padding-top: 1.4rem; }
  .panel-tools { position: absolute; top: .85rem; right: .85rem; z-index: 1; display: flex; gap: .2rem; }
  .panel-tools button { display: grid; place-items: center; width: 2rem; height: 2rem; padding: 0; border: 0; border-radius: 50%;
                        background: none; color: #888; cursor: pointer; transition: background-color .15s, color .15s; }
  .panel-tools button:hover, .panel-tools button:focus-visible { background: #262626; color: #fff; outline: none; }
  .panel-tools button:disabled { visibility: hidden; transition: none; }
  .panel-tools svg { width: 16px; height: 16px; }
  dialog.project.shows-picture .panel-tools button { background: rgba(0, 0, 0, .6); color: #fff; }
  dialog.project.shows-picture .panel-tools button:hover:not(:disabled) { background: rgba(0, 0, 0, .9); }
  .panel-image { position: relative; aspect-ratio: 16 / 10; margin: -1.4rem -1.5rem 1.1rem; overflow: hidden; background: #151515; }
  .panel-image img { display: block; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity .35s ease-out; }
  .panel-image.loaded img { opacity: 1; }
  .panel-credit { position: absolute; right: 0; bottom: 0; left: 0; padding: 1.6rem .85rem .5rem; color: #cfcfcf;
                  font-size: .72rem; text-align: right; text-shadow: 0 1px 2px #000;
                  background: linear-gradient(to top, rgba(0, 0, 0, .55), transparent); pointer-events: none; }
  .panel-credit[hidden] { display: none; }
  .panel-image:not(.loaded) { background: linear-gradient(100deg, #151515 40%, #1d1d1d 50%, #151515 60%) 0 0 / 250% 100%;
                              animation: shimmer 1.6s linear infinite; }
  @keyframes shimmer { from { background-position: 100% 0; } to { background-position: 0 0; } }
  .panel-where { margin: 0 2.5rem .4rem 0; color: #888; font-size: .72rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
  .panel-title { margin: 0 2rem .3rem 0; color: #fff; font-size: 1.35rem; font-weight: 700; letter-spacing: -.01em; line-height: 1.25; }
  .panel-pills { display: flex; flex-wrap: wrap; gap: .4rem; margin: .45rem 0 .9rem; }
  .panel-pill { display: inline-flex; align-items: center; gap: .4rem; padding: .15rem .6rem; border-radius: 999px;
                background: #1c1c1c; color: #eee; font-size: .8rem; font-weight: 600; }
  .panel-note { margin: 0 0 1rem; padding: .6rem .8rem; border-left: 2px solid #555; background: #141414; color: #ddd; font-size: .9rem; }
  .panel-note:empty { display: none; }
  /* No gap between the columns, so each line runs unbroken across both; the room is in the label's own cell. */
  .panel-facts { display: grid; grid-template-columns: auto 1fr; margin: 0 0 1.1rem; font-size: .85rem; }
  .panel-facts dt, .panel-facts dd { margin: 0; padding: .45rem 0; border-top: 1px solid #1c1c1c; }
  .panel-facts dt { padding-right: 1rem; color: #777; }
  .panel-facts dd { color: #ddd; }
  .panel-about p { margin: 0 0 .8em; color: #bbb; }
  /* The way to its source on a line of its own, the rest under it. */
  .panel-foot { display: flex; flex-wrap: wrap; gap: .6rem .5rem; margin-top: auto; padding-top: 1.4rem; }
  .panel-break { flex-basis: 100%; height: 0; }
  .panel-foot a, .panel-foot button { display: inline-flex; align-items: center; padding: .4rem .9rem; border: 1px solid #333;
                border-radius: 999px; background: none; color: #ddd; font: inherit; font-size: .9rem; cursor: pointer; text-decoration: none; }
  .panel-foot a:hover, .panel-foot button:hover, .panel-foot a:focus-visible, .panel-foot button:focus-visible {
    border-color: #888; color: #fff; outline: none; text-decoration: none; }
  .panel-foot .primary { border-color: #eee; background: #eee; color: #000; font-weight: 600; }
  .panel-foot .primary:hover, .panel-foot .primary:focus-visible { background: #fff; color: #000; }
  .panel-updated { width: 100%; margin: .6rem 0 0; color: #666; font-size: .8rem; }
  @media (max-width: 34rem) {
    dialog.project { width: 100%; max-width: 100%; height: auto; max-height: 88dvh; margin: auto 0 0;
                     border: 1px solid #262626; border-width: 1px 0 0; border-radius: 16px 16px 0 0; }
    .panel-body { padding: 3.2rem 1.25rem 1.5rem; }
    dialog.project.shows-picture .panel-body { padding-top: 1.25rem; }
    .panel-image { margin: -1.25rem -1.25rem 1rem; border-radius: 15px 15px 0 0; }
    .panel-tools { top: .75rem; right: .75rem; gap: .9rem; }
    .panel-tools button { width: 2.5rem; height: 2.5rem; }
    dialog.project[open] { animation: sheet .22s ease-out; }
    @keyframes sheet { from { transform: translateY(100%); } }
  }
  @media (max-width: 34rem) {
    .row .details { white-space: normal; }
    .row .note { margin: .15rem 0 0; }
    .row .details { margin-left: 0; }
    .row .details::after { content: " ·"; }
  }
"""

def mark(path):
    return (f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{path}" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>')


# A project's panel. The script fills it in from the page's data as a project is opened.
PANEL = f"""<dialog class="project" aria-labelledby="panel-title">
<div class="panel-tools"><button class="panel-back" type="button" aria-label="Previous project">{mark("M15 6l-6 6l6 6")}</button><button class="panel-on" type="button" aria-label="Next project">{mark("M9 6l6 6l-6 6")}</button><button class="panel-close" type="button" aria-label="Close">{mark("M6 6l12 12M18 6l-12 12")}</button></div>
<div class="panel-body">
<div class="panel-image" hidden><img alt="" decoding="async" referrerpolicy="no-referrer"><p class="panel-credit"></p></div>
<p class="panel-where"></p>
<h2 class="panel-title" id="panel-title"></h2>
<div class="panel-pills"></div>
<p class="panel-note"></p>
<div class="panel-change"><h3>Recently updated</h3><p></p></div>
<div class="panel-say"><h3>Have your say</h3><ul></ul></div>
<dl class="panel-facts"></dl>
<div class="panel-about"></div>
<div class="panel-foot"><a class="panel-source primary" target="_blank" rel="noopener"></a><span class="panel-break"></span><a class="panel-site" target="_blank" rel="noopener">Project site ↗</a><button class="panel-copy" type="button">Copy link</button><button class="panel-map" type="button">Show on map</button><a class="panel-wrong" target="_blank" rel="noopener">Suggest a correction</a><p class="panel-updated"></p></div>
</div>
</dialog>"""

# Before Recently updated, as before Pushpin's Just announced: Tabler's "sparkles" (outline, MIT license).
SPARK_MARK = ('<svg class="spark-mark" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" '
              'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M16 18a2 2 0 0 1 2 2a2 2 0 0 1 2 -2a2 2 0 0 1 -2 -2a2 2 0 0 1 -2 2z"/>'
              '<path d="M16 6a2 2 0 0 1 2 2a2 2 0 0 1 2 -2a2 2 0 0 1 -2 -2a2 2 0 0 1 -2 2z"/>'
              '<path d="M9 18a6 6 0 0 1 6 -6a6 6 0 0 1 -6 -6a6 6 0 0 1 -6 6a6 6 0 0 1 6 6z"/></g></svg>')

SAY_MARK = ('<svg class="spark-mark" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a3 3 0 0 1 0 6"/>'
            '<path d="M10 8v11a1 1 0 0 1 -1 1h-1a1 1 0 0 1 -1 -1v-5"/>'
            '<path d="M12 8h0l4.524 -3.77a.9 .9 0 0 1 1.476 .692v12.156a.9 .9 0 0 1 -1.476 .692l-4.524 -3.77h-8a1 1 0 0 1 '
            '-1 -1v-4a1 1 0 0 1 1 -1h8"/></g></svg>')

# On the button that subscribes to the deadlines: Tabler Icons' "calendar-plus", drawn as its siblings are.
CALENDAR_MARK = ('<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2" '
                 'stroke-linecap="round" stroke-linejoin="round">'
                 '<path d="M12.5 21h-6.5a2 2 0 0 1 -2 -2v-12a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v6"/>'
                 '<path d="M16 3v4"/><path d="M8 3v4"/><path d="M4 11h16"/><path d="M16 19h6"/>'
                 '<path d="M19 16v6"/></g></svg>')

# The banners' lines open their project's panel here, rather than the page loading again at its address.
FRESH_SCRIPT = """
<script>
  for (const line of document.querySelectorAll(".fresh-one[href]")) {
    line.addEventListener("click", event => {
      const row = document.getElementById(new URLSearchParams(line.getAttribute("href")).get("project"));
      if (!row || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      row.click();
    });
  }
</script>"""

# The site's icons (static/, drawn by make_icons.py), from a page root levels up.
def icons_head():
    return ('<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
            '<link rel="icon" href="/favicon-32.png" sizes="32x32" type="image/png">'
            '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
            '<link rel="manifest" href="/manifest.webmanifest">')


# OpenFreeMap's dark style as this site draws it, for any map on it (the main one, a project page's): it needs a
# map already made, called map.
STYLE_JS = """    // OpenFreeMap's dark style, recolored to read more easily: the land lifted a little from black and the water
    // sunk below it in a deep blue, so the harbor and the rivers stand apart from the land; roads a shade or two
    // lighter than the land, rather than black lines edged in gray, the bigger a little lighter (solid colors: a
    // road is drawn in many overlapping pieces, and see-through ones add up to a bright web downtown); parks barely
    // green; and the place names a little brighter than the style's.
    const LAND = "#242426", WATER = "#0b1520";
    const RECOLOR = {
      "background": {"background-color": LAND},
      "water": {"fill-color": WATER},
      "waterway": {"line-color": WATER},
      "landuse_residential": {"fill-opacity": 0},
      "landcover_wood": {"fill-color": "#252b26"},
      "landuse_park": {"fill-color": "#252b26"},
      "building": {"fill-color": "#2c2c2e", "fill-outline-color": "#333335"},
      "aeroway-area": {"fill-color": "#29292b"},
      "aeroway-runway": {"line-color": "#303032"},
      "aeroway-taxiway": {"line-color": "#2a2a2c"},
      "aeroway-runway-casing": {"line-opacity": 0},
      "road_area_pier": {"fill-color": LAND},
      "road_pier": {"line-color": LAND},
      "highway_path": {"line-color": "#28282a"},
      "highway_minor": {"line-color": "#2a2a2c"},
      "highway_major_casing": {"line-opacity": 0},
      "highway_major_inner": {"line-color": "#303032"},
      "highway_major_subtle": {"line-color": "#2d2d2f"},
      "highway_motorway_casing": {"line-opacity": 0},
      "highway_motorway_inner": {"line-color": "#373739"},
      "highway_motorway_subtle": {"line-color": "#303032"},
      "railway": {"line-color": "#2d2d2f"},
      "railway_transit": {"line-color": "#2d2d2f"},
      "railway_minor": {"line-color": "#2a2a2c"},
      "railway_dashline": {"line-color": LAND},
      "railway_transit_dashline": {"line-color": LAND},
      "railway_minor_dashline": {"line-color": LAND},
      "highway_name_other": {"text-color": "#6a6a6a", "text-halo-color": LAND},
      "highway_name_motorway": {"text-color": "#7a7a7a"},
      "water_name": {"text-color": "#5a7896", "text-halo-color": "rgba(0, 0, 0, 0)"},
    };
    for (const id of ["place_other", "place_suburb", "place_village", "place_town", "place_city", "place_city_large"]) {
      RECOLOR[id] = {"text-color": "#9a9a9a", "text-halo-color": "rgba(36, 36, 38, .85)"};
    }
    // As soon as the style's read, before any of it is drawn, so its own colors never show. Any layer a later
    // version of the style renames is left as the style has it.
    map.on("style.load", () => {
      for (const [id, paint] of Object.entries(RECOLOR)) {
        if (map.getLayer(id)) for (const [name, value] of Object.entries(paint)) map.setPaintProperty(id, name, value);
      }
    });
"""

# The map, drawn by MapLibre from OpenFreeMap's dark vector tiles, which need no key: the browser draws its roads
# and names itself, so they're sharp on any screen. It gives the page a dotMap: its element; its zoom, a step
# higher than MapLibre's own (whose tiles are twice the size), for the cutoffs in LEAST; a way to hear it zoom; to
# draw a set of projects' dots (biggest first); to say what it's leaving out; to fit a set in view; and to go to
# one project and open its note.
MAP_JS = """
    // MapLibre is a megabyte, so it's fetched when the map is about to be seen, not with the page. Until then
    // the page talks to this stand-in, which keeps what it's asked for and hands it over once the map is up.
    const MAP_CSS = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.min.css";
    const MAP_SRC = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.js";
    const mapBox = document.getElementById("map");
    const hintBox = Object.assign(document.createElement("div"), {className: "map-hint"});
    mapBox.append(hintBox);
    const START = innerWidth < 544 ? 11 : 12;  // A phone's narrower map, one step further out.
    const APART = 12;  // From here in, every project is its own dot; further out, they're grouped.
    // A map given a few projects to fit (a town's, or a page of them) shows each of them, however close together.
    const FITTED = mapBox.hasAttribute("data-fit");
    const escape = text => text.replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
    const radius = row => Math.max(2.2, Math.min(7, Math.sqrt(Math.max(0, +row.dataset.units) || 0) / 3.3));
    // A number for each project, so a row and its dot can find each other and light up together.
    const numbers = new Map(rows.map((row, at) => [row.id, at]));
    let map = null, opening = null, lit = null;
    const wanted = {shown: [], fit: null};  // What was asked for before the map was up.
    const zoomHandlers = [];
    // A city files a tower and the affordable homes that go with it as two projects at one address, and gives
    // them the same point; stacked exactly, one dot hides under the other. Projects sharing a point are set a
    // few metres apart around it, far enough to tell apart and click once the map is in close, and too little
    // to move any of them off their street.
    const TOGETHER = 18;  // Metres between the dots of projects filed at one address.
    const placed = new Map();  // Where each project's dot goes, once that's been worked out.
    const place = shown => {
      const sharing = new Map();
      for (const row of shown) {
        const where = `${row.dataset.lat},${row.dataset.lon}`;
        if (!sharing.has(where)) sharing.set(where, []);
        sharing.get(where).push(row);
      }
      placed.clear();
      for (const together of sharing.values()) {
        for (const [at, row] of together.entries()) {
          const lat = +row.dataset.lat, lon = +row.dataset.lon;
          if (together.length === 1) { placed.set(row.id, [lon, lat]); continue; }
          const way = at / together.length * 2 * Math.PI;
          placed.set(row.id, [lon + Math.cos(way) * TOGETHER / (111320 * Math.cos(lat * Math.PI / 180)),
                              lat + Math.sin(way) * TOGETHER / 111320]);
        }
      }
    };
    const at = row => placed.get(row.id) || [+row.dataset.lon, +row.dataset.lat];
    // The smaller over the bigger: a higher sort key is drawn on top.
    const geojson = shown => {
      place(shown);
      return {type: "FeatureCollection", features: shown.map(row => ({
        type: "Feature", geometry: {type: "Point", coordinates: at(row)},
        id: numbers.get(row.id),
        properties: {row: row.id, name: row.querySelector(".title").textContent, homes: +row.dataset.units,
                     color: colors[row.dataset.status], radius: radius(row), order: -row.dataset.units},
      }))};
    };
    // A dot's note: its name, which opens its panel, and its homes and status.
    const note = row => {
      const status = row.dataset.status, box = document.createElement("div");
      box.innerHTML = `<a href="?project=${row.id}" class="to-row">${escape(row.querySelector(".title").textContent)}</a><br>`
        + `<span class="muted">${(+row.dataset.units).toLocaleString()} homes · ${labels[status].toLowerCase()}</span>`;
      box.querySelector("a").addEventListener("click", event => { event.preventDefault(); openProject(row, true); });
      return new maplibregl.Popup({offset: radius(row) + 4, maxWidth: "280px"})
        .setLngLat(at(row)).setDOMContent(box).addTo(map);
    };
    const fitTo = shown => {
      const bounds = new maplibregl.LngLatBounds();
      for (const row of shown) bounds.extend(at(row));
      map.fitBounds(bounds, {padding: 30, maxZoom: 13, animate: false});
    };
    const dotMap = {
      container: mapBox,
      zoom: () => map ? Math.round(map.getZoom()) + 1 : START,
      onZoom: then => zoomHandlers.push(then),
      draw: shown => {
        wanted.shown = shown;
        if (map && map.getSource("dots")) map.getSource("dots").setData(geojson(shown));
      },
      // Whether it's drawing groups rather than each project: further out than the view it opens at.
      grouping: () => !!map && !FITTED && map.getZoom() < APART,
      hint: text => { hintBox.textContent = text; },
      fit: shown => { wanted.fit = shown; if (map && map.loaded()) fitTo(shown); },
      // Asked to go somewhere before it's up (Show on map), the map is fetched there and then.
      goTo: row => start().then(() => {
        map.once("moveend", () => note(row));
        map.flyTo({center: at(row), zoom: 15});
      }),
      light: (id, on) => {
        if (id === null || id === undefined) return;
        if (map && map.getSource("dots")) map.setFeatureState({source: "dots", id}, {lit: on});
        const row = rows[id];
        if (row) row.classList.toggle("near", on);
      },
    };
    // The library, fetched once; then the map itself.
    const library = () => new Promise((done, failed) => {
      if (window.maplibregl) return done();
      document.head.append(Object.assign(document.createElement("link"), {rel: "stylesheet", href: MAP_CSS}));
      document.head.append(Object.assign(document.createElement("script"), {src: MAP_SRC, onload: done, onerror: failed}));
    });
    const start = () => opening || (opening = library().then(build));
    function build() {
      // Until the map has loaded (OpenFreeMap's servers are slow now and then), a note in its middle.
      const loading = Object.assign(document.createElement("div"), {className: "map-loading", textContent: "Loading map…"});
      mapBox.append(loading);
      map = new maplibregl.Map({
        container: mapBox, style: "https://tiles.openfreemap.org/styles/dark", center: [-71.08, 42.365],
        zoom: START - 1, scrollZoom: false, dragRotate: false, pitchWithRotate: false, touchPitch: false,
        attributionControl: {compact: true},
      });
      map.touchZoomRotate.disableRotation();
      map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-left");
      map.addControl(new maplibregl.FullscreenControl(), "top-left");
      // Full screen has no page to scroll past, so there the trackpad and wheel zoom the map; in the page they
      // don't, where they'd take the scroll the reader meant for the page itself.
      document.addEventListener("fullscreenchange", () => {
        if (document.fullscreenElement === mapBox) map.scrollZoom.enable(); else map.scrollZoom.disable();
      });
      for (const then of zoomHandlers) map.on("zoomend", then);
""" + STYLE_JS + """      map.on("load", () => {
        loading.remove();
        // The credits folded to their ⓘ, which the map's terms ask be on it; MapLibre opens them at first on a wide map.
        const credits = mapBox.querySelector(".maplibregl-ctrl-attrib");
        if (credits) { credits.classList.remove("maplibregl-compact-show"); credits.removeAttribute("open"); }
        // Further out than APART, projects near each other are gathered into one group; from there in, and on a
        // fitted map, each keeps its own dot. A project on its own stays a dot at every zoom either way.
        map.addSource("dots", {
          type: "geojson", data: geojson(wanted.shown),
          cluster: !FITTED, clusterMaxZoom: APART - 1, clusterRadius: 30, clusterMinPoints: 3,
          clusterProperties: {homes: ["+", ["get", "homes"]]},
        });
        map.addLayer({
          id: "dots", type: "circle", source: "dots", filter: ["!", ["has", "point_count"]],
          layout: {"circle-sort-key": ["get", "order"]},
          // A ring in the status's color around a faint fill of the same; the one under the pointer, or under it
          // in the list, brighter and thicker.
          paint: {"circle-color": ["get", "color"],
                  "circle-radius": ["+", ["get", "radius"], ["case", ["boolean", ["feature-state", "lit"], false], 2, 0]],
                  "circle-opacity": ["case", ["boolean", ["feature-state", "lit"], false], .5, .22],
                  "circle-stroke-color": ["get", "color"],
                  "circle-stroke-width": ["case", ["boolean", ["feature-state", "lit"], false], 2.5, 1.5]},
        });
        // Close in, the bigger projects say what they are: at street level every one of them, further out only
        // the largest, so the names never crowd the map.
        map.addLayer({
          id: "dot-names", type: "symbol", source: "dots", minzoom: 12.5,
          filter: ["all", ["!", ["has", "point_count"]],
                   [">=", ["get", "homes"], ["step", ["zoom"], 300, 13.5, 150, 14.5, 50, 15.5, 0]]],
          layout: {"text-field": ["get", "name"], "text-font": ["Noto Sans Regular"], "text-size": 11,
                   "text-offset": [0, 1.1], "text-anchor": "top", "text-max-width": 9, "text-optional": true,
                   "symbol-sort-key": ["-", 0, ["get", "homes"]]},
          paint: {"text-color": "#c9c9c9", "text-halo-color": "#242426", "text-halo-width": 1.4},
        });
        // A group: a ring in the map's own gray, as wide as the homes in it, saying how many they come to.
        map.addLayer({
          id: "groups", type: "circle", source: "dots", filter: ["has", "point_count"],
          paint: {"circle-color": "#101011", "circle-opacity": .82,
                  "circle-radius": ["interpolate", ["linear"], ["get", "homes"], 0, 10, 500, 13, 3000, 19],
                  "circle-stroke-color": "#9a9a9a", "circle-stroke-width": 1.5},
        });
        map.addLayer({
          id: "group-homes", type: "symbol", source: "dots", filter: ["has", "point_count"],
          layout: {
            "text-field": ["case", [">=", ["get", "homes"], 1000],
                           ["concat", ["number-format", ["/", ["get", "homes"], 1000], {"max-fraction-digits": 1}], "k"],
                           ["to-string", ["get", "homes"]]],
            "text-font": ["Noto Sans Regular"], "text-size": 11, "text-allow-overlap": true,
          },
          paint: {"text-color": "#e2e2e2"},
        });
        // A group opens: the map goes in until its projects come apart.
        map.on("click", "groups", event => {
          const group = event.features[0];
          map.getSource("dots").getClusterExpansionZoom(group.properties.cluster_id).then(zoom => {
            map.easeTo({center: group.geometry.coordinates, zoom: Math.max(zoom, APART)});
          }).catch(() => map.easeTo({center: group.geometry.coordinates, zoom: APART}));
        });
        map.on("mouseenter", "groups", () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", "groups", () => { map.getCanvas().style.cursor = ""; });
        if (wanted.fit && wanted.fit.length) fitTo(wanted.fit);
        // The page asked what the map was drawing before there was a map: now there is, it's asked again.
        for (const then of zoomHandlers) then();
        map.on("click", "dots", event => note(document.getElementById(event.features[0].properties.row)));
        // The dot under the pointer and its row, lit together, and the same the other way about.
        map.on("mousemove", "dots", event => {
          const id = event.features[0].id;
          if (id === lit) return;
          dotMap.light(lit, false);
          dotMap.light(lit = id, true);
        });
        map.on("mouseleave", "dots", () => { dotMap.light(lit, false); lit = null; map.getCanvas().style.cursor = ""; });
        map.on("mouseenter", "dots", () => { map.getCanvas().style.cursor = "pointer"; });
      });
    }
    // Fetched as the map comes near the window, so a reader who never reaches it never waits for it.
    if (window.IntersectionObserver) {
      const watcher = new IntersectionObserver(seen => {
        if (seen.some(one => one.isIntersecting)) { watcher.disconnect(); start(); }
      }, {rootMargin: "300px"});
      watcher.observe(mapBox);
    } else start();
"""

# The map's library and tiles are fetched later, by the script; this only warms the way to them.
MAP_HEAD = ('<link rel="preconnect" href="https://cdnjs.cloudflare.com" crossorigin>'
            '<link rel="preconnect" href="https://tiles.openfreemap.org" crossorigin>')


SCRIPT = """
<script>
  {
    const colors = %s, labels = %s;
    const rows = [...document.querySelectorAll(".row")];
    const buttons = [...document.querySelectorAll(".filter button")];
    const search = document.querySelector(".search");
    /*MAP*/
    const bySize = [...rows].sort((a, b) => b.dataset.units - a.dataset.units);
    // Every project shows at every zoom: at the zooms a reader starts at as its own dot, and further out, where
    // the whole region is in view and the dots would be a carpet, gathered into groups saying how many homes.
    // Recent updates, Large projects and Have your say fit their few projects in view instead of opening wide.
    const fitting = dotMap.container.hasAttribute("data-fit");
    let searching = false, pinned = null;  // Neither changes what's drawn now; the map holds them all.
    const draw = () => {
      // The biggest first, so the smaller are drawn over them and a small project beside a big one can be clicked.
      dotMap.draw(bySize.filter(row => !row.hidden || row === pinned));
      dotMap.hint(dotMap.grouping() ? "Projects grouped · zoom in to separate them" : "Bigger dots, more homes");
    };
    dotMap.onZoom(draw);
    const show = () => {
      const chosen = buttons.find(button => button.getAttribute("aria-pressed") === "true").dataset.show;
      const words = search.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
      searching = words.length > 0;
      pinned = null;
      for (const row of rows) {
        const status = row.dataset.status;
        const fits = (chosen === "all" || (chosen === "active" ? status !== "complete" : status === chosen))
          && words.every(word => row.dataset.words.includes(word));
        row.hidden = !fits;
      }
      draw();
      // The page's own lists, not a map's credits (MapLibre's are a <details> too).
      for (const details of document.querySelectorAll("main > details")) {
        const showing = [...details.querySelectorAll(".row:not([hidden])")];
        const homes = showing.reduce((sum, row) => sum + +row.dataset.units, 0);
        details.querySelector(".count").textContent = showing.length
          ? `${showing.length} project${showing.length === 1 ? "" : "s"} · ${homes.toLocaleString()} homes` : "none";
        if (words.length && showing.length) details.open = true;
      }
    };
    for (const button of buttons) button.addEventListener("click", () => {
      buttons.forEach(other => other.setAttribute("aria-pressed", other === button));
      show();
    });
    search.addEventListener("input", show);
    // The faint line under a list's heading while it's stuck at the top: the one there whose list is still on
    // screen. Checked as the page scrolls, once a frame at most, and when a list opens or closes.
    {
      const headings = [...document.querySelectorAll("main > details > summary")];
      let queued = false;
      const mark = () => {
        queued = false;
        for (const heading of headings) {
          const list = heading.parentElement.getBoundingClientRect();
          heading.classList.toggle("stuck", heading.parentElement.open && list.top < 0 && list.bottom > heading.offsetHeight);
        }
      };
      const soon = () => { if (!queued) { queued = true; requestAnimationFrame(mark); } };
      addEventListener("scroll", soon, {passive: true});
      for (const heading of headings) heading.parentElement.addEventListener("toggle", soon);
      mark();
    }
    show();
    // Recent updates' few projects, wherever they are, all in view at once.
    if (fitting && rows.length) dotMap.fit(rows);

    // A project's panel: opened from its row or its dot, with its id in the address so it can be linked to.
    // Their details are a file of their own (the same for every page, so a browser fetches it once), asked for
    // as a project is about to be opened rather than with the page.
    let panels = null, asking = null;
    const details = () => asking || (asking = fetch("/panels.json").then(answer => answer.json())
      .then(all => (panels = all)));
    // Pointing at a row is a good sign one's about to be opened; failing that, once the page is quiet.
    addEventListener("pointerover", event => { if (event.target.closest(".row, .fresh-one")) details(); },
                     {once: true, passive: true});
    (window.requestIdleCallback || (then => setTimeout(then, 2500)))(details);
    const view = document.querySelector("dialog.project");
    const part = name => view.querySelector(".panel-" + name);
    const make = (tag, props, ...children) => { const el = Object.assign(document.createElement(tag), props); el.append(...children); return el; };
    let shown = null, pushed = false;
    // Through the list from inside the panel: what the filter and search show, town by town, in the page's order.
    const beside = step => {
      const showing = rows.filter(row => !row.hidden);
      const at = showing.indexOf(shown);
      return at < 0 ? null : showing[at + step] || null;
    };
    function fill(row) {
      shown = row;
      const p = panels[row.id];
      rows.forEach(other => other.classList.toggle("lit", other === row));
      part("where").textContent = [p.town, p.neighborhood].filter(Boolean).join(" · ");
      part("title").textContent = p.name;
      part("pills").replaceChildren(make("span", {className: "panel-pill"}, row.querySelector(".icon").cloneNode(true), labels[p.status]),
        make("span", {className: "panel-pill"}, p.units.toLocaleString() + (p.units === 1 ? " home" : " homes")));
      part("note").textContent = p.note;
      // Have your say: each open comment period and upcoming meeting, linking to where to comment or to join.
      part("change").hidden = !p.change.length;
      part("change").querySelector("p").textContent = p.change[0] || "";
      part("say").hidden = !p.say.length;
      // Each one says where a comment goes, under it: a date on its own leaves a reader nowhere to go.
      const told = new Set();  // The same words under each of a project's three meetings would only be noise.
      part("say").querySelector("ul").replaceChildren(...p.say.map(([text, link, kind, how]) => {
        const first = how && !told.has(how);
        if (how) told.add(how);
        return make("li", {},
          make("a", {href: link, target: "_blank", rel: "noopener",
                     textContent: text + (kind === "comment" ? " · Comment ↗" : " ↗")}),
          ...(first ? [make("p", {className: "say-how", textContent: how})] : []));
      }));
      part("facts").replaceChildren(...p.facts.flatMap(([label, value]) => [make("dt", {textContent: label}), make("dd", {textContent: value})]));
      part("about").replaceChildren(...p.description.split(/\\n+/).map(text => text.trim()).filter(Boolean)
        .map(text => make("p", {textContent: text})));
      const image = part("image"), img = image.querySelector("img");
      image.classList.remove("loaded");
      image.hidden = !p.image;
      view.classList.toggle("shows-picture", !!p.image);
      if (p.image) img.src = p.image; else img.removeAttribute("src");
      const credit = image.querySelector(".panel-credit");
      credit.textContent = p.credit || "";
      credit.hidden = !p.credit;
      // The same picture again, already loaded, fires no load event of its own.
      if (p.image && img.complete && img.naturalWidth) image.classList.add("loaded");
      part("source").hidden = !p.link;
      part("source").href = p.link;
      part("source").textContent = (p.origin ? "View on " + p.origin : "View source") + " ↗";
      part("copy").textContent = "Copy link";
      // A correction, as an issue with the project named and the fields a note can set; the site says nothing
      // until it's been judged against the city's own record.
      part("wrong").href = REPO + "/issues/new?" + new URLSearchParams({
        title: `Note ${shown.id}`,
        labels: "correction",
        body: [`<!-- ${p.name} · ${location.origin}/p/${shown.id}/ -->`, "",
               "What’s wrong, or what’s new:", "", "", "",
               "status: (proposed, approved, under construction, complete, stalled — blank keeps it as it is)",
               "date: (2026-09-22, optional)", "link: (optional)", ""].join("\\n"),
      });
      part("site").hidden = !p.site;
      part("site").href = p.site;
      part("updated").textContent = p.updated ? "Last update " + p.updated : "";
      part("body").scrollTop = 0;
      part("back").disabled = !beside(-1);
      part("on").disabled = !beside(1);
    }
    const img = part("image").querySelector("img");
    img.addEventListener("load", () => part("image").classList.add("loaded"));
    img.addEventListener("error", () => {
      if (!img.getAttribute("src")) return;
      part("image").hidden = true;
      view.classList.remove("shows-picture");
    });
    // This page's address with a project's panel open in it (?project=<id>), or with none.
    const withProject = id => {
      const url = new URL(location.href);
      if (id) url.searchParams.set("project", id); else url.searchParams.delete("project");
      url.hash = "";
      return url.pathname + url.search;
    };
    async function openProject(row, push) {
      await details();
      fill(row);
      if (push) { history.pushState(null, "", withProject(row.id)); pushed = true; } else history.replaceState(null, "", withProject(row.id));
      if (!view.open) { view.showModal(); document.body.classList.add("viewing"); }
    }
    const closeProject = () => { if (view.open) view.close(); };
    // Closing takes its address away: Back, where opening it added one; otherwise in place.
    view.addEventListener("close", () => {
      document.body.classList.remove("viewing");
      if (pushed) { pushed = false; history.back(); } else history.replaceState(null, "", withProject(null));
    });
    const step = where => { const near = beside(where); if (near) openProject(near, false); };
    part("close").addEventListener("click", closeProject);
    // The panel's address, which is the project's, to share: the whole of it, this page's and ?project=.
    part("copy").addEventListener("click", async () => {
      // The share page's address, not this one's: shown in a message or a post, it carries the project's own
      // picture and words, and whoever opens it lands back here, on its panel.
      const link = new URL("/p/" + shown.id + "/", location.href).href;
      try { await navigator.clipboard.writeText(link); part("copy").textContent = "Copied"; }
      catch { part("copy").textContent = "Couldn’t copy"; }
    });
    // Esc, wherever focus is: a panel opened by a link as the page loads has nothing in it focused, and the
    // browser's own Esc for a dialog waits for focus inside it.
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && view.open) { event.preventDefault(); closeProject(); }
    });
    part("back").addEventListener("click", () => step(-1));
    part("on").addEventListener("click", () => step(1));
    view.addEventListener("keydown", event => {
      const where = {ArrowLeft: -1, ArrowRight: 1}[event.key];
      if (!where || event.metaKey || event.ctrlKey || event.altKey) return;
      event.preventDefault();
      step(where);
    });
    // A click outside it, on the backdrop, closes it; a press that began inside and drifted out doesn't.
    let pressedIn = false;
    view.addEventListener("pointerdown", event => { pressedIn = event.target !== view; });
    view.addEventListener("click", event => { if (event.target === view && !pressedIn) closeProject(); });
    // Its dot, found and opened on the map.
    part("map").addEventListener("click", () => {
      const row = shown;
      closeProject();
      pinned = row;
      draw();
      dotMap.container.scrollIntoView({behavior: "smooth", block: "center"});
      dotMap.goTo(row);
    });
    // Pointing at a row lights its dot on the map, as pointing at the dot lights the row.
    for (const [at, row] of rows.entries()) {
      row.addEventListener("pointerenter", () => dotMap.light && dotMap.light(at, true));
      row.addEventListener("pointerleave", () => dotMap.light && dotMap.light(at, false));
    }
    // Clicking anywhere on a row opens it; with a modifier key, its address opens in a new tab as usual.
    for (const row of rows) row.addEventListener("click", event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      openProject(row, true);
    });
    // A link to a project (?project=<id>, or #<id> from before) opens its panel, with its town's list open behind
    // it; Back and Forward follow the address.
    const fromAddress = async () => {
      const id = new URLSearchParams(location.search).get("project") || decodeURIComponent(location.hash.slice(1));
      const row = id && document.getElementById(id);
      if (row && row.matches("details")) {  // A town's, from an old link: its list, open.
        if (view.open) { pushed = false; view.close(); }
        row.open = true;
        row.scrollIntoView({block: "start"});
      } else if (row && row.classList.contains("row")) {
        row.closest("details").open = true;
        if (view.open) { await details(); fill(row); } else openProject(row, false);
      } else if (view.open) { pushed = false; view.close(); }
    };
    addEventListener("popstate", () => { pushed = false; fromAddress(); });
    fromAddress();
  }
</script>"""


def source_note(town, rows, today):
    """Where a town's list comes from, and for a MassBuilds town, when anything in it was last updated, since
    some towns' entries go a year or more without."""
    # Said as the page it opens says itself, so the link tells a reader — and a search engine — where it goes.
    page = f' <a class="to-town" href="/{slug(town)}/">New housing in {html.escape(town)} →</a>'
    if town in FROM:
        return f'<p class="from">From {FROM[town]}, which lists {LISTS[town]}.{page}</p>'
    if town not in INNER_RING:
        return ""
    latest = max((p["dated"] for p in rows if p["id"].startswith("massbuilds-") and p["dated"]), default=None)
    stale = latest and (today - latest).days > 180
    return (f'<p class="from">From <a href="https://www.massbuilds.com/">MassBuilds</a>'
            f'{f", last updated here {when(latest, today)}" if latest else ""}'
            f'{", so it may be out of date" if stale else ""}.{page}</p>')


def recently_changed(projects, today):
    """The projects with something that happened in the last RECENT_DAYS days, newest first, with what."""
    found = [(project, change(project, today)) for project in projects]
    found = [(project, changed) for project, changed in found if changed]
    return sorted(found, key=lambda pair: (pair[1][0], pair[0]["units"]), reverse=True)


def fresh_banner(recent, today, more="/recent/"):
    """The home page's Recently updated lines: the newest few, under the heading, and the way to the rest. They
    stay when nothing's changed lately, saying so."""
    fresh = recent[:BANNER_LINES]
    if fresh:
        lines = "".join(
            f'<a class="fresh-one" href="?project={html.escape(project["id"])}">{status_icon(project["status"])}'
            f'<b>{html.escape(project["name"])}</b> · '
            f'{html.escape(changed[1])} · {html.escape(when(changed[0], today))}</a>' for project, changed in fresh)
    else:
        lines = f'<span class="fresh-one fresh-none">Nothing’s changed in the last {RECENT_DAYS} days</span>'
    # The way to the rest (Recent updates), except on Recent updates, which is the rest.
    see_all = f'<a class="fresh-more" href="{more}">See all →</a>' if more else ""
    return (f'<p class="fresh"><span class="fresh-tag">{SPARK_MARK}Recently updated</span>{see_all}'
            f'<span class="fresh-list">{lines}</span></p>')


def slug(town):
    return re.sub(r"[^a-z0-9]+", "-", town.lower()).strip("-")


# The site's pages, besides the home page: each one's path, name, and what it says of itself under the header.
PAGES = {
    "recent": ("recent/", "Recent updates", f"What’s changed in the last {RECENT_DAYS} days."),
    "large": ("large/", "Large projects", f"The biggest projects, {LARGE_HOMES} homes or more, and how far along each is."),
    "say": ("have-your-say/", "Have your say", "Upcoming public meetings and open comment periods on Boston’s projects, "
                                               "soonest first."),
}


def say_line(projects, today, more, none=None):
    """Have your say, a line under Recently updated: the soonest comment deadline or meeting, which stays put (the
    soonest is the one that matters), and the way to the rest. With nothing coming up it says none, if a page has
    words for that (a town's: only Boston and Cambridge publish any), and is left out otherwise."""
    coming = sorted(((say_day(p["say"][0]), p) for p in projects if p.get("say")), key=lambda pair: pair[0])
    if not coming and not none:
        return ""
    see_all = f'<a class="fresh-more" href="{more}">See all →</a>' if more else ""
    if coming:
        lines = "".join(
            f'<a class="fresh-one" href="?project={html.escape(project["id"])}">'
            f'{shared.icon(project["say"][0]["kind"])}<b>{html.escape(project["name"])}</b> · '
            f'{html.escape(say_text(project["say"][0], today, short=True))}</a>'
            for _, project in coming[:BANNER_LINES])
    else:
        lines = f'<span class="fresh-one fresh-none">{html.escape(none)}</span>'
    return (f'<p class="fresh say"><span class="fresh-tag">{SAY_MARK}Have your say</span>{see_all}'
            f'<span class="fresh-list">{lines}</span></p>')


def subscribe(path="say.ics", label="these dates"):
    """The ways to subscribe to a calendar of ours. A browser with no calendar app behind it does nothing at all
    with webcal://, which is most of Chrome, so Google's own way in is offered beside it and the file itself
    under them both."""
    web = f"webcal://buildhousing.org/{path}"
    google = "https://calendar.google.com/calendar/r?cid=" + urllib.parse.quote(web, safe="")
    return (f'<p class="how-do"><a class="do primary" href="{google}" target="_blank" rel="noopener">'
            f'{CALENDAR_MARK}Google Calendar</a>'
            f'<a class="do" href="{web}">Apple Calendar or Outlook</a>'
            f'<a class="do" href="/{path}" download>Download the file</a></p>'
            f'<p class="how-follow">Subscribing keeps {label} up to date by itself, so a deadline that moves moves '
            'with it; the downloaded file is a copy of today’s.</p>')


def how_to_be_heard():
    """Under Have your say: what a comment does, what makes one count, and where each city takes them. Dates on
    their own tell a reader when to act, not how."""
    return (
        '<section class="how"><h2>How to be heard</h2>'
        '<p>A project under review is decided by a board, and what neighbours write in is part of the record it '
        'decides on. A comment counts for more when it says who you are and where you live, names the project and '
        'its address, and says plainly what you want done and why — a few honest sentences of your own beat a form '
        'letter.</p>'
        '<ul><li><b>Boston:</b> comment on the form at the foot of the project’s page on bostonplans.org, before '
        'the period closes. The page names the city planner handling it, who takes comments by email too, and '
        'meetings listed here are open to anyone.</li>'
        f'<li><b>Cambridge:</b> email <a href="mailto:{CAMBRIDGE_COMMENT}">{CAMBRIDGE_COMMENT}</a> with the '
        'project’s address and case number, by 5 pm the day before the meeting, or register on the '
        f'<a href="{CAMBRIDGE_BOARD}">Planning Board’s page</a> to speak at the hearing itself. The board may not '
        'take public comment on an item that isn’t listed as a hearing.</li>'
        '<li><b>The towns around them:</b> most publish neither deadlines nor agendas anywhere we can read, so '
        'their projects show no dates here. Their planning department or town clerk will say when the board next '
        'meets.</li></ul>'
        '<h3>Keep the dates</h3>' + subscribe() + '</section>')


def about(projects, towns):
    """What the site is, in a line, for the foot of each page, with the numbers as of this build. Its own name is
    the one piece of markup in it, so a reader sees it for the name it is."""
    active = [p for p in projects if p["status"] != "complete"]
    return (
        f"<b>{html.escape(NAME)}</b> follows new housing in Boston and the {len(towns) - 1} towns around it, from "
        f"first filing to move-in: {len(projects):,} projects, {sum(p['units'] for p in projects):,} homes, "
        f"{len(active):,} still in progress. It reads Boston’s Planning Department, Cambridge’s development log and "
        "MAPC’s MassBuilds every few hours, and lifts a project to the top of its town’s list each time it moves "
        "along."
    )


def footer(here, towns, said):
    """The foot of each page, as Pushpin's: what the site is (said, in full), then its pages, its towns and its
    sources in short lists under faint headings (the page it's on, at here, marked), then where the data comes
    from. Its links, like every link between the site's pages, are from the site's root."""
    marked = ' aria-current="page"'
    groups = [
        ("Browse", "browse", [("All projects", "")] + [(name, path) for path, name, _ in PAGES.values()]),
        ("Towns", "towns", [(town, f"{slug(town)}/") for town in towns]),
        ("Sources", "sources", [("Boston Planning", "https://www.bostonplans.org/projects/development-projects"),
                                ("Cambridge log", CAMBRIDGE_PAGE), ("MassBuilds", "https://www.massbuilds.com/")]),
        # A calendar app is given the address to subscribe to, not one to download the once: webcal:// asks it to
        # keep the deadlines and meetings up to date by itself.
        ("Follow", "follow", [("Deadlines by calendar", "webcal://buildhousing.org/say.ics"),
                              ("Updates by RSS", "updates.xml")]),
    ]
    def link(label, href):
        outside = href.startswith(("http", "webcal"))
        current = marked if not outside and href == here else ""
        return f'<li><a href="{href if outside else "/" + href}"{current}>{html.escape(label)}</a></li>'
    lists = "".join(
        f'<div class="{kind}"><h2>{heading}</h2><ul>{"".join(link(label, href) for label, href in links)}</ul></div>'
        for heading, kind, links in groups
    )
    return (
        f'<footer><p class="foot-about">{said}</p>'
        f'<nav class="site-links" aria-label="{html.escape(NAME)}">{lists}</nav></footer>'
    )


def by_town(projects):
    """The towns with projects, Boston first as the city the rest are around, then the others by name."""
    return sorted({project["town"] for project in projects}, key=lambda town: (town != "Boston", town))


def as_data(page, town, projects, today, title, description):
    """What the page is, for search engines to read: the site itself; a town's list of projects; and Have your
    say's meetings as events, each with when it starts and where to join."""
    url = SITE_URL + (f"{slug(town)}/" if page == "town" else PAGES[page][0] if page else "")
    graph = [{"@type": "WebSite", "@id": SITE_URL, "name": NAME, "url": SITE_URL, "description": TAGLINE,
              "inLanguage": "en-US"},
             {"@type": "WebPage", "name": title, "url": url, "description": description, "isPartOf": {"@id": SITE_URL}}]
    if page == "town":
        graph.append({"@type": "ItemList", "name": f"Housing projects in {town}", "url": url,
                      "numberOfItems": len(projects), "itemListElement": [
                          {"@type": "ListItem", "position": at, "name": project["name"],
                           "url": f"{url}?project={project['id']}"}
                          for at, project in enumerate(projects[:100], start=1)]})
    if page == "say":
        for project in projects:
            for item in project.get("say", []):
                if item["kind"] != "meeting":
                    continue
                starts = item["when"]
                graph.append({
                    "@type": "Event", "name": f"{item['what']}: {project['name']}",
                    "startDate": starts.isoformat(),
                    "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode",
                    "eventStatus": "https://schema.org/EventScheduled",
                    "location": {"@type": "VirtualLocation", "url": item["link"]},
                    "organizer": {"@type": "Organization",
                                  "name": "Boston Planning Department" if project["id"].startswith("boston-")
                                  else "Cambridge Planning Board"},
                    "description": f"{item['what']} about {project['name']}, {project['units']:,} homes in "
                                   f"{project['neighborhood'] or project['town']}.",
                    "url": item["link"],
                })
    data = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{data}</script>'


def head(path, title, description, picture=None, mapped=True):
    """What a page tells search engines and link previews: what it's about, and its one address."""
    url = SITE_URL + path
    # The picture a link to the site shows (housing/static/share.png, drawn by make_icons.py): the house and the
    # statuses' rings on the site's black. The words of a preview are the page's own title and description.
    return (f'<meta name="description" content="{html.escape(description)}">'
            f'<link rel="canonical" href="{url}">'
            f'<meta property="og:type" content="website"><meta property="og:site_name" content="{html.escape(NAME)}">'
            f'<meta property="og:title" content="{html.escape(title)}">'
            f'<meta property="og:description" content="{html.escape(description)}"><meta property="og:url" content="{url}">'
            f'<meta property="og:image" content="{picture or SITE_URL + "share.png"}">'
            # The card's size, which a project's own picture doesn't share.
            + ('' if picture else '<meta property="og:image:width" content="1200">'
                                  '<meta property="og:image:height" content="630">')
            + f'<meta property="og:image:alt" content="{html.escape(NAME)}">'
            '<meta name="twitter:card" content="summary_large_image">'
            + f'<link rel="alternate" type="application/rss+xml" title="{html.escape(NAME)}: recent updates" '
              f'href="{SITE_URL}updates.xml">'
            + icons_head() + (MAP_HEAD if mapped else ""))


def page_heading(page, town):
    """A page's own heading, for search engines and screen readers: the header shows it as the site's name and
    the page's, which read as one line there but aren't a heading."""
    return (f"New housing in {town}" if page == "town" else PAGES[page][1] if page
            else "New housing around Boston")


def counted(projects, status):
    return sum(1 for project in projects if project["status"] == status)


def town_note(town, projects, today):
    """A town's page in a line, which stands as its tagline: how much is under way and at what stage, the largest
    of it, what's been finished, and where the numbers come from. Empty for a town with nothing at all."""
    active = [p for p in projects if p["status"] != "complete"]
    done = [p for p in projects if p["status"] == "complete"]
    if not active and not done:
        return ""
    from_ = FROM[town] if town in FROM else "MAPC’s MassBuilds"
    latest = max((p["dated"] for p in projects if p["dated"]), default=None)
    source = f'From {from_}{f", updated {when(latest, today)}" if latest else ""}.'
    finished = (f', and {len(done):,} finished since {COMPLETE_SINCE}' if done else "")
    if not active:
        return (f'{html.escape(town)} has nothing under way; {len(done):,} '
                f'{"project has" if len(done) == 1 else "projects have"} been finished since {COMPLETE_SINCE}. {source}')
    stages = ", ".join(f"{counted(active, key)} {label.lower()}" for key, (label, _) in STATUSES.items()
                       if key != "complete" and counted(active, key))
    biggest = max(active, key=lambda project: project["units"])
    return (f'{html.escape(town)} has {len(active):,} housing '
            f'{"project" if len(active) == 1 else "projects"} under way, {sum(p["units"] for p in active):,} homes '
            f'in all: {stages}, the largest {html.escape(biggest["name"])} ({biggest["units"]:,} homes)'
            f'{finished}. {source}')


def render(projects, built_at, failed, page=None, town=None):
    """The home page, every project by town; or one of PAGES, each a single list across the towns: Recent
    updates, what's changed in the last RECENT_DAYS days, newest first, each saying what happened; Large
    projects, those of LARGE_HOMES homes or more, biggest first, each with how far along it is; or, with page
    "town", a town's page: its projects, most recently changed first."""
    today = built_at.date()
    everything = projects  # All of them, for the footer's numbers, whichever this page shows.
    towns = by_town(projects)
    changes = recently_changed(projects, today)
    path = f"{slug(town)}/" if page == "town" else PAGES[page][0] if page else ""
    if page == "town":
        order = lambda project: (updated(project) or date.min, project["units"])
        projects = sorted((p for p in projects if p["town"] == town), key=order, reverse=True)
        changes = [(p, c) for p, c in changes if p["town"] == town]
        rows = "".join(row(project, today) for project in projects)
        heading = html.escape(town)
    elif page == "recent":
        projects = [project for project, _ in changes]
        rows = "".join(row(project, today, changed) for project, changed in changes)
        heading = f"In the last {RECENT_DAYS} days"
    elif page == "say":
        coming = sorted((p for p in projects if p.get("say")), key=lambda p: say_day(p["say"][0]))
        projects = coming
        rows = "".join(row(p, today, (say_day(p["say"][0]), say_text(p["say"][0], today))) for p in coming)
        heading = "Coming up"
    elif page == "large":
        projects = sorted((p for p in projects if p["units"] >= LARGE_HOMES), key=lambda p: p["units"], reverse=True)
        rows = "".join(row(project, today, large=True) for project in projects)
        heading = f"{LARGE_HOMES} homes or more"
    if page:
        note = source_note(town, projects, today).replace(f' <a class="to-town" href="/{slug(town)}/">', '<a hidden>') \
            if page == "town" else ""
        sections = (f'<details open><summary><span class="town">{heading}</span><span class="count"></span></summary>'
                    f'{note}<ul>{rows}</ul></details>')
    else:
        order = lambda project: (updated(project) or date.min, project["units"])
        in_town = {}
        for project in sorted(projects, key=order, reverse=True):
            in_town.setdefault(project["town"], []).append(project)
        sections = "".join(
            f'<details id="town-{slug(town)}"><summary><span class="town">{html.escape(town)}</span>'
            f'<span class="count"></span></summary>{source_note(town, in_town[town], today)}'
            f'<ul>{"".join(row(project, today) for project in in_town[town])}</ul></details>'
            for town in towns
        )
    # The pages across the towns show every status, a finished project among them; the home page and a town's,
    # what's under way.
    shown = "all" if page in ("recent", "large", "say") else "active"
    choices = [("active", "In progress")] + [(key, label) for key, (label, _) in STATUSES.items()] + [("all", "All")]
    def colored(key):
        return f' style="color:{STATUSES[key][1]}"' if key in STATUSES else ""
    # Under the map: the filter on the left, the search on the right.
    filter_row = (
        '<div class="controls"><div class="filter">' + "".join(
            f'<button type="button" data-show="{key}" aria-pressed="{"true" if key == shown else "false"}">'
            f'<span{colored(key)}>{shared.icon(key.replace(" ", "-"))}</span>{label}</button>'
            for key, label in choices
        ) + f'</div>{shared.SEARCH}</div>'
    )
    # Named by source, not town: MassBuilds down is eleven towns at once.
    unreached = list(dict.fromkeys("MassBuilds" if town in INNER_RING else FROM.get(town, town) for town in failed))
    missing = (f'<p class="empty">Couldn’t reach {" or ".join(unreached)} this time; showing what the last build had.</p>'
               if failed else "")
    active = [p for p in projects if p["status"] != "complete"]
    town_said = (f"New housing in {town}: {len(active):,} projects in progress, "
                 f"{sum(p['units'] for p in active):,} homes, from proposal to move-in.")
    # A town says it all in its one line, the way to the rest of them at the end of it.
    said = (f'{town_note(town, projects, today) or html.escape(town_said)} <a href="/">All towns →</a>'
            if page == "town" else f'{PAGES[page][2]} <a href="/">All projects →</a>' if page else html.escape(TAGLINE))
    intro = (f'<h1 class="page-heading">{html.escape(page_heading(page, town))}</h1>'
             f'<div class="intro"><p class="tagline">{said}</p></div>')
    # Over every page's map: on Large projects, only its own projects' changes, so each opens here.
    # Both banners over every page's map, except the two pages that are a banner at length: Recent updates and
    # Have your say, where the lines would be the first rows under them said twice. On Large projects, only the
    # changes among its own projects, so a line opens where it stands.
    if page in ("recent", "say"):
        banner = ""
    else:
        banner = fresh_banner([(p, c) for p, c in changes if page != "large" or p["units"] >= LARGE_HOMES], today)
        # Under it, the soonest chances to have a say among the projects that page lists. A town that publishes
        # no comment periods or meetings says so, rather than leaving the line out.
        nothing = (f"{town} doesn’t publish comment periods or meetings" if page == "town"
                   and town not in ("Boston", "Cambridge") else None)
        banner += say_line(projects, today, more="/have-your-say/", none=nothing)
    # Under Have your say, how to say it; under a town's list, that town's own calendar.
    after = (how_to_be_heard() if page == "say" else
             f'<section class="how"><h3>{html.escape(town)}’s deadlines and meetings, in your calendar</h3>'
             + subscribe(f"{slug(town)}/say.ics", f"{html.escape(town)}’s dates") + '</section>' if page == "town"
             else '<p class="follow"><a href="/updates.xml">Follow these updates by RSS →</a></p>'
             if page == "recent" else "")
    body = (f'{intro}{banner}<div id="map"{" data-fit" if page else ""}></div>{filter_row}{missing}{sections}{after}'
            f'{footer(path, towns, about(everything, towns))}{PANEL}'
            f'<script>const REPO = {json.dumps(REPO_URL)};</script>')
    script = (SCRIPT % (json.dumps({key: color for key, (_, color) in STATUSES.items()}),
                        json.dumps({key: label for key, (label, _) in STATUSES.items()}))).replace(
        "    /*MAP*/\n", MAP_JS) + FRESH_SCRIPT
    # After the site's name: the page's, or on the home page the city it's all in.
    name = town if page == "town" else PAGES[page][1] if page else PLACE
    title = (f"New housing in {town} · {NAME}" if page == "town" else f"{name} · {NAME}" if page else
             f"{NAME}: new housing in {PLACE}, Cambridge and the towns around it")
    description = (town_said if page == "town" else PAGES[page][2] if page else
                   f"{TAGLINE} {len(active):,} projects in progress across Boston and the towns around it, on a map "
                   "and in a list for each town, with their status, size and details.")
    return shared.page(
        "news", title, body + script, css=CSS,
        head=head(path, title, description) + as_data(page, town, projects, today, title, description),
        symbols=ICON_SYMBOLS,
        updated=built_at, links=[(NAME, "/", not page)], marked={NAME: MARK + html.escape(NAME)}, here=name,
        indexable=True,
    )


def share_page(project, today):
    """A project's page for links to it: its own picture and words for whoever shows the link, what it is in a
    few lines for anyone who lands here, and, for a reader whose browser runs scripts, the way on to its panel
    where the site opens projects."""
    label = STATUSES[project["status"]][0]
    homes = f'{project["units"]:,} home{"s" if project["units"] != 1 else ""}'
    place = ", ".join(part for part in (project["neighborhood"], project["town"]) if part)
    panel = f"/?project={project['id']}"
    path = f"p/{project['id']}/"
    title = f"{project['name']}, {place} · {NAME}"
    lead = project["description"].split("\n")[0].strip()
    description = f"{project['name']} in {place}: {homes}, {label.lower()}. {lead}".strip()
    if len(description) > 160:
        description = description[:157].rsplit(" ", 1)[0] + "…"
    said = "".join(part for part in (
        f'<p>{html.escape(homes)} · {html.escape(label)} · {html.escape(place)}.</p>',
        f'<p>{html.escape(lead)}</p>' if lead else "",
    ) if part)
    # A page of its own, not the site's: whoever lands here is on their way to its panel, so it carries none of
    # the site's styles or scripts, only what a link shows and a few lines for a reader without them.
    return (
        f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n<meta name="robots" content="index, follow">\n'
        '<meta name="theme-color" content="#000000">\n'
        f'{head(path, title, description, picture=project.get("image") or None, mapped=False)}\n'
        f'<title>{html.escape(title)}</title>\n'
        '<style>body { margin: 0; padding: 2rem 1.25rem; color: #ddd; background: #000;\n'
        '  font: 17px/1.5 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }\n'
        '  main { max-width: 46rem; margin: 0 auto; } h1 { margin: 0 0 .5rem; color: #fff; font-size: 1.5rem; }\n'
        '  p { margin: 0 0 .6rem; color: #bbb; } a { color: #fff; }</style>\n'
        f'</head>\n<body>\n<main><h1>{html.escape(project["name"])}</h1>{said}'
        f'<p><a href="{panel}">Open it on the map →</a></p></main>\n'
        f'<script>location.replace({json.dumps(panel)});</script>\n</body>\n</html>\n'
    )


def sitemap(projects, towns, built_at):
    """Every page, for search engines, each project's share page with the day it last changed."""
    paths = [("", built_at.date())] + [(path, built_at.date()) for path, _, _ in PAGES.values()]
    paths += [(f"{slug(town)}/", built_at.date()) for town in towns]
    # A project's share page, which leads on to its panel: what a link to a project points at.
    paths += [(f"p/{project['id']}/", updated(project)) for project in projects]
    entries = "".join(f"<url><loc>{SITE_URL}{path}</loc>{f'<lastmod>{day.isoformat()}</lastmod>' if day else ''}</url>"
                      for path, day in paths)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>\n'


def ics_text(value):
    """A value as a calendar file writes it, with what it reads as punctuation held back."""
    return (str(value).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def folded(line):
    """A line kept to the 75 octets a calendar file allows, the rest carried on a line beginning with a space."""
    raw, pieces, first = line.encode(), [], True
    while raw:
        room = 75 if first else 74
        cut = min(room, len(raw))
        while 0 < cut < len(raw) and raw[cut] & 0xC0 == 0x80:  # Never split a character down the middle.
            cut -= 1
        pieces.append(("" if first else " ") + raw[:cut].decode())
        raw, first = raw[cut:], False
    return "\r\n".join(pieces)


def calendar(projects, built_at, town=None):
    """Every comment deadline and meeting to come, as a calendar to subscribe to: a deadline is the day it falls
    on, a meeting the two hours from when it starts. Each keeps the same id from build to build, so a calendar
    already subscribed to moves an event rather than adding a second one."""
    stamp = built_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    where = f" in {town}" if town else ""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{NAME}//buildhousing.org//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_text(f'{NAME}: have your say{where}')}",
             f"X-WR-CALDESC:{ics_text(f'Comment deadlines and public meetings on new housing{where}, from {SITE_URL}')}",
             "X-WR-TIMEZONE:America/New_York", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for project in projects:
        for item in project.get("say", []):
            day = say_day(item)
            moment = item["when"]
            if isinstance(moment, datetime):
                times = [f"DTSTART:{moment.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
                         f"DTEND:{(moment + timedelta(hours=2)).astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"]
            else:  # A deadline, and a meeting whose source gives no time, take the whole day.
                times = [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]
            summary = (f"Comments close: {project['name']}" if item["kind"] == "comment"
                       else f"{project['name']}: {item['what']}")
            about = (f"{project['units']:,} homes, {STATUSES[project['status']][0].lower()}, "
                     f"{project['neighborhood'] or project['town']}.")
            told = f"{about} {item.get('how') or ''}".strip()
            lines += ["BEGIN:VEVENT", f"UID:{project['id']}-{item['kind']}-{day:%Y%m%d}@buildhousing.org",
                      f"DTSTAMP:{stamp}"] + times + [
                      f"SUMMARY:{ics_text(summary)}",
                      f"DESCRIPTION:{ics_text(told + chr(10) + SITE_URL + 'p/' + project['id'] + '/')}",
                      f"URL:{SITE_URL}p/{project['id']}/", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded(line) for line in lines) + "\r\n"


def updates_feed(changes, built_at):
    """What's changed lately, as a feed to follow: one item for each project that's moved along, newest first."""
    items = []
    for project, (day, what) in changes[:50]:
        where = f"{project['neighborhood']}, {project['town']}" if project["neighborhood"] else project["town"]
        told = (f"{what} · {project['units']:,} homes · {where}")
        title = f"{project['name']}: {what}"
        items.append(
            f"<item><title>{html.escape(title)}</title>"
            f"<link>{SITE_URL}p/{project['id']}/</link>"
            f"<guid isPermaLink=\"false\">{project['id']}-{what.lower().replace(' ', '-')}-{day.isoformat()}</guid>"
            f"<pubDate>{format_datetime(datetime.combine(day, datetime.min.time(), BOSTON))}</pubDate>"
            f"<description>{html.escape(told)}</description></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            f"<title>{html.escape(f'{NAME}: recent updates')}</title>"
            f"<link>{SITE_URL}recent/</link>"
            f"<description>{html.escape(f'New housing around {PLACE} as it is filed, approved, built and finished.')}</description>"
            f'<language>en-us</language><lastBuildDate>{format_datetime(built_at)}</lastBuildDate>'
            f'<atom:link href="{SITE_URL}updates.xml" rel="self" type="application/rss+xml"/>'
            f"{''.join(items)}</channel></rss>\n")


# The rendering at the head of a Boston project's page on bostonplans.org, which the city's data doesn't carry.
BOSTON_IMAGE = re.compile(r"""bpdaInteriorHeaderImg">\s*<img src=["'](/getattachment/[^"']+)["']""")


def boston_image(link):
    """A Boston project's rendering, from its page; empty for one without."""
    try:
        page = shared.fetch(link, USER_AGENT, attempts=2, timeout=20)[1].decode("utf-8", "replace")
    except Exception as error:
        print(f"  no picture for {link} ({error})", file=sys.stderr)
        return None  # Tried again next build.
    found = BOSTON_IMAGE.search(page)
    return "https://www.bostonplans.org" + found.group(1) if found else ""


# Have your say: Boston's open comment periods, from each project's page, and its public meetings, from the
# Planning Department's calendar, whose feed runs a few months ahead and links each meeting to its project's page.
BOSTON = ZoneInfo("America/New_York")
CALENDAR_URL = "https://www.bostonplans.org/news-calendar/calendar?view=month&rss=relationship"
COMMENT_PERIOD = re.compile(r"Comment period ends (\w{3} \d{1,2}, \d{4})")
PROJECT_LINK = re.compile(r"bostonplans\.org(/projects/development-projects/[^\"'?#\s<>]+)", re.I)
# A project filed more recently than this may have a comment period open, whatever its status says.
FILED_WITHIN = timedelta(days=120)


def project_path(link):
    """A Boston project's page as its path alone, to match a meeting's link to it however it's written."""
    found = PROJECT_LINK.search(link or "")
    return found.group(1).lower().rstrip("/") if found else None


def comment_period(page):
    """The day a project page's comment period ends, or None when it has none."""
    found = COMMENT_PERIOD.search(page)
    return datetime.strptime(found.group(1), "%b %d, %Y").date() if found else None


# Cambridge's Planning Board meetings, in a table of what's scheduled for each: its items name their case
# numbers ("(PB-410)"), which the development log gives for each project as well.
CAMBRIDGE_BOARD = "https://www.cambridgema.gov/CDD/zoninganddevelopment/planningboard/planningboardmeetings"
BOARD_CASE = re.compile(r"\(\s*(PB[\s-]?\d+)\s*\)", re.I)

# The Cambridge Redevelopment Authority's own meetings, about its own projects (2400 Massachusetts Avenue and
# the rest). Its site gives the same page as JSON, with the meetings still to come in "upcoming".
# Where a comment actually goes, which the dates alone don't say. Boston takes them on the project's own page,
# on the form at the foot of it; Cambridge's Planning Board by email, with the case number, the day before.
BOSTON_COMMENT = "#comment_Form"
CAMBRIDGE_COMMENT = "planningboardcomment@cambridgema.gov"
CRA = "https://www.cambridgeredevelopment.org"
CRA_MEETINGS = CRA + "/meetings"
# How the two sources write a street between them: "2400 Mass Ave" is the city's "2400 Massachusetts Avenue".
STREET_WORDS = {"mass": "massachusetts", "ave": "avenue", "av": "avenue", "st": "street", "rd": "road",
                "dr": "drive", "ln": "lane", "pl": "place", "sq": "square", "ct": "court", "blvd": "boulevard",
                "pkwy": "parkway", "hwy": "highway", "ter": "terrace", "cir": "circle"}


def address_key(text):
    """The number and street an address names, for matching one source's wording against another's: "2400 Mass
    Ave" and "2400 Massachusetts Avenue" both come to ("2400", "massachusetts"). None where it names none."""
    words = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
    for at, word in enumerate(words[:-1]):
        if word.isdigit() and len(word) <= 5:
            street = words[at + 1]
            return word, STREET_WORDS.get(street, street)
    return None


def cra_meetings(feed, today):
    """The Redevelopment Authority's meetings from today on, as {when, what, link, about}: about is the address
    it's for, taken from its title, or failing that from where it's held."""
    found = []
    for item in json.loads(feed).get("upcoming", []):
        try:
            starts = datetime.fromtimestamp(item["startDate"] / 1000, BOSTON)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        if starts.date() < today:
            continue
        title = re.sub(r"\s+", " ", (item.get("title") or "").strip())
        # Some are written in capitals ("CRA BOARD MEETING"), which would shout on the page.
        if title and title == title.upper():
            title = re.sub(r"\bCra\b", "CRA", title.title())
        where = (item.get("location") or {}).get("addressLine1", "")
        found.append({"when": starts, "what": title or "CRA meeting", "link": CRA + (item.get("fullUrl") or ""),
                      "about": address_key(title) or address_key(where)})
    return sorted(found, key=lambda item: item["when"])


def board_meetings(page, today):
    """The Planning Board's meetings from today on, as {when, what, cases}: one for each item with a case number,
    a hearing said to be one. Its table holds a row for each meeting, the last column its items."""
    found = []
    for match in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
                 for cell in re.findall(r"<td[^>]*>(.*?)</td>", match.group(1), re.S)]
        if len(cells) < 3:
            continue
        try:
            day = datetime.strptime(cells[0].strip(), "%B %d, %Y").date()
        except ValueError:
            continue
        if day < today:
            continue
        for item in cells[-1].split("•")[1:]:
            case = BOARD_CASE.search(item)
            if not case:
                continue
            # "H earing – 9&25 Birch Street (PB-412) - Materials": the city's own spacing, taken as it comes.
            kind = "Planning Board hearing" if re.match(r"\s*h\s*earing", item, re.I) else "Planning Board meeting"
            found.append({"when": day, "what": kind, "cases": {re.sub(r"[^A-Z0-9]", "", case.group(1).upper())}})
    return sorted(found, key=lambda item: item["when"])


def meetings(feed, today):
    """The calendar feed's meetings from today on, each about the projects its description links to: a list of
    {paths, title, starts, link}, soonest first."""
    items = []
    for item in re.findall(r"<item>(.*?)</item>", feed, re.S):
        def field(name):
            found = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", item, re.S)
            return html.unescape(found.group(1).strip()) if found else ""
        try:
            starts = parsedate_to_datetime(field("pubDate")).astimezone(BOSTON)
        except (TypeError, ValueError):
            continue
        paths = {project_path(match.group(0)) for match in PROJECT_LINK.finditer(field("description"))}
        if starts.date() >= today and paths:
            items.append({"paths": paths, "title": field("title"), "starts": starts, "link": field("link")})
    return sorted(items, key=lambda item: item["starts"])


def clock(moment):
    """6 PM, 6:30 PM."""
    hour = moment.hour % 12 or 12
    return f"{hour}{f':{moment.minute:02d}' if moment.minute else ''} {'AM' if moment.hour < 12 else 'PM'}"


def meeting_kind(title, name):
    """What kind of meeting it is: its title with the project's name taken off its front ("25 Supertest Street IAG
    Meeting": "IAG Meeting"); a title that names it further in ("Discussion of 121B Agreement for One Mystic
    Avenue"), or not at all, whole."""
    if title.lower().startswith(name.lower()):
        return title[len(name):].strip(" -–|:/") or "Public meeting"
    return title


def add_say(projects, today):
    """Each Boston project's open comment period and upcoming meetings, as project["say"]: a list of
    {when, what, link}, soonest first. Pages are read for projects under review or filed lately; a source that
    can't be reached leaves its part out, and the build goes on."""
    wanted = [p for p in projects if p["id"].startswith("boston-") and p["link"] and p["status"] != "complete" and (
        p["status"] == "proposed" or (iso_date(p.get("filed")) and today - iso_date(p["filed"]) <= FILED_WITHIN))]
    def read(project):
        try:
            return comment_period(shared.fetch(project["link"], USER_AGENT, attempts=2, timeout=20)[1].decode("utf-8", "replace"))
        except Exception as error:
            print(f"  no comment period for {project['link']} ({error})", file=sys.stderr)
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        closes = dict(zip((p["id"] for p in wanted), pool.map(read, wanted)))
    try:
        calendar = meetings(shared.fetch(CALENDAR_URL, USER_AGENT, attempts=2, timeout=30)[1].decode("utf-8", "replace"), today)
    except Exception as error:
        print(f"✗ Boston's calendar: {error}", file=sys.stderr)
        calendar = []
    try:
        board = board_meetings(shared.fetch(CAMBRIDGE_BOARD, USER_AGENT, attempts=2, timeout=30)[1].decode("utf-8", "replace"), today)
    except Exception as error:
        print(f"✗ Cambridge's Planning Board: {error}", file=sys.stderr)
        board = []
    try:
        cra = cra_meetings(shared.fetch(CRA_MEETINGS + "?format=json", USER_AGENT, attempts=2, timeout=30)[1]
                           .decode("utf-8", "replace"), today)
    except Exception as error:
        print(f"✗ Cambridge Redevelopment Authority: {error}", file=sys.stderr)
        cra = []
    by_case = {p["case"]: p for p in projects if p.get("case")}
    by_path = {project_path(p["link"]): p for p in projects if p["id"].startswith("boston-")}
    for project in projects:
        project["say"] = []
        ends = closes.get(project["id"])
        if ends and ends >= today:
            project["say"].append({"when": ends, "what": "Comment period ends", "kind": "comment",
                                   "link": project["link"] + BOSTON_COMMENT,
                                   "how": "Comment on the form at the foot of the city’s project page, "
                                          "or by email to the planner it names, before the day is out."})
    for meeting in calendar:
        for path in meeting["paths"]:
            project = by_path.get(path)
            if project:
                project["say"].append({"when": meeting["starts"], "link": meeting["link"], "kind": "meeting",
                                       "what": meeting_kind(meeting["title"], project["name"]),
                                       "how": "Open to anyone; the city’s calendar entry says where it is and how "
                                              "to join, and comments can go to the project page as well."})
    for meeting in board:
        for case in meeting["cases"]:
            project = by_case.get(case)
            if project:
                told = (f"Email {CAMBRIDGE_COMMENT} with case {project['case']} by 5 pm the day before, or register "
                        "on the Planning Board’s page to speak at the meeting.")
                project["say"].append({"when": meeting["when"], "what": meeting["what"], "kind": "meeting",
                                       "link": CAMBRIDGE_BOARD,
                                       "how": told if meeting["what"].endswith("hearing") else
                                       f"Email {CAMBRIDGE_COMMENT} with case {project['case']}. The board may not "
                                       "take public comment on an item that isn’t a hearing."})
    # The Redevelopment Authority's meetings, each on the project at the address it names; one that names none
    # we track (its board's own meetings) has no project to sit under, and is left out.
    by_address = {}
    for project in projects:
        key = address_key(project["name"]) if project["town"] == "Cambridge" else None
        if key:
            by_address.setdefault(key, project)
    for meeting in cra:
        project = by_address.get(meeting["about"])
        if project:
            project["say"].append({"when": meeting["when"], "what": meeting["what"], "kind": "meeting",
                                   "link": meeting["link"],
                                   "how": "The Redevelopment Authority’s own meeting; its page says how to join."})
    for project in projects:
        project["say"].sort(key=say_day)
    open_ = sum(1 for p in projects for item in p["say"] if item["kind"] == "comment")
    print(f"✓ Have your say: {open_} comment periods open, "
          f"{sum(1 for p in projects for item in p['say'] if item['kind'] == 'meeting')} meetings coming up")


def say_day(item):
    """The day of a comment deadline (a date) or a meeting (a moment)."""
    return item["when"].date() if isinstance(item["when"], datetime) else item["when"]


def say_text(item, today, short=False):
    """One comment period or meeting, as a line says it: "Comments close Sep 30", "IAG Meeting · Mon, Sep 28, 6 PM"."""
    day = say_day(item)
    if item["kind"] == "comment":
        return f"Comments close {when(day, today)}" if short else f"Comment period ends {day:%a, %b} {day.day}"
    moment = item["when"]
    if short:
        return f"Meeting {when(day, today)}"
    at = f", {clock(moment)}" if isinstance(moment, datetime) else ""  # Cambridge's table gives no time.
    return f"{item['what']} · {day:%a, %b} {day.day}{at}"


def add_images(projects, known):
    """Each Boston project's rendering, fetched from its page the first time it's seen and kept from build to build
    after that, so a build fetches only the new ones. Returns them all, to keep for next time."""
    images = {ident: url for ident, url in known.items() if url is not None}
    wanted = [p for p in projects if p["id"].startswith("boston-") and p["link"] and p["id"] not in images]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for project, url in zip(wanted, pool.map(lambda p: boston_image(p["link"]), wanted)):
            if url is not None:
                images[project["id"]] = url
    if wanted:
        print(f"✓ Boston pictures: looked for {len(wanted)}, found {sum(1 for p in wanted if images.get(p['id']))}")
    for project in projects:
        project.setdefault("image", images.get(project["id"], ""))
    return images


def main():
    built_at = datetime.now(timezone.utc)
    previous = shared.previous_build(PROJECTS_URL, USER_AGENT)
    every = float(os.environ.get("HOUSING_EVERY_HOURS") or 0)
    if every and previous.get("built") and \
            built_at - datetime.fromisoformat(previous["built"]) < timedelta(hours=every) - timedelta(minutes=10):
        print(f"Built at {previous['built']}; not due yet, so not rebuilding.")
        return

    # All at once, so a source that's slow to answer (MassBuilds, some days) holds the build up only as long as
    # its own retries take, not once for each of its towns.
    def load(source):
        town, url, read = source
        try:
            return read(shared.fetch(url, USER_AGENT, attempts=2, timeout=45)[1]), None
        except Exception as error:
            return None, error
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        loaded = list(pool.map(load, SOURCES))
    projects, failed, kept = [], [], previous.get("sources", {})
    for (town, _, _), (found, error) in zip(SOURCES, loaded):
        if not error:
            print(f"✓ {town}: {len(found)} projects with homes")
        else:
            print(f"✗ {town}: {error}", file=sys.stderr)
            failed.append(town)
            if town not in kept:
                continue
            found = [dict(project, dated=date.fromisoformat(project["dated"]) if project["dated"] else None)
                     for project in kept[town]]
        projects += [dict(project, source=town) for project in found]

    if not projects:
        sys.exit("No town loaded — not writing the page.")
    raw = {town: [{key: value for key, value in project.items() if key != "source"}
                  for project in projects if project["source"] == town] for town, _, _ in SOURCES}
    images = add_images(projects, previous.get("images", {}))
    add_say(projects, built_at.astimezone(BOSTON).date())
    projects = apply_edits(projects, read_edits(EDITS.read_text()))
    record = track(projects, previous, built_at.date())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    (OUT_DIR / "index.html").write_text(render(projects, built_at, failed))
    for page, (path, _, _) in PAGES.items():
        (OUT_DIR / path).mkdir(exist_ok=True)
        (OUT_DIR / path / "index.html").write_text(render(projects, built_at, failed, page))
    towns = by_town(projects)
    for town in towns:
        (OUT_DIR / slug(town)).mkdir(exist_ok=True)
        (OUT_DIR / slug(town) / "index.html").write_text(render(projects, built_at, failed, "town", town))
    for project in projects:
        (OUT_DIR / "p" / project["id"]).mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "p" / project["id"] / "index.html").write_text(share_page(project, built_at.date()))
    # Every project's panel, for any page to ask for as one is opened.
    (OUT_DIR / "panels.json").write_text(json.dumps(
        {p["id"]: panel_data(p, built_at.date()) for p in projects}, ensure_ascii=False, default=str))
    # To follow without coming back: the deadlines and meetings as a calendar to subscribe to, one for the
    # whole region and one for each town, and what's changed as a feed.
    (OUT_DIR / "say.ics").write_text(calendar(projects, built_at))
    for town in towns:
        (OUT_DIR / slug(town) / "say.ics").write_text(
            calendar([p for p in projects if p["town"] == town], built_at, town))
    (OUT_DIR / "updates.xml").write_text(updates_feed(recently_changed(projects, built_at.date()), built_at))
    (OUT_DIR / "sitemap.xml").write_text(sitemap(projects, towns, built_at))
    (OUT_DIR / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}sitemap.xml\n")
    saved = {"built": built_at.isoformat(), "projects": record, "sources": raw, "images": images}
    (OUT_DIR / "projects.json").write_text(json.dumps(saved, ensure_ascii=False, default=str))
    recent = recently_changed(projects, built_at.date())
    print(f"Wrote dist/{OUT_DIR.name}/index.html, {', '.join(path for path, _, _ in PAGES.values())}, {len(towns)} towns' "
          f"pages, {len(projects)} share pages (p/), panels.json, say.ics, updates.xml, sitemap.xml and projects.json: "
          f"{len(projects)} projects, {len(recent)} changed in the last {RECENT_DAYS} days")


if __name__ == "__main__":
    main()
