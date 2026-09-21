"""The housing tracker: new housing being proposed, approved and built around Boston, from the cities' own lists
of development projects, with our own changes and additions on top (edits.txt). Each build compares the cities'
statuses with the last build's, so a project moving on (approved, under construction) comes to the top of its
list on the day it's seen to.

Run from the repo's top folder: python3 -m housing.build, which writes dist/housing."""

import html
import json
import os
import re
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

# Finished projects are kept for a few years, as what's recently been built; before that they're history.
COMPLETE_SINCE = 2020

# Where each project is along the way, in order, with the dot it's shown by on the map.
STATUSES = {
    "proposed": ("Proposed", "#8a8a8a"),
    "approved": ("Approved", "#e0a93b"),
    "under construction": ("Under construction", "#4c9be8"),
    "complete": ("Complete", "#55b86a"),
    "stalled": ("Stalled", "#d65a50"),
}

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
        if not units or not status:
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
            "origin": "Boston Planning Department",
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
        if not units or not status or not row.get("latitude"):
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
            "origin": "Cambridge development log",
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
        if not units or not status or row.get("latitude") is None:
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
    """When each project was last seen to change status, from the previous build's record: today, for one
    whose status has changed or that's new since then. The first build has nothing to compare with, so it
    dates nothing. Returns the record to keep for next time."""
    before = previous.get("projects")
    record = {}
    for project in projects:
        kept = (before or {}).get(project["id"])
        if before is None:
            since = None
        elif kept and kept["status"] == project["status"]:
            since = kept["since"]
        else:
            since = today.isoformat()
        project["since"] = date.fromisoformat(since) if since else None
        record[project["id"]] = {"status": project["status"], "since": since}
    return record


def updated(project):
    """The latest thing known to have happened to it."""
    return max((d for d in (project["dated"], project.get("since"), project.get("edited")) if d), default=None)


def when(day, today):
    """A date as a row shows it: "Aug 14" this year, "Aug 2024" before."""
    if not day:
        return ""
    return f"{day:%b} {day.day}" if day.year == today.year else f"{day:%b %Y}"


