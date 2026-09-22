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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import shared.site as shared

ROOT = Path(__file__).parent
OUT_DIR = ROOT.parent / "dist" / "housing"
EDITS = ROOT / "edits.txt"
USER_AGENT = "Mozilla/5.0 (compatible; housing-tracker/1.0)"
SITE_URL = "https://housing.grahamhagenah.com/"
NAME = "Housing Tracker Boston"
# The name as the header sets it, with the city in gray after it.
MARKED_NAME = 'Housing Tracker <span class="city">Boston</span>'
TAGLINE = "New homes around Boston, from proposal to move-in."
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
FRESH_SHOWN = 8

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
        for key in ("note", "link", "description", "image"):
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
        "updated": when(updated(project), today),
        "say": [[say_text(item, today), item["link"], item["kind"]] for item in project.get("say", [])],
    }


CSS = """
  #map { height: 22rem; margin: 0 0 1.25rem; border: 1px solid #222; border-radius: 6px; background: #242426; }
  @media (max-width: 34rem) { #map { height: 16rem; } }
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
  .fresh { display: flex; align-items: baseline; gap: .6rem; margin: 0 0 1.25rem; padding: .5rem 0; font-size: .85rem;
           border-top: 1px solid #1c1c1c; border-bottom: 1px solid #1c1c1c; }
  .fresh-tag { flex: none; color: #6e6e6e; font-size: .72rem; font-weight: 400; letter-spacing: .07em; text-transform: uppercase; }
  .spark-mark { width: 12px; height: 12px; margin-right: .45em; vertical-align: -1px; }
  /* One line, cut short with "…" if it must: as it turns over, a longer one never pushes the page down. */
  .fresh-one { min-width: 0; overflow: hidden; color: #999; text-overflow: ellipsis; white-space: nowrap; }
  .fresh-one b { color: #fff; font-weight: 500; }
  .fresh-none { color: #666; }
  .fresh-more { flex: none; margin-left: auto; color: #888; }
  .fresh-more:hover, .fresh-one:hover { color: #fff; text-decoration: none; }
  .fresh-one:hover b { text-decoration: underline; }
  @media (max-width: 34rem) {
    .fresh { display: grid; grid-template-columns: 1fr auto; gap: .1rem .6rem; }
    .fresh-tag { grid-column: 1 / -1; }
    .fresh-more { margin-left: 0; }
  }
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
  .tagline a { color: #bbb; }
  /* Large projects' bar of four steps, before its town: how far along the project is. */
  .steps { display: inline-flex; flex: none; gap: 2px; margin-right: .6rem; }
  .steps i { width: 9px; height: 4px; border-radius: 1px; background: #2a2a2a; }
  /* The footer, as Pushpin's: set off by a faint line, what the site is, then its pages, towns and sources in
     short lists under small, faint headings, the towns in two columns. */
  footer { margin-top: 3.5rem; padding-top: 2.25rem; border-top: 1px solid rgba(255, 255, 255, .09); }
  .foot-about { max-width: 38rem; margin: 0; color: #999; font-size: .9rem; line-height: 1.6; }
  .site-links { display: grid; grid-template-columns: minmax(0, 10rem) minmax(0, 18rem) minmax(0, 10rem); gap: 1.75rem 2.5rem;
                margin: 1.25rem 0 2rem; }
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
  /* In a project's panel: its open comment period and meetings, each a link to comment or join. */
  .panel-say { margin: 0 0 1.1rem; padding: .7rem .85rem; border: 1px solid #2a2a2a; border-radius: 6px; }
  .panel-say[hidden] { display: none; }
  .panel-say h3 { margin: 0 0 .4rem; color: #888; font-size: .72rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
  .panel-say ul { display: grid; gap: .3rem; }
  .panel-say a { color: #eee; font-size: .88rem; }
  /* What the site is, under its name. */
  .intro { margin: -1.25rem 0 1.25rem; }
  /* On as many lines as it takes, rather than cut short: the home page's fits on one. */
  .tagline { margin: 0; color: #888; font-size: .9rem; }
  .sites .city { color: #777; }
  @media (max-width: 34rem) {
    .tagline { font-size: .8rem; }
  }
  /* The map's controls, notes and credits, quiet and dark like the page. */
  .maplibregl-map { font: inherit; }
  .maplibregl-map .map-hint { position: absolute; left: 0; bottom: 0; z-index: 2; }
  .maplibregl-popup-content { padding: .7rem .9rem; border: 1px solid #333; border-radius: 6px; background: #111; color: #ddd;
                              font-size: .85rem; line-height: 1.4; box-shadow: none; }
  .maplibregl-popup-content a { color: #fff; }
  .maplibregl-popup-content .muted { color: #888; }
  .maplibregl-popup-close-button { color: #666; font-size: 1rem; }
  /* The arrow to its dot, whichever side the note opens on (MapLibre picks the side with room), in the note's own
     color, so it reads as part of it. */
  .maplibregl-popup-anchor-bottom .maplibregl-popup-tip, .maplibregl-popup-anchor-bottom-left .maplibregl-popup-tip,
  .maplibregl-popup-anchor-bottom-right .maplibregl-popup-tip { border-top-color: #111; }
  .maplibregl-popup-anchor-top .maplibregl-popup-tip, .maplibregl-popup-anchor-top-left .maplibregl-popup-tip,
  .maplibregl-popup-anchor-top-right .maplibregl-popup-tip { border-bottom-color: #111; }
  .maplibregl-popup-anchor-left .maplibregl-popup-tip { border-right-color: #111; }
  .maplibregl-popup-anchor-right .maplibregl-popup-tip { border-left-color: #111; }
  .maplibregl-ctrl-group { background: #111; border: 1px solid #333; box-shadow: none !important; }
  .maplibregl-ctrl-group button + button { border-top-color: #333; }
  .maplibregl-ctrl-group button .maplibregl-ctrl-icon { filter: invert(.75); }
  /* The credits: folded, only a small, faint ⓘ with nothing behind it; opened, a dark band. Its icon is drawn here
     in gray rather than MapLibre's black inverted, which also turned the blue ring it's given when focused orange;
     from the keyboard it gets a quiet ring of its own instead. */
  .maplibregl-ctrl-attrib, .maplibregl-ctrl-attrib.maplibregl-compact { background: none; color: #555; font-size: 9px; }
  /* Opened, its line of credits centered in the band, level with the ⓘ at its end. */
  .maplibregl-ctrl-attrib.maplibregl-compact-show { display: flex; align-items: center; box-sizing: border-box; min-height: 24px;
                                                    padding-top: 0; padding-bottom: 0; background: rgba(0, 0, 0, .6); }
  .maplibregl-ctrl-attrib a { color: #666; }
  .maplibregl-ctrl-attrib-button, .maplibregl-ctrl-attrib.maplibregl-compact-show .maplibregl-ctrl-attrib-button {
    background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3E%3Cpath fill='%23888' fill-rule='evenodd' d='M4 10a6 6 0 1 0 12 0 6 6 0 1 0-12 0m5-3a1 1 0 1 0 2 0 1 1 0 1 0-2 0m0 3a1 1 0 1 1 2 0v3a1 1 0 1 1-2 0'/%3E%3C/svg%3E") center / 16px no-repeat;
    opacity: .45; transition: opacity .15s; }
  .maplibregl-ctrl-attrib-button:hover, .maplibregl-ctrl-attrib.maplibregl-compact-show .maplibregl-ctrl-attrib-button { opacity: .8; }
  .maplibregl-ctrl-attrib-button:focus { box-shadow: none; }
  .maplibregl-ctrl-attrib-button:focus-visible { box-shadow: 0 0 0 1px #555; opacity: .8; }
  /* While the map loads, a quiet note in its middle, fading in only if loading takes a moment. */
  .map-loading { position: absolute; inset: 0; z-index: 1; display: grid; place-items: center; color: #666; font-size: .85rem;
                 pointer-events: none; animation: appear .3s ease-out .4s both; }
  /* What the map is leaving out at this zoom, quiet in its corner. */
  .map-hint { margin: 0 0 .45rem .55rem !important; padding: .1rem .45rem; border-radius: 3px; background: rgba(0, 0, 0, .65);
              color: #888; font-size: .72rem; pointer-events: none; }
  .map-hint:empty { display: none; }
  .panel-pill .icon { width: 12px; height: 12px; }
  main > details { border-top: 1px solid #1c1c1c; }
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
  .from { margin: -.3rem 0 .6rem; color: #666; font-size: .8rem; }
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
  .panel-foot { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: auto; padding-top: 1.2rem; }
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
<div class="panel-image" hidden><img alt="" decoding="async" referrerpolicy="no-referrer"></div>
<p class="panel-where"></p>
<h2 class="panel-title" id="panel-title"></h2>
<div class="panel-pills"></div>
<p class="panel-note"></p>
<div class="panel-say"><h3>Have your say</h3><ul></ul></div>
<dl class="panel-facts"></dl>
<div class="panel-about"></div>
<div class="panel-foot"><a class="panel-source primary" target="_blank" rel="noopener"></a><a class="panel-site" target="_blank" rel="noopener">Project site ↗</a><button class="panel-copy" type="button">Copy link</button><button class="panel-map" type="button">Show on map</button><p class="panel-updated"></p></div>
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

# Recently updated, over the home page's map: the newest few, turning over one at a time so a visit shows
# several, as Pushpin's Just announced does; each opens its project's panel here. It starts anywhere among them,
# holds while it's under the pointer or has the keyboard or the page is in the background, and stays put for
# anyone who's asked for less motion.
FRESH_SCRIPT = """
<script>
  {
    const banner = document.querySelector(".fresh-one[href]");
    if (banner) {
      const show = pick => {
        banner.href = "?project=" + encodeURIComponent(pick.id);
        banner.replaceChildren(Object.assign(document.createElement("b"), {textContent: pick.name}), ` · ${pick.what} · ${pick.when}`);
      };
      let at = Math.floor(Math.random() * FRESH.length);
      show(FRESH[at]);
      banner.addEventListener("click", event => {
        const row = document.getElementById(new URLSearchParams(banner.getAttribute("href")).get("project"));
        if (!row || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        row.click();
      });
      if (FRESH.length > 1 && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
        let held = false;
        for (const [name, on] of [["pointerenter", true], ["pointerleave", false], ["focusin", true], ["focusout", false]]) {
          banner.closest(".fresh").addEventListener(name, () => { held = on; });
        }
        setInterval(() => {
          if (held || document.hidden) return;
          banner.animate([{opacity: 1}, {opacity: 0, offset: .45}, {opacity: 1}], {duration: 1400, easing: "ease-in-out"});
          setTimeout(() => show(FRESH[at = (at + 1) % FRESH.length]), 620);
        }, 7000);
      }
    }
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
    const map = new maplibregl.Map({
      container: "map", style: "https://tiles.openfreemap.org/styles/dark", center: [-71.08, 42.365],
      zoom: (innerWidth < 544 ? 11 : 12) - 1,  // A phone's narrower map, one step further out.
      scrollZoom: false, dragRotate: false, pitchWithRotate: false, touchPitch: false, attributionControl: {compact: true},
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-left");
    const escape = text => text.replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
    const radius = row => Math.max(2.2, Math.min(7, Math.sqrt(Math.max(0, +row.dataset.units) || 0) / 3.3));
    // A dot's note: its name, which opens its panel, and its homes and status.
    const note = row => {
      const status = row.dataset.status, box = document.createElement("div");
      box.innerHTML = `<a href="?project=${row.id}" class="to-row">${escape(row.querySelector(".title").textContent)}</a><br>`
        + `<span class="muted">${(+row.dataset.units).toLocaleString()} homes · ${labels[status].toLowerCase()}</span>`;
      box.querySelector("a").addEventListener("click", event => { event.preventDefault(); openProject(row, true); });
      return new maplibregl.Popup({offset: radius(row) + 4, maxWidth: "280px"})
        .setLngLat([+row.dataset.lon, +row.dataset.lat]).setDOMContent(box).addTo(map);
    };
    let waiting = null;  // What to draw once the map's style has loaded.
    const hintBox = Object.assign(document.createElement("div"), {className: "map-hint"});
    const dotMap = {
      container: map.getContainer(),
      zoom: () => Math.round(map.getZoom()) + 1,
      onZoom: then => map.on("zoomend", then),
      // The smaller over the bigger: a higher sort key is drawn on top.
      draw: shown => {
        const data = {type: "FeatureCollection", features: shown.map(row => ({
          type: "Feature", geometry: {type: "Point", coordinates: [+row.dataset.lon, +row.dataset.lat]},
          properties: {row: row.id, color: colors[row.dataset.status], radius: radius(row), order: -row.dataset.units},
        }))};
        if (map.getSource("dots")) map.getSource("dots").setData(data); else waiting = data;
      },
      hint: text => { hintBox.textContent = text; },
      fit: shown => {
        const bounds = new maplibregl.LngLatBounds();
        for (const row of shown) bounds.extend([+row.dataset.lon, +row.dataset.lat]);
        map.fitBounds(bounds, {padding: 30, maxZoom: 13, animate: false});
      },
      goTo: row => {
        map.once("moveend", () => note(row));
        map.flyTo({center: [+row.dataset.lon, +row.dataset.lat], zoom: 15});
      },
    };
    dotMap.container.append(hintBox);
    // Until the map has loaded (OpenFreeMap's servers are slow now and then), a note in its middle.
    const loading = Object.assign(document.createElement("div"), {className: "map-loading", textContent: "Loading map…"});
    dotMap.container.append(loading);
""" + STYLE_JS + """    map.on("load", () => {
      loading.remove();
      // The credits folded to their ⓘ, which the map's terms ask be on it; MapLibre opens them at first on a wide map.
      const credits = dotMap.container.querySelector(".maplibregl-ctrl-attrib");
      if (credits) { credits.classList.remove("maplibregl-compact-show"); credits.removeAttribute("open"); }
      map.addSource("dots", {type: "geojson", data: waiting || {type: "FeatureCollection", features: []}});
      map.addLayer({
        id: "dots", type: "circle", source: "dots", layout: {"circle-sort-key": ["get", "order"]},
        // A ring in the status's color around a faint fill of the same.
        paint: {"circle-color": ["get", "color"], "circle-radius": ["get", "radius"], "circle-opacity": .22,
                "circle-stroke-color": ["get", "color"], "circle-stroke-width": 1.5},
      });
      map.on("click", "dots", event => note(document.getElementById(event.features[0].properties.row)));
      map.on("mouseenter", "dots", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "dots", () => { map.getCanvas().style.cursor = ""; });
    });
"""

MAP_HEAD = ('<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.min.css">'
               '<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.js"></script>')


SCRIPT = """
<script>
  {
    const colors = %s, labels = %s;
    const rows = [...document.querySelectorAll(".row")];
    const buttons = [...document.querySelectorAll(".filter button")];
    const search = document.querySelector(".search");
    /*MAP*/
    const bySize = [...rows].sort((a, b) => b.dataset.units - a.dataset.units);
    // Zoomed out, only the bigger projects, so the map isn't a carpet of dots; the smaller come in as it's zoomed
    // in, all of them at street level: at each zoom, the fewest homes a project needs to be drawn. A search, and
    // the pages of a few projects (Recent updates, Large projects), show every one that fits.
    const LEAST = [[16, 0], [15, 20], [14, 50], [13, 100], [12, 150]], FARTHEST = 300;
    const fitting = dotMap.container.hasAttribute("data-fit");
    let searching = false, pinned = null;  // pinned: a project Show on map went to, drawn whatever its size.
    const least = () => {
      if (fitting || searching) return 0;
      const zoom = dotMap.zoom();
      return (LEAST.find(([at]) => zoom >= at) || [0, FARTHEST])[1];
    };
    const draw = () => {
      const fewest = least();
      // The biggest first, so the smaller are drawn over them and a small project beside a big one can be clicked.
      dotMap.draw(bySize.filter(row => !row.hidden && (+row.dataset.units >= fewest || row === pinned)));
      dotMap.hint(fewest ? `Showing ${fewest}+ homes · zoom in for more` : "");
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
    const data = JSON.parse(document.getElementById("projects").textContent);
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
      const p = data[row.id];
      rows.forEach(other => other.classList.toggle("lit", other === row));
      part("where").textContent = [p.town, p.neighborhood].filter(Boolean).join(" · ");
      part("title").textContent = p.name;
      part("pills").replaceChildren(make("span", {className: "panel-pill"}, row.querySelector(".icon").cloneNode(true), labels[p.status]),
        make("span", {className: "panel-pill"}, p.units.toLocaleString() + (p.units === 1 ? " home" : " homes")));
      part("note").textContent = p.note;
      // Have your say: each open comment period and upcoming meeting, linking to where to comment or to join.
      part("say").hidden = !p.say.length;
      part("say").querySelector("ul").replaceChildren(...p.say.map(([text, link, kind]) => make("li", {},
        make("a", {href: link, target: "_blank", rel: "noopener",
                   textContent: text + (kind === "comment" ? " · Comment ↗" : " ↗")}))));
      part("facts").replaceChildren(...p.facts.flatMap(([label, value]) => [make("dt", {textContent: label}), make("dd", {textContent: value})]));
      part("about").replaceChildren(...p.description.split(/\\n+/).map(text => text.trim()).filter(Boolean)
        .map(text => make("p", {textContent: text})));
      const image = part("image"), img = image.querySelector("img");
      image.classList.remove("loaded");
      image.hidden = !p.image;
      view.classList.toggle("shows-picture", !!p.image);
      if (p.image) img.src = p.image; else img.removeAttribute("src");
      // The same picture again, already loaded, fires no load event of its own.
      if (p.image && img.complete && img.naturalWidth) image.classList.add("loaded");
      part("source").hidden = !p.link;
      part("source").href = p.link;
      part("source").textContent = (p.origin ? "View on " + p.origin : "View source") + " ↗";
      part("copy").textContent = "Copy link";
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
    function openProject(row, push) {
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
      try { await navigator.clipboard.writeText(location.href); part("copy").textContent = "Copied"; }
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
    // Clicking anywhere on a row opens it; with a modifier key, its address opens in a new tab as usual.
    for (const row of rows) row.addEventListener("click", event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      openProject(row, true);
    });
    // A link to a project (?project=<id>, or #<id> from before) opens its panel, with its town's list open behind
    // it; Back and Forward follow the address.
    const fromAddress = () => {
      const id = new URLSearchParams(location.search).get("project") || decodeURIComponent(location.hash.slice(1));
      const row = id && document.getElementById(id);
      if (row && row.matches("details")) {  // A town's, from an old link: its list, open.
        if (view.open) { pushed = false; view.close(); }
        row.open = true;
        row.scrollIntoView({block: "start"});
      } else if (row && row.classList.contains("row")) {
        row.closest("details").open = true;
        if (view.open) fill(row); else openProject(row, false);
      } else if (view.open) { pushed = false; view.close(); }
    };
    addEventListener("popstate", () => { pushed = false; fromAddress(); });
    fromAddress();
  }
</script>"""


def source_note(town, rows, today):
    """Where a town's list comes from, and for a MassBuilds town, when anything in it was last updated, since
    some towns' entries go a year or more without."""
    page = f' <a class="to-town" href="/{slug(town)}/">{html.escape(town)}’s page →</a>'
    if town in FROM:
        return f'<p class="from">From {FROM[town]}.{page}</p>'
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
    """The home page's Recently updated line: one of the newest few, which its script turns over, and the way to
    the rest. It stays when nothing's changed lately, saying so."""
    fresh = [{"id": project["id"], "name": project["name"], "what": changed[1], "when": when(changed[0], today)}
             for project, changed in recent[:FRESH_SHOWN]]
    if fresh:
        first = fresh[0]
        one = (f'<a class="fresh-one" href="?project={html.escape(first["id"])}"><b>{html.escape(first["name"])}</b> · '
               f'{html.escape(first["what"])} · {html.escape(first["when"])}</a>')
    else:
        one = f'<span class="fresh-one fresh-none">Nothing’s changed in the last {RECENT_DAYS} days</span>'
    # The way to the rest (Recent updates), except on Recent updates, which is the rest.
    see_all = f'<a class="fresh-more" href="{more}">See all →</a>' if more else ""
    banner = f'<p class="fresh"><span class="fresh-tag">{SPARK_MARK}Recently updated</span>{one}{see_all}</p>'
    return banner, json.dumps(fresh, ensure_ascii=False).replace("</", "<\\/")


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
    soonest = min(((say_day(p["say"][0]), p) for p in projects if p.get("say")), key=lambda pair: pair[0], default=None)
    if not soonest and not none:
        return ""
    see_all = f'<a class="fresh-more" href="{more}">See all →</a>' if more else ""
    if soonest:
        project = soonest[1]
        one = (f'<a class="fresh-one" href="?project={html.escape(project["id"])}"><b>{html.escape(project["name"])}</b> · '
               f'{html.escape(say_text(project["say"][0], today, short=True))}</a>')
    else:
        one = f'<span class="fresh-one fresh-none">{html.escape(none)}</span>'
    return f'<p class="fresh say"><span class="fresh-tag">{SAY_MARK}Have your say</span>{one}{see_all}</p>'


def about(projects, towns):
    """What the site is, in full, for the foot of each page: what it follows, how much, from where, and how it
    keeps up, with the numbers as of this build."""
    active = [p for p in projects if p["status"] != "complete"]
    return (
        f"{NAME} follows new housing in Boston and the {len(towns) - 1} towns around it, from a project’s first "
        f"filing until people move in: {len(projects):,} projects and {sum(p['units'] for p in projects):,} homes, "
        f"{len(active):,} of those projects ({sum(p['units'] for p in active):,} homes) still in progress. It gathers "
        "them from Boston’s Planning Department, Cambridge’s development log and MAPC’s MassBuilds every few hours "
        "and notes each time a project moves along, so what’s just been filed, approved or broken ground rises to "
        "the top of its town’s list."
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
    ]
    def link(label, href):
        outside = href.startswith("http")
        current = marked if not outside and href == here else ""
        return f'<li><a href="{href if outside else "/" + href}"{current}>{html.escape(label)}</a></li>'
    lists = "".join(
        f'<div class="{kind}"><h2>{heading}</h2><ul>{"".join(link(label, href) for label, href in links)}</ul></div>'
        for heading, kind, links in groups
    )
    return (
        f'<footer><p class="foot-about">{html.escape(said)}</p>'
        f'<nav class="site-links" aria-label="{html.escape(NAME)}">{lists}</nav>'
        '<p>From <a href="https://data.boston.gov/dataset/article80-development-projects">Boston’s Article 80 '
        'projects</a> (anything over about 20,000 square feet or 15 homes), <a href="' + CAMBRIDGE_PAGE +
        '">Cambridge’s development log</a> (50,000 square feet or 10 homes) and, for the towns around them, MAPC’s '
        '<a href="https://www.massbuilds.com/">MassBuilds</a>, with notes of our own. A project’s date is the latest '
        'of its filing, its approval, its last update and the day it was seen to move on.</p></footer>'
    )


def by_town(projects):
    """The towns with projects, Boston first as the city the rest are around, then the others by name."""
    return sorted({project["town"] for project in projects}, key=lambda town: (town != "Boston", town))


def head(path, title, description):
    """What a page tells search engines and link previews: what it's about, and its one address."""
    url = SITE_URL + path
    return (f'<meta name="description" content="{html.escape(description)}">'
            f'<link rel="canonical" href="{url}">'
            f'<meta property="og:type" content="website"><meta property="og:site_name" content="{html.escape(NAME)}">'
            f'<meta property="og:title" content="{html.escape(title)}">'
            f'<meta property="og:description" content="{html.escape(description)}"><meta property="og:url" content="{url}">'
            + icons_head() + MAP_HEAD)


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
    data = json.dumps({p["id"]: panel_data(p, today) for p in projects}, ensure_ascii=False).replace("</", "<\\/")
    active = [p for p in projects if p["status"] != "complete"]
    town_said = (f"New housing in {town}: {len(active):,} projects in progress, "
                 f"{sum(p['units'] for p in active):,} homes, from proposal to move-in.")
    said = (f'{html.escape(town_said)} <a href="/">All towns →</a>' if page == "town"
            else f'{PAGES[page][2]} <a href="/">All projects →</a>' if page else html.escape(TAGLINE))
    intro = f'<div class="intro"><p class="tagline">{said}</p></div>'
    # Over every page's map: on Large projects, only its own projects' changes, so each opens here.
    if page == "large":
        banner, fresh = fresh_banner([(p, c) for p, c in changes if p["units"] >= LARGE_HOMES], today, more="/recent/")
    elif page == "town":
        banner, fresh = fresh_banner(changes, today, more="/recent/")
    elif page == "say":
        banner, fresh = fresh_banner([(p, c) for p, c in changes if p.get("say")], today, more="/recent/")
    else:
        banner, fresh = fresh_banner(changes, today, more="" if page == "recent" else "/recent/")
    # And under it, the soonest chance to have a say, but on Have your say itself, which is all of them.
    if page != "say":
        # A town that publishes no comment periods or meetings says so, rather than leaving the line out.
        nothing = (f"{town} doesn’t publish comment periods or meetings" if page == "town"
                   and town not in ("Boston", "Cambridge") else None)
        banner += say_line(projects, today, more="/have-your-say/", none=nothing)
    body = (f'{intro}{banner}<div id="map"{" data-fit" if page else ""}></div>{filter_row}{missing}{sections}'
            f'{footer(path, towns, about(everything, towns))}{PANEL}'
            f'<script type="application/json" id="projects">{data}</script>'
            f'<script>const FRESH = {fresh};</script>')
    script = (SCRIPT % (json.dumps({key: color for key, (_, color) in STATUSES.items()}),
                        json.dumps({key: label for key, (label, _) in STATUSES.items()}))).replace(
        "    /*MAP*/\n", MAP_JS) + FRESH_SCRIPT
    name = town if page == "town" else PAGES[page][1] if page else None
    title = f"New housing in {town} · {NAME}" if page == "town" else f"{name} · {NAME}" if page else NAME
    description = (town_said if page == "town" else PAGES[page][2] if page else
                   f"{TAGLINE} {len(active):,} projects in progress across Boston and the towns around it, on a map "
                   "and in a list for each town, with their status, size and details.")
    return shared.page(
        "news", title, body + script, css=CSS, head=head(path, title, description), symbols=ICON_SYMBOLS,
        updated=built_at, links=[(NAME, "/", not page)], marked={NAME: MARKED_NAME}, here=name, indexable=True,
    )


def sitemap(projects, towns, built_at):
    """Every page, for search engines. A project's panel has no page of its own to list."""
    paths = [("", built_at.date())] + [(path, built_at.date()) for path, _, _ in PAGES.values()]
    paths += [(f"{slug(town)}/", built_at.date()) for town in towns]
    entries = "".join(f"<url><loc>{SITE_URL}{path}</loc>{f'<lastmod>{day.isoformat()}</lastmod>' if day else ''}</url>"
                      for path, day in paths)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>\n'


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
    by_case = {p["case"]: p for p in projects if p.get("case")}
    by_path = {project_path(p["link"]): p for p in projects if p["id"].startswith("boston-")}
    for project in projects:
        project["say"] = []
        ends = closes.get(project["id"])
        if ends and ends >= today:
            project["say"].append({"when": ends, "what": "Comment period ends", "link": project["link"], "kind": "comment"})
    for meeting in calendar:
        for path in meeting["paths"]:
            project = by_path.get(path)
            if project:
                project["say"].append({"when": meeting["starts"], "link": meeting["link"], "kind": "meeting",
                                       "what": meeting_kind(meeting["title"], project["name"])})
    for meeting in board:
        for case in meeting["cases"]:
            project = by_case.get(case)
            if project:
                project["say"].append({"when": meeting["when"], "what": meeting["what"], "kind": "meeting",
                                       "link": CAMBRIDGE_BOARD})
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
    (OUT_DIR / "sitemap.xml").write_text(sitemap(projects, towns, built_at))
    (OUT_DIR / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}sitemap.xml\n")
    saved = {"built": built_at.isoformat(), "projects": record, "sources": raw, "images": images}
    (OUT_DIR / "projects.json").write_text(json.dumps(saved, ensure_ascii=False, default=str))
    recent = recently_changed(projects, built_at.date())
    print(f"Wrote dist/{OUT_DIR.name}/index.html, {', '.join(path for path, _, _ in PAGES.values())}, {len(towns)} towns' "
          f"pages, sitemap.xml and projects.json: {len(projects)} projects, {len(recent)} changed "
          f"in the last {RECENT_DAYS} days")


if __name__ == "__main__":
    main()
