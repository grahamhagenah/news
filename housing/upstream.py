"""What our edits to MassBuilds' projects (edits.txt) would change in MassBuilds itself, and, asked to, sending
them: a change that leaves the whole record valid as an edit, anything else as a flag, both of which a MassBuilds
moderator sees before they change anything there.

Run from the repo's top folder: python3 -m housing.upstream to see what would be sent, and

    MASSBUILDS_EMAIL=you@example.com MASSBUILDS_PASSWORD=… python3 -m housing.upstream --send

to send it. Nothing is sent by a build: this is run by hand, and posts publicly under that account. What's been
sent is kept in housing/sent.txt, so nothing goes twice

For each project we've changed, it says what MassBuilds has now, what we'd change it to, and whether MassBuilds
would take that as an edit, which must leave the whole record valid: where the record is missing something an
edit needs, the change goes in a flag instead, a note for MAPC to act on. Each project we've added in one of
MassBuilds' towns is listed as a new project to propose there."""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import shared.site as shared
from housing import build

API = "https://api.massbuilds.com"
RECORD_URL = API + "/developments/{}.jsonapi"
SENT = Path(__file__).parent / "sent.txt"

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


def sendings(edits, fetch_record=record):
    """What we'd send MassBuilds: for each project we've changed, an edit (its changed fields), a flag (our note,
    or a change the record is too thin to take), or both; and each project of ours in one of its towns, as one to
    add there by hand."""
    out = []
    for ident, fields in edits.items():
        if ident.startswith("massbuilds-"):
            number = ident.split("-", 1)[1]
            try:
                current = fetch_record(number)
            except Exception as error:
                out.append({"id": ident, "kind": "unread", "why": str(error)})
                continue
            changed = changes(fields, current)
            note = fields.get("note", "")
            if fields.get("hide", "").lower() in ("yes", "true"):
                note = note or "Hidden in our tracker: not new housing, or a duplicate."
            if not changed and not note:
                continue
            gaps = missing(current, changed) if changed else []
            one = {"id": ident, "number": number, "name": current.get("name"), "town": current.get("municipal"),
                   "page": build.MASSBUILDS_PAGE.format(number), "was": current, "changed": changed,
                   "note": note, "gaps": gaps}
            # A change the record is too thin to take goes as a flag, with what we'd have changed spelled out.
            one["kind"] = "edit" if changed and not gaps else "flag"
            if one["kind"] == "flag":
                said = "; ".join(f"{field}: {value}" for field, value in changed.items())
                one["reason"] = " ".join(part for part in (note, f"(We have {said}.)" if said else "") if part)
            out.append(one)
        elif fields.get("town") in build.INNER_RING and fields.get("lat") and fields.get("lon"):
            out.append({"id": ident, "kind": "new", "new": added(fields)})
    return out


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


def ask(url, data=None, token=None, email=None, kind="application/json"):
    """MassBuilds, asked something: its answer as what it sent back."""
    request = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"User-Agent": build.USER_AGENT, "Accept": kind, "Content-Type": kind})
    if token:
        request.add_header("Authorization", f'Token token="{token}", email="{email}"')
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            body = answer.read().decode("utf-8", "replace")
            return answer.status, json.loads(body) if body.strip().startswith(("{", "[")) else body
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        return error.code, json.loads(body) if body.strip().startswith(("{", "[")) else body


def signed_in(email, password):
    """A token for the account, from MassBuilds' sign-in."""
    status, answer = ask(f"{API}/my/users/sign_in", {"user": {"email": email, "password": password}})
    if status != 201 or not isinstance(answer, dict) or not answer.get("token"):
        raise SystemExit(f"Couldn’t sign in to MassBuilds ({status}): {answer}")
    return answer["token"]


def already_sent():
    """What's gone up before: "massbuilds-1549 edit"."""
    if not SENT.exists():
        return set()
    return {" ".join(line.split()[:2]) for line in SENT.read_text().splitlines()
            if line.strip() and not line.startswith("#")}


def post(one, token, email):
    """One correction, as an edit or a flag; the pair MassBuilds answers with."""
    about = {"development": {"data": {"type": "developments", "id": one["number"]}}}
    if one["kind"] == "edit":
        body = {"data": {"type": "edits", "attributes": {"proposed_changes": one["changed"]}, "relationships": about}}
        return ask(f"{API}/edits.jsonapi", body, token, email, kind="application/vnd.api+json")
    body = {"data": {"type": "flags", "attributes": {"reason": one["reason"]}, "relationships": about}}
    return ask(f"{API}/flags.jsonapi", body, token, email, kind="application/vnd.api+json")


def send(ours):
    """Each correction not sent before, posted; what happened, as lines of text."""
    email, password = os.environ.get("MASSBUILDS_EMAIL"), os.environ.get("MASSBUILDS_PASSWORD")
    if not email or not password:
        raise SystemExit("Set MASSBUILDS_EMAIL and MASSBUILDS_PASSWORD to send; they aren’t kept in the repo.")
    token = signed_in(email, password)
    before, lines, went = already_sent(), [], []
    for one in ours:
        if one["kind"] not in ("edit", "flag"):
            if one["kind"] == "new":
                lines.append(f"{one['new']['name']}: ours to add by hand at "
                             "https://www.massbuilds.com/map/developments/create")
            continue
        if f"{one['id']} {one['kind']}" in before:
            lines.append(f"{one['name']}: its {one['kind']} went up before.")
            continue
        status, answer = post(one, token, email)
        if status in (200, 201):
            lines.append(f"{one['name']}: {one['kind']} sent, for a MassBuilds moderator to approve. {one['page']}")
            went.append(f"{one['id']} {one['kind']} {date.today().isoformat()}")
        else:
            lines.append(f"{one['name']}: MassBuilds wouldn’t take the {one['kind']} ({status}): {answer}")
    if went:
        fresh = not SENT.exists() or not SENT.read_text().strip()
        with SENT.open("a") as kept:
            if fresh:
                kept.write("# What's gone up to MassBuilds, so nothing goes twice: <project> <edit|flag> <day>\n")
            kept.write("\n".join(went) + "\n")
    return lines


def main():
    edits = build.read_edits(build.EDITS.read_text())
    if "--send" in sys.argv[1:]:
        print("\n".join(send(sendings(edits))))
        return
    print("\n".join(report(edits)))


if __name__ == "__main__":
    sys.exit(main())
