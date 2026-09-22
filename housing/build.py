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
    Both name its town, the page having projects from all of them in one list."""
    label, color = STATUSES[project["status"]]
    day = changed[0] if changed else updated(project)
    ident = html.escape(project["id"])
    note = f'<p class="note">{html.escape(project["note"])}</p>' if project.get("note") else ""
    where = project["town"] if changed or large else project["neighborhood"]
    place = " · ".join(html.escape(part) for part in (where, when(day, today)) if part)
    homes = f'{project["units"]:,} home{"s" if project["units"] != 1 else ""}'
    words = " ".join((project["name"], project["neighborhood"], project["town"], project["description"], project.get("note", "")))
    return (
        f'<li class="row" id="{ident}" data-status="{project["status"]}" data-units="{project["units"]}" '
        f'data-lat="{project["lat"]:.5f}" data-lon="{project["lon"]:.5f}" data-words="{html.escape(words.casefold())}">'
        f'{status_icon(project["status"], label)}'
        f'<span class="headline"><a class="title" href="#{ident}">{html.escape(project["name"])}</a>'
        f' <span class="details">{homes} · {html.escape(changed[1]) if changed else label.lower()}</span></span> '
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
    }


CSS = """
  #map { height: 22rem; margin: 0 0 1.25rem; border: 1px solid #222; border-radius: 6px; background: #242426; }
  @media (max-width: 34rem) { #map { height: 16rem; } }
  /* The filter under the map rather than under the header. */
  #map + .filter { margin-top: 0; }
  /* Recently updated, a line over the map, as Pushpin's Just announced: one of the newest, and the way to the rest. */
  .fresh { display: flex; align-items: baseline; gap: .6rem; margin: 0 0 1.25rem; padding: .5rem 0; font-size: .85rem;
           border-top: 1px solid #1c1c1c; border-bottom: 1px solid #1c1c1c; }
  .fresh-tag { flex: none; color: #6e6e6e; font-size: .72rem; font-weight: 400; letter-spacing: .07em; text-transform: uppercase; }
  .spark-mark { width: 12px; height: 12px; margin-right: .45em; vertical-align: -1px; }
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
  /* Recent updates' name for itself, after the site's in the header. */
  .sites .here { color: #fff; font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; }
  .sites a:not([aria-current]) .city { color: inherit; }
  .tagline a { color: #bbb; }
  /* Large projects' bar of four steps, before its town: how far along the project is. */
  .steps { display: inline-flex; flex: none; gap: 2px; margin-right: .6rem; }
  .steps i { width: 9px; height: 4px; border-radius: 1px; background: #2a2a2a; }
  /* The footer, as Pushpin's: set off by a faint line, what the site is, then its pages, towns and sources in
     short lists under small, faint headings, the towns in two columns. */
  footer { margin-top: 3.5rem; padding-top: 2.25rem; border-top: 1px solid rgba(255, 255, 255, .09); }
  .foot-tagline { color: #888; font-size: .95rem; }
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
  /* What the site is, under its name, with the search beside it. */
  .intro { display: flex; justify-content: space-between; align-items: baseline; gap: 1.5rem; margin: -1.25rem 0 1.25rem; }
  .tagline { min-width: 0; margin: 0; overflow: hidden; color: #888; font-size: .9rem; white-space: nowrap; text-overflow: ellipsis; }
  .sites .city { color: #777; }
  /* The search gives up its width before the tagline does. */
  .intro .search { flex: 0 1 10rem; min-width: 6rem; margin-left: 0; }
  @media (max-width: 34rem) {
    .intro { flex-direction: column; align-items: stretch; gap: .9rem; }
    .intro .search { flex: none; width: 100%; font-size: 1rem; }  /* Stacked, its flex size would be its height. */
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
  .maplibregl-popup-anchor-bottom .maplibregl-popup-tip { border-top-color: #333; }
  .maplibregl-popup-anchor-top .maplibregl-popup-tip { border-bottom-color: #333; }
  .maplibregl-ctrl-group { background: #111; border: 1px solid #333; box-shadow: none !important; }
  .maplibregl-ctrl-group button + button { border-top-color: #333; }
  .maplibregl-ctrl-group button .maplibregl-ctrl-icon { filter: invert(.75); }
  .maplibregl-ctrl-attrib, .maplibregl-ctrl-attrib.maplibregl-compact { background: rgba(0, 0, 0, .6); color: #555; font-size: 9px; }
  .maplibregl-ctrl-attrib a { color: #666; }
  .maplibregl-ctrl-attrib-button { filter: invert(.7); background-color: transparent; }
  /* While the map loads, a quiet note in its middle, fading in only if loading takes a moment. */
  .map-loading { position: absolute; inset: 0; z-index: 1; display: grid; place-items: center; color: #666; font-size: .85rem;
                 pointer-events: none; animation: appear .3s ease-out .4s both; }
  /* What the map is leaving out at this zoom, quiet in its corner. */
  .map-hint { margin: 0 0 .45rem .55rem !important; padding: .1rem .45rem; border-radius: 3px; background: rgba(0, 0, 0, .65);
              color: #888; font-size: .72rem; pointer-events: none; }
  .map-hint:empty { display: none; }
  .panel-pill .icon { width: 12px; height: 12px; }
  main > details { border-top: 1px solid #1c1c1c; }
  main > details > summary { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; padding: .8rem 0;
            cursor: pointer; list-style: none; font-weight: 700; }
  main > details > summary::-webkit-details-marker { display: none; }
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
  .row .details { flex: none; margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }
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
<dl class="panel-facts"></dl>
<div class="panel-about"></div>
<div class="panel-foot"><a class="panel-source primary" target="_blank" rel="noopener"></a><a class="panel-site" target="_blank" rel="noopener">Project site ↗</a><button class="panel-map" type="button">Show on map</button><p class="panel-updated"></p></div>
</div>
</dialog>"""

# Before Recently updated, as before Pushpin's Just announced: Tabler's "sparkles" (outline, MIT license).
SPARK_MARK = ('<svg class="spark-mark" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" '
              'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M16 18a2 2 0 0 1 2 2a2 2 0 0 1 2 -2a2 2 0 0 1 -2 -2a2 2 0 0 1 -2 2z"/>'
              '<path d="M16 6a2 2 0 0 1 2 2a2 2 0 0 1 2 -2a2 2 0 0 1 -2 -2a2 2 0 0 1 -2 2z"/>'
              '<path d="M9 18a6 6 0 0 1 6 -6a6 6 0 0 1 -6 -6a6 6 0 0 1 -6 6a6 6 0 0 1 6 6z"/></g></svg>')

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
        banner.href = "#" + pick.id;
        banner.replaceChildren(Object.assign(document.createElement("b"), {textContent: pick.name}), ` · ${pick.what} · ${pick.when}`);
      };
      let at = Math.floor(Math.random() * FRESH.length);
      show(FRESH[at]);
      banner.addEventListener("click", event => {
        const row = document.getElementById(banner.getAttribute("href").slice(1));
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
def icons_head(root):
    return (f'<link rel="icon" href="{root}favicon.svg" type="image/svg+xml">'
            f'<link rel="icon" href="{root}favicon-32.png" sizes="32x32" type="image/png">'
            f'<link rel="apple-touch-icon" href="{root}apple-touch-icon.png">'
            f'<link rel="manifest" href="{root}manifest.webmanifest">')


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
      box.innerHTML = `<a href="#${row.id}" class="to-row">${escape(row.querySelector(".title").textContent)}</a><br>`
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
    // OpenFreeMap's dark style, recolored to read more easily: the land lifted a little from black and the water
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
    map.on("load", () => {
      loading.remove();
      // The credits folded to their ⓘ, which the map's terms ask be on it; MapLibre opens them at first on a wide map.
      const credits = dotMap.container.querySelector(".maplibregl-ctrl-attrib");
      if (credits) { credits.classList.remove("maplibregl-compact-show"); credits.removeAttribute("open"); }
      map.addSource("dots", {type: "geojson", data: waiting || {type: "FeatureCollection", features: []}});
      map.addLayer({
        id: "dots", type: "circle", source: "dots", layout: {"circle-sort-key": ["get", "order"]},
        paint: {"circle-color": ["get", "color"], "circle-radius": ["get", "radius"], "circle-opacity": .85,
                "circle-stroke-color": "#000", "circle-stroke-width": 1},
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
    function openProject(row, push) {
      fill(row);
      if (push) { history.pushState(null, "", "#" + row.id); pushed = true; } else history.replaceState(null, "", "#" + row.id);
      if (!view.open) { view.showModal(); document.body.classList.add("viewing"); }
    }
    const closeProject = () => { if (view.open) view.close(); };
    // Closing takes its address away: Back, where opening it added one; otherwise in place.
    view.addEventListener("close", () => {
      document.body.classList.remove("viewing");
      if (pushed) { pushed = false; history.back(); } else history.replaceState(null, "", location.pathname + location.search);
    });
    const step = where => { const near = beside(where); if (near) openProject(near, false); };
    part("close").addEventListener("click", closeProject);
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
    // A link to a project opens it, with its town's list open behind it; Back and Forward follow the address.
    const fromAddress = () => {
      const row = location.hash.length > 1 && document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (row && row.matches("details")) {  // A town's, from the footer: its list, open.
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
    if town in FROM:
        return f'<p class="from">From {FROM[town]}.</p>'
    if town not in INNER_RING:
        return ""
    latest = max((p["dated"] for p in rows if p["id"].startswith("massbuilds-") and p["dated"]), default=None)
    stale = latest and (today - latest).days > 180
    return (f'<p class="from">From <a href="https://www.massbuilds.com/">MassBuilds</a>'
            f'{f", last updated here {when(latest, today)}" if latest else ""}'
            f'{", so it may be out of date" if stale else ""}.</p>')


def recently_changed(projects, today):
    """The projects with something that happened in the last RECENT_DAYS days, newest first, with what."""
    found = [(project, change(project, today)) for project in projects]
    found = [(project, changed) for project, changed in found if changed]
    return sorted(found, key=lambda pair: (pair[1][0], pair[0]["units"]), reverse=True)


def fresh_banner(recent, today):
    """The home page's Recently updated line: one of the newest few, which its script turns over, and the way to
    the rest. It stays when nothing's changed lately, saying so."""
    fresh = [{"id": project["id"], "name": project["name"], "what": changed[1], "when": when(changed[0], today)}
             for project, changed in recent[:FRESH_SHOWN]]
    if fresh:
        first = fresh[0]
        one = (f'<a class="fresh-one" href="#{html.escape(first["id"])}"><b>{html.escape(first["name"])}</b> · '
               f'{html.escape(first["what"])} · {html.escape(first["when"])}</a>')
    else:
        one = f'<span class="fresh-one fresh-none">Nothing’s changed in the last {RECENT_DAYS} days</span>'
    banner = (f'<p class="fresh"><span class="fresh-tag">{SPARK_MARK}Recently updated</span>{one}'
              f'<a class="fresh-more" href="recent/">See all →</a></p>')
    return banner, json.dumps(fresh, ensure_ascii=False).replace("</", "<\\/")


def slug(town):
    return re.sub(r"[^a-z0-9]+", "-", town.lower()).strip("-")


# The site's pages, besides the home page: each one's path, name, and what it says of itself under the header.
PAGES = {
    "recent": ("recent/", "Recent updates", f"What’s changed in the last {RECENT_DAYS} days."),
    "large": ("large/", "Large projects", f"The biggest projects, {LARGE_HOMES} homes or more, and how far along each is."),
}


def footer(page, towns):
    """The foot of each page, as Pushpin's: what the site is, then its pages, its towns and its sources in short
    lists under faint headings (this page marked), then where the data comes from."""
    root = "../" if page else ""
    here = PAGES[page][0] if page else ""
    marked = ' aria-current="page"'
    groups = [
        ("Browse", "browse", [("All projects", "")] + [(name, path) for path, name, _ in PAGES.values()]),
        ("Towns", "towns", [(town, f"#town-{slug(town)}") for town in towns]),
        ("Sources", "sources", [("Boston Planning", "https://www.bostonplans.org/projects/development-projects"),
                                ("Cambridge log", CAMBRIDGE_PAGE), ("MassBuilds", "https://www.massbuilds.com/")]),
    ]
    def link(label, href):
        outside = href.startswith("http")
        current = marked if not outside and href == here and not href.startswith("#") else ""
        return f'<li><a href="{href if outside else root + href or "./"}"{current}>{html.escape(label)}</a></li>'
    lists = "".join(
        f'<div class="{kind}"><h2>{heading}</h2><ul>{"".join(link(label, href) for label, href in links)}</ul></div>'
        for heading, kind, links in groups
    )
    return (
        f'<footer><p class="foot-tagline">{html.escape(TAGLINE)}</p>'
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


def render(projects, built_at, failed, page=None):
    """The home page, every project by town; or one of PAGES, each a single list across the towns: Recent
    updates, what's changed in the last RECENT_DAYS days, newest first, each saying what happened; Large
    projects, those of LARGE_HOMES homes or more, biggest first, each with how far along it is."""
    today = built_at.date()
    towns = by_town(projects)
    changes = recently_changed(projects, today)
    if page == "recent":
        projects = [project for project, _ in changes]
        rows = "".join(row(project, today, changed) for project, changed in changes)
        heading = f"In the last {RECENT_DAYS} days"
    elif page == "large":
        projects = sorted((p for p in projects if p["units"] >= LARGE_HOMES), key=lambda p: p["units"], reverse=True)
        rows = "".join(row(project, today, large=True) for project in projects)
        heading = f"{LARGE_HOMES} homes or more"
    if page:
        sections = (f'<details open><summary><span class="town">{heading}</span><span class="count"></span></summary>'
                    f'<ul>{rows}</ul></details>')
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
    # The pages across the towns show every status, a finished project among them; the home page what's under way.
    shown = "all" if page else "active"
    choices = [("active", "In progress")] + [(key, label) for key, (label, _) in STATUSES.items()] + [("all", "All")]
    def colored(key):
        return f' style="color:{STATUSES[key][1]}"' if key in STATUSES else ""
    filter_row = (
        '<div class="filter">' + "".join(
            f'<button type="button" data-show="{key}" aria-pressed="{"true" if key == shown else "false"}">'
            f'<span{colored(key)}>{shared.icon(key.replace(" ", "-"))}</span>{label}</button>'
            for key, label in choices
        ) + '</div>'
    )
    # Named by source, not town: MassBuilds down is eleven towns at once.
    unreached = list(dict.fromkeys("MassBuilds" if town in INNER_RING else FROM.get(town, town) for town in failed))
    missing = (f'<p class="empty">Couldn’t reach {" or ".join(unreached)} this time; showing what the last build had.</p>'
               if failed else "")
    data = json.dumps({p["id"]: panel_data(p, today) for p in projects}, ensure_ascii=False).replace("</", "<\\/")
    said = f'{PAGES[page][2]} <a href="../">All projects →</a>' if page else html.escape(TAGLINE)
    intro = f'<div class="intro"><p class="tagline">{said}</p>{shared.SEARCH}</div>'
    banner, fresh = ("", "[]") if page else fresh_banner(changes, today)
    body = (f'{intro}{banner}<div id="map"{" data-fit" if page else ""}></div>{filter_row}{missing}{sections}'
            f'{footer(page, towns)}{PANEL}'
            f'<script type="application/json" id="projects">{data}</script>'
            f'<script>const FRESH = {fresh};</script>')
    script = (SCRIPT % (json.dumps({key: color for key, (_, color) in STATUSES.items()}),
                        json.dumps({key: label for key, (label, _) in STATUSES.items()}))).replace(
        "    /*MAP*/\n", MAP_JS) + FRESH_SCRIPT
    name = PAGES[page][1] if page else None
    root = "../" if page else ""
    return shared.page(
        "news", f"{name} · {NAME}" if page else NAME, body + script, css=CSS, head=icons_head(root) + MAP_HEAD,
        symbols=ICON_SYMBOLS, updated=built_at, links=[(NAME, root or "./", not page)], marked={NAME: MARKED_NAME},
        here=name,
    )


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
    projects = apply_edits(projects, read_edits(EDITS.read_text()))
    record = track(projects, previous, built_at.date())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    (OUT_DIR / "index.html").write_text(render(projects, built_at, failed))
    for page, (path, _, _) in PAGES.items():
        (OUT_DIR / path).mkdir(exist_ok=True)
        (OUT_DIR / path / "index.html").write_text(render(projects, built_at, failed, page))
    saved = {"built": built_at.isoformat(), "projects": record, "sources": raw, "images": images}
    (OUT_DIR / "projects.json").write_text(json.dumps(saved, ensure_ascii=False, default=str))
    recent = recently_changed(projects, built_at.date())
    print(f"Wrote dist/{OUT_DIR.name}/index.html, {', '.join(path for path, _, _ in PAGES.values())} and projects.json: {len(projects)} projects, {len(recent)} changed "
          f"in the last {RECENT_DAYS} days")


if __name__ == "__main__":
    main()
