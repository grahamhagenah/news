"""What our edits to MassBuilds' projects (edits.txt) would change in MassBuilds itself, for sending upstream by
hand, on each project's MassBuilds page. It only reads; nothing is sent.

Run from the repo's top folder: python3 -m housing.upstream

For each project we've changed, it says what MassBuilds has now, what we'd change it to, and whether MassBuilds
would take that as an edit, which must leave the whole record valid: where the record is missing something an
edit needs, the change goes in a flag instead, a note for MAPC to act on. Each project we've added in one of
MassBuilds' towns is listed as a new project to propose there."""

import json
import sys

import shared.site as shared
from housing import build

RECORD_URL = "https://api.massbuilds.com/developments/{}.jsonapi"

# What an edit must leave filled in, from MassBuilds' Edit model (rails/app/models/edit.rb). Its test for a
# project under way reads `status == ('in_construction' || 'completed')`, which in Ruby is only
# 'in_construction', so a finished project needs only the basics.
REQUIRED = ["name", "status", "latitude", "longitude", "year_compl", "hu", "commsf", "descr"]
UNDER_WAY_REQUIRED = ["singfamhu", "smmultifam", "lgmultifam", "affrd_unit", "aff_u30", "aff_30_50", "aff_50_80",
                      "aff_80p", "gqpop", "ret_sqft", "ofcmd_sqft", "indmf_sqft", "whs_sqft", "rnd_sqft", "ei_sqft",
                      "other_sqft", "hotel_sqft", "hotelrms", "publicsqft"]

# Our statuses in MassBuilds' terms. Proposed is either of its two for a project not yet begun.
AS_MASSBUILDS = {"under construction": "in_construction", "complete": "completed"}
NOT_BEGUN = ("projected", "planning")


def record(ident):
    """MassBuilds' record of a project, by its number."""
    return json.loads(shared.fetch(RECORD_URL.format(ident), build.USER_AGENT, timeout=60)[1])["data"]["attributes"]


def changes(fields, current):
    """What our edit would change in MassBuilds' record, as MassBuilds' own fields."""
    changed = {}
    status = fields.get("status", "").lower()
    if status == "stalled":
        if not current.get("stalled"):
            changed["stalled"] = True
    elif status:
        if current.get("stalled"):
            changed["stalled"] = False
        wanted = AS_MASSBUILDS.get(status)
        if wanted and current.get("status") != wanted:
            changed["status"] = wanted
        elif status == "proposed" and current.get("status") not in NOT_BEGUN:
            changed["status"] = "planning"
        if changed.get("status") == "completed" and fields.get("date"):
            year = int(fields["date"][:4])
            if current.get("year_compl") != year:
                changed["year_compl"] = year
    if fields.get("description") and fields["description"] != (current.get("descr") or "").strip():
        changed["descr"] = fields["description"]
    if fields.get("link") and fields["link"] != current.get("prj_url"):
        changed["prj_url"] = fields["link"]
    return changed


def missing(current, changed):
    """What an edit would need that the record, with the change made, doesn't have."""
    after = dict(current, **changed)
    needed = REQUIRED + (UNDER_WAY_REQUIRED if after.get("status") == "in_construction" else [])
    return [field for field in needed if after.get(field) is None or after.get(field) == ""]


def added(fields):
    """One of ours in a MassBuilds town, as a new MassBuilds project."""
    return {
        "name": fields.get("name"), "municipal": fields.get("town"), "address": fields.get("address", ""),
        "hu": build.number(fields.get("units")),
        "status": AS_MASSBUILDS.get(fields.get("status", "").lower(), "planning"),
        "stalled": fields.get("status", "").lower() == "stalled",
        "latitude": float(fields["lat"]), "longitude": float(fields["lon"]),
        "descr": fields.get("description") or fields.get("note", ""), "prj_url": fields.get("link", ""),
    }


def report(edits, fetch_record=record):
    """The report, as lines of text."""
    lines, count = [], 0
    for ident, fields in edits.items():
        if ident.startswith("massbuilds-"):
            number = ident.split("-", 1)[1]
            page = build.MASSBUILDS_PAGE.format(number)
            try:
                current = fetch_record(number)
            except Exception as error:
                lines += [f"{ident}: couldn't read MassBuilds' record ({error})", ""]
                continue
            changed = changes(fields, current)
            note = fields.get("note", "")
            if fields.get("hide", "").lower() in ("yes", "true"):
                note = note or "Hidden in our tracker: not new housing, or a duplicate."
            if not changed and not note:
                continue
            count += 1
            lines.append(f"{current.get('name')} ({current.get('municipal')})  {page}")
            for field, value in changed.items():
                lines.append(f"  {field}: {current.get(field)!r} → {value!r}")
            if changed:
                gaps = missing(current, changed)
                if gaps:
                    lines.append(f"  As a flag: an edit would be refused, since the record has no {', '.join(gaps)}.")
                else:
                    lines.append("  As an edit: the record has everything an edit needs.")
            if note:
                lines.append(f"  Flag: {note}")
            lines.append("")
        elif fields.get("town") in build.INNER_RING and fields.get("lat") and fields.get("lon"):
            count += 1
            new = added(fields)
            lines.append(f"New: {new['name']} ({new['municipal']})  https://www.massbuilds.com/map/developments/create")
            lines += [f"  {field}: {value!r}" for field, value in new.items() if value not in ("", None, False)]
            gaps = [field for field in REQUIRED if new.get(field) in (None, "")]
            gaps += ["year_compl", "commsf"]  # Ours never have these; MassBuilds asks for both.
            lines.append(f"  Also needs: {', '.join(dict.fromkeys(gaps))}.")
            lines.append("")
    lines.append(f"{count} project{'' if count == 1 else 's'} to send upstream." if count
                 else "Nothing to send upstream: no edits to MassBuilds' projects, or they all match.")
    return lines


def main():
    edits = build.read_edits(build.EDITS.read_text())
    print("\n".join(report(edits)))


if __name__ == "__main__":
    sys.exit(main())
