#!/usr/bin/env python3
"""Note a project in edits.txt, from a GitHub issue titled "Note <project id>".

Each project's panel opens one of these pre-filled ("Suggest a correction"); anyone may open one, and
.github/workflows/housing-notes.yml applies only the repo owner's, with the title in $ISSUE_TITLE and the body
in $ISSUE_BODY. Writes the reply for the issue to reply.md, and changed= and summary= to $GITHUB_OUTPUT.

The body is the note itself, and any of these lines anywhere in it:

  status: proposed, approved, under construction, complete or stalled
  date: 2026-09-22
  link: an address for the panel's source button
  image: a picture of it
  hide: yes, to leave it off the site

A line with nothing after its colon is ignored, so the template's own empty fields do nothing.
"""

import json
import os
import re
import sys
from pathlib import Path

from housing import build

FIELDS = ("status", "date", "link", "image", "hide")


def said(body):
    """An issue's body as the note itself and the fields set in it."""
    fields, lines = {}, []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("<!--", "#", ">")):
            continue
        key, colon, value = line.partition(":")
        if colon and key.strip().lower() in FIELDS:
            if value.strip() and not value.strip().startswith("("):
                fields[key.strip().lower()] = value.strip()
        else:
            lines.append(line)
    return " ".join(lines).strip(), fields


def written(ident, note, fields):
    """The block edits.txt keeps for a project."""
    lines = [ident] + [f"{key}: {fields[key]}" for key in FIELDS if key in fields]
    if note:
        lines.insert(1, f"note: {note}")
    return "\n".join(lines)


def apply(ident, note, fields, text):
    """edits.txt with this project's block written or replaced, and what to say about it."""
    blocks = re.split(r"\n\s*\n", text.rstrip("\n"))
    block = written(ident, note, fields)
    for at, one in enumerate(blocks):
        first = next((line.strip() for line in one.splitlines() if line.strip() and not line.startswith("#")), "")
        if first == ident:
            blocks[at] = block
            return "\n\n".join(blocks) + "\n", "replaced"
    return "\n\n".join(blocks + [block]) + "\n", "added"


def known(ident):
    """The project's name, from the site's own panels; None where the site doesn't have it."""
    try:
        panels = json.loads(build.shared.fetch(build.SITE_URL + "panels.json", build.USER_AGENT, timeout=30)[1])
    except Exception as error:
        print(f"couldn’t read the site’s projects ({error})", file=sys.stderr)
        return None
    return (panels.get(ident) or {}).get("name")


def main():
    title = os.environ.get("ISSUE_TITLE", "")
    ident = title.split(None, 1)[1].strip() if len(title.split(None, 1)) > 1 else ""
    note, fields = said(os.environ.get("ISSUE_BODY", ""))
    reply, changed, summary = "", False, ""

    if not re.fullmatch(r"[a-z0-9-]+", ident):
        reply = f"“{ident}” doesn’t look like a project id. A note's title is *Note boston-4930*."
    elif fields.get("status") and fields["status"].lower() not in build.STATUSES:
        reply = (f"There’s no status called “{fields['status']}”. It can be "
                 + ", ".join(build.STATUSES) + ".")
    elif not note and not fields:
        reply = "This note is empty, so nothing was changed. Write what’s wrong or what’s new in the body."
    else:
        name = known(ident)
        if name is None:
            reply = f"The site has no project called `{ident}`, so nothing was changed."
        else:
            edits = Path("housing/edits.txt")
            text, how = apply(ident, note, fields, edits.read_text())
            edits.write_text(text)
            changed, summary = True, f"Note {name}"
            told = "".join(f"\n- {key}: {fields[key]}" for key in FIELDS if key in fields)
            reply = (f"{how.capitalize()} a note on **{name}** ({ident}):\n\n> {note or '(none)'}{told}\n\n"
                     f"It’ll be on the page in a minute or two: {build.SITE_URL}p/{ident}/")

    Path("reply.md").write_text(reply + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write(f"changed={'true' if changed else 'false'}\nsummary={summary}\n")


if __name__ == "__main__":
    main()