def row(project, today):
    label, color = STATUSES[project["status"]]
    day = updated(project)
    ident = html.escape(project["id"])
    note = f'<p class="note">{html.escape(project["note"])}</p>' if project.get("note") else ""
    place = " · ".join(html.escape(part) for part in (project["neighborhood"], when(day, today)) if part)
    homes = f'{project["units"]:,} home{"s" if project["units"] != 1 else ""}'
    words = " ".join((project["name"], project["neighborhood"], project["town"], project["description"], project.get("note", "")))
    return (
        f'<li class="row" id="{ident}" data-status="{project["status"]}" data-units="{project["units"]}" '
        f'data-lat="{project["lat"]:.5f}" data-lon="{project["lon"]:.5f}" data-words="{html.escape(words.casefold())}">'
        f'<span class="dot" style="background:{color}" role="img" aria-label="{label}"></span>'
        f'<span class="headline"><a class="title" href="#{ident}">{html.escape(project["name"])}</a>'
        f' <span class="details">{homes} · {label.lower()}</span></span> '
        f'<span class="source"><span>{place}</span></span>{note}</li>'
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
  #map { height: 22rem; margin: 0 0 1.5rem; border: 1px solid #222; border-radius: 6px; background: #0a0a0a; }
  @media (max-width: 34rem) { #map { height: 16rem; } }
  .leaflet-container { font: inherit; }
  .leaflet-popup-content-wrapper, .leaflet-popup-tip { background: #111; color: #ddd; border: 1px solid #333; box-shadow: none; }
  .leaflet-popup-content { margin: .7rem .9rem; font-size: .85rem; line-height: 1.4; }
  .leaflet-popup-content a { color: #fff; }
  .leaflet-popup-content .muted { color: #888; }
  .leaflet-container a.leaflet-popup-close-button { color: #666; }
  .leaflet-control-attribution { background: rgba(0, 0, 0, .6) !important; color: #555; font-size: 10px; }
  .leaflet-control-attribution a { color: #777; }
  .leaflet-bar a { background: #111; color: #999; border-color: #333; }
  .leaflet-bar a:hover { background: #222; color: #fff; }
  .legend { display: flex; flex-wrap: wrap; gap: .3rem 1rem; margin: -1rem 0 1.75rem; color: #666; font-size: .8rem; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; align-self: center; }
  .legend .dot { margin-right: .4em; }
  details { border-top: 1px solid #1c1c1c; }
  details:last-of-type { border-bottom: 1px solid #1c1c1c; }
  summary { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; padding: .8rem 0;
            cursor: pointer; list-style: none; font-weight: 700; }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: "›"; display: inline-block; width: 1em; color: #666; transition: transform .15s; }
  details[open] > summary::before { transform: rotate(90deg); }
  summary .town { flex: 1; }
  summary .count { color: #666; font-size: .8rem; font-weight: normal; }
  details > ul { padding-bottom: 1rem; }
  .from { margin: -.3rem 0 .6rem; color: #666; font-size: .8rem; }
  .from a { color: #999; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  .from.stale { color: #b08a4a; }
  .row .details { flex: none; margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }
  .row .note { grid-column: 2 / -1; margin: -.2rem 0 0; color: #999; font-size: .8em; }
  .row { cursor: pointer; }
  .row:hover .title { text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  .row.lit .title { color: #ffd479; }
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
  .panel-image a { position: absolute; right: .6rem; bottom: .5rem; padding: .1rem .5rem; border-radius: 999px;
                   background: rgba(0, 0, 0, .6); color: #ccc; font-size: .72rem; }
  .panel-where { margin: 0 2.5rem .4rem 0; color: #888; font-size: .72rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
  .panel-title { margin: 0 2rem .3rem 0; color: #fff; font-size: 1.35rem; font-weight: 700; letter-spacing: -.01em; line-height: 1.25; }
  .panel-pills { display: flex; flex-wrap: wrap; gap: .4rem; margin: .45rem 0 .9rem; }
  .panel-pill { display: inline-flex; align-items: center; gap: .4rem; padding: .15rem .6rem; border-radius: 999px;
                background: #1c1c1c; color: #eee; font-size: .8rem; font-weight: 600; }
  .panel-note { margin: 0 0 1rem; padding: .6rem .8rem; border-left: 2px solid #ffd479; background: #141414; color: #ddd; font-size: .9rem; }
  .panel-note:empty { display: none; }
  .panel-facts { display: grid; grid-template-columns: auto 1fr; gap: 0 1rem; margin: 0 0 1.1rem; font-size: .85rem; }
  .panel-facts dt, .panel-facts dd { margin: 0; padding: .45rem 0; border-top: 1px solid #1c1c1c; }
  .panel-facts dt { color: #777; }
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
    .row { padding-left: calc(8px + .8em); }
    .row > .dot { position: absolute; left: 0; top: calc(.4rem + .72em - 4px); }
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
<div class="panel-image" hidden><img alt="" decoding="async" referrerpolicy="no-referrer"><a target="_blank" rel="noopener">Full size ↗</a></div>
<p class="panel-where"></p>
<h2 class="panel-title" id="panel-title"></h2>
<div class="panel-pills"></div>
<p class="panel-note"></p>
<dl class="panel-facts"></dl>
<div class="panel-about"></div>
<div class="panel-foot"><a class="panel-source primary" target="_blank" rel="noopener"></a><a class="panel-site" target="_blank" rel="noopener">Project site ↗</a><button class="panel-map" type="button">Show on map</button><p class="panel-updated"></p></div>
</div>
</dialog>"""

LEAFLET = ('<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">'
           '<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>')

# The filter, search and map work from the rows themselves: a dot on the map for each row showing, and each
# town's count of what's showing. A dot opens a note naming its project, which leads to its row.
SCRIPT = """
<script>
  {
    const colors = %s, labels = %s;
    const rows = [...document.querySelectorAll(".row")];
    const buttons = [...document.querySelectorAll(".filter button")];
    const search = document.querySelector(".search");
    const map = L.map("map", {preferCanvas: true, scrollWheelZoom: false}).setView([42.345, -71.08], 12);
    // Esri's dark gray canvas, which needs no key: the land and water, then the place names over them.
    const esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/";
    L.tileLayer(esri + "World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
      maxNativeZoom: 16, maxZoom: 18, attribution: "Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
    }).addTo(map);
    L.tileLayer(esri + "World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", {maxNativeZoom: 16, maxZoom: 18}).addTo(map);
    const dots = L.layerGroup().addTo(map);
    const escape = text => text.replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
    const marker = row => {
      const name = row.querySelector(".title").textContent, units = +row.dataset.units, status = row.dataset.status;
      const dot = L.circleMarker([+row.dataset.lat, +row.dataset.lon], {
        radius: Math.max(3, Math.min(11, Math.sqrt(units) / 2.2)), weight: 1, color: "#000",
        fillColor: colors[status], fillOpacity: .85,
      });
      dot.bindPopup(`<a href="#${row.id}" class="to-row">${escape(name)}</a><br><span class="muted">`
        + `${units.toLocaleString()} homes · ${labels[status].toLowerCase()}</span>`);
      dot.on("popupopen", event => event.popup.getElement().querySelector(".to-row").addEventListener("click", click => {
        click.preventDefault();
        openProject(row, true);
      }));
      return dot;
    };
    const markers = new Map(rows.map(row => [row, marker(row)]));
    const show = () => {
      const chosen = buttons.find(button => button.getAttribute("aria-pressed") === "true").dataset.show;
      const words = search.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
      dots.clearLayers();
      for (const row of rows) {
        const status = row.dataset.status;
        const fits = (chosen === "all" || (chosen === "active" ? status !== "complete" : status === chosen))
          && words.every(word => row.dataset.words.includes(word));
        row.hidden = !fits;
        if (fits) dots.addLayer(markers.get(row));
      }
      for (const details of document.querySelectorAll("details")) {
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
      const dot = make("span", {className: "dot"});
      dot.style.background = colors[p.status];
      part("pills").replaceChildren(make("span", {className: "panel-pill"}, dot, labels[p.status]),
        make("span", {className: "panel-pill"}, p.units.toLocaleString() + (p.units === 1 ? " home" : " homes")));
      part("note").textContent = p.note;
      part("facts").replaceChildren(...p.facts.flatMap(([label, value]) => [make("dt", {textContent: label}), make("dd", {textContent: value})]));
      part("about").replaceChildren(...p.description.split(/\\n+/).map(text => text.trim()).filter(Boolean)
        .map(text => make("p", {textContent: text})));
      const image = part("image"), img = image.querySelector("img");
      image.classList.remove("loaded");
      image.hidden = !p.image;
      view.classList.toggle("shows-picture", !!p.image);
      if (p.image) { img.src = p.image; image.querySelector("a").href = p.image; } else img.removeAttribute("src");
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
      const row = shown, dot = markers.get(row);
      closeProject();
      if (row.hidden) dots.addLayer(dot);
      document.getElementById("map").scrollIntoView({behavior: "smooth", block: "center"});
      map.once("moveend", () => dot.openPopup());
      map.flyTo(dot.getLatLng(), 16);
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
      if (row && row.classList.contains("row")) {
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
    return (f'<p class="from{" stale" if stale else ""}">From <a href="https://www.massbuilds.com/">MassBuilds</a>'
            f'{f", last updated here {when(latest, today)}" if latest else ""}.</p>')


def render(projects, built_at, failed):
    today = built_at.date()
    order = lambda project: (updated(project) or date.min, project["units"])
    towns = {}
    for project in sorted(projects, key=order, reverse=True):
        towns.setdefault(project["town"], []).append(project)
    sections = "".join(
        f'<details><summary><span class="town">{html.escape(town)}</span><span class="count"></span></summary>'
        f'{source_note(town, rows, today)}<ul>{"".join(row(project, today) for project in rows)}</ul></details>'
        for town, rows in sorted(towns.items())
    )
    choices = [("active", "In progress")] + [(key, label) for key, (label, _) in STATUSES.items()] + [("all", "All")]
    filter_row = (
        '<div class="filter">' + "".join(
            f'<button type="button" data-show="{key}" aria-pressed="{"true" if key == "active" else "false"}">{label}</button>'
            for key, label in choices
        ) + f'<div class="finders">{shared.SEARCH}</div></div>'
    )
    legend = '<div class="legend">' + "".join(
        f'<span><span class="dot" style="background:{color}"></span>{label}</span>' for label, color in STATUSES.values()
    ) + "</div>"
    missing = (f'<p class="empty">Couldn’t load {" or ".join(failed)} this time; showing what the last build had.</p>'
               if failed else "")
    footer = (
        '<footer><p>From <a href="https://data.boston.gov/dataset/article80-development-projects">Boston’s Article 80 '
        'projects</a> (anything over about 20,000 square feet or 15 homes), <a href="' + CAMBRIDGE_PAGE +
        '">Cambridge’s development log</a> (50,000 square feet or 10 homes) and, for the towns around them, MAPC’s '
        '<a href="https://www.massbuilds.com/">MassBuilds</a>, with notes of our own. A project’s date is the latest '
        'of its filing, its approval, its last update and the day it was seen to move on.</p></footer>'
    )
    data = json.dumps({p["id"]: panel_data(p, today) for p in projects}, ensure_ascii=False).replace("</", "<\\/")
    body = (f'{filter_row}<div id="map"></div>{legend}{missing}{sections}{footer}{PANEL}'
            f'<script type="application/json" id="projects">{data}</script>')
    script = SCRIPT % (json.dumps({key: color for key, (_, color) in STATUSES.items()}),
                       json.dumps({key: label for key, (label, _) in STATUSES.items()}))
    return shared.page(
        "news", "Housing", body + script, css=CSS, head=LEAFLET, updated=built_at,
        links=[("Housing", "./", True)],
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

    projects, failed, kept = [], [], previous.get("sources", {})
    for town, url, read in SOURCES:
        try:
            found = read(shared.fetch(url, USER_AGENT, timeout=60)[1])
            print(f"✓ {town}: {len(found)} projects with homes")
        except Exception as error:
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
    (OUT_DIR / "index.html").write_text(render(projects, built_at, failed))
    saved = {"built": built_at.isoformat(), "projects": record, "sources": raw, "images": images}
    (OUT_DIR / "projects.json").write_text(json.dumps(saved, ensure_ascii=False, default=str))
    print(f"Wrote dist/housing/index.html and projects.json: {len(projects)} projects")


if __name__ == "__main__":
    main()
