#!/usr/bin/env python3
"""Fetch every source in sources.txt and write the coming weeks' Boston-area events to dist/events. Run from
the repo's top folder: python3 -m events.build"""

import html
import json
import os
import re
import shutil
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from shared import site as shared

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.txt"
SKIP_FILE = ROOT / "skip.txt"
OUT_DIR = ROOT.parent / "dist" / "events"
REPO_URL = "https://github.com/grahamhagenah/news"
DAYS_AHEAD = 30  # How far ahead the page lists events,
MUSIC_DAYS_AHEAD = 60  # and concerts, which venues announce (and people buy tickets for) further ahead.
# Each build publishes its listings beside the page; a source that fails next time falls back to its copy
# there, if it's no older than this.
LISTINGS_URL = "https://events.grahamhagenah.com/listings.json"
FALLBACK_LIMIT = timedelta(days=2)
BOSTON = ZoneInfo("America/New_York")
USER_AGENT = "Mozilla/5.0 (compatible; events-feed/1.0)"
CATEGORIES = {"music": "Music", "film": "Film", "art": "Art & talks"}

# The same listings, for anyone: its own name, About and Contact pages, shorter previews, and only the
# sources fine to republish (a line in sources.txt ending in public=no stays on this page only).
PUBLIC_NAME = "Boston, Daily"
PUBLIC_DIR = ROOT.parent / "dist" / "public"
PUBLIC_URL = "https://boston.grahamhagenah.com/"
# Its pages link to each other from the site's root, since the home page puts a kind's page's address in the
# address bar without loading it, and a relative link would then go astray.
PUBLIC_ROOT = urlsplit(PUBLIC_URL).path
PUBLIC_CONTACT = "gwhagenah@gmail.com"  # Where the contact form's messages go, through FormSubmit.
PUBLIC_ABOUT_CHARS = 240  # Of the venue's own words in a preview.
PUBLIC_TITLE = f"{PUBLIC_NAME} · Concerts, films and talks around Boston"
PUBLIC_TAGLINE = "Concerts, films, and talks around Boston, Cambridge, and Somerville, aggregated from select venues."
# The public site's tonight page: what's still to come today. It holds tomorrow's too, and shows only the day it
# is where it's read, so it rolls over at midnight, before the next build.
PUBLIC_TONIGHT = ("tonight/", f"Things to do in Boston tonight · {PUBLIC_NAME}",
                  "Concerts, films and talks still to come today around Boston, Cambridge and Somerville, aggregated "
                  "from select venues.",
                  "Tonight around Boston, Cambridge, and Somerville: everything still to come today, aggregated from "
                  "select venues.")

# The public site's weekend pages, Friday to Sunday: this weekend's (the one it is, or from Monday to Thursday the
# one coming) and next weekend's. Each: its address, what it's called, its title and description.
PUBLIC_WEEKENDS = [
    ("weekend/", "This weekend", f"Things to do in Boston this weekend · {PUBLIC_NAME}",
     "Concerts, films and talks around Boston, Cambridge and Somerville this weekend, Friday to Sunday, "
     "aggregated from select venues."),
    ("weekend/next/", "Next weekend", f"Things to do in Boston next weekend · {PUBLIC_NAME}",
     "Concerts, films and talks around Boston, Cambridge and Somerville next weekend, Friday to Sunday, "
     "aggregated from select venues."),
]


def weekend_days(day, ahead=0):
    """The Friday and Sunday of the weekend day is in, or, Monday to Thursday, of the one coming; ahead=1 for
    the weekend after that."""
    friday = day - timedelta(days=day.weekday() - 4) if day.weekday() >= 4 else day + timedelta(days=4 - day.weekday())
    friday += timedelta(weeks=ahead)
    return friday, friday + timedelta(days=2)


def weekend_span(friday, sunday):
    """"Sep 18–20", or "Sep 30–Oct 2" across months."""
    return f"{friday:%b} {friday.day}–" + (f"{sunday.day}" if sunday.month == friday.month else f"{sunday:%b} {sunday.day}")


# Each kind's own page on the public site, which its filter link goes to: its address, and what it tells
# search engines and readers it is.
PUBLIC_PAGES = {
    "music": ("music", f"Concerts around Boston · {PUBLIC_NAME}",
              "Concerts at clubs, bars and halls across Boston, Cambridge and Somerville for the next two months, "
              "aggregated from select venues.",
              "Concerts around Boston, Cambridge, and Somerville, aggregated from select venues."),
    "film": ("film", f"Movie showtimes and repertory film in Boston · {PUBLIC_NAME}",
             "Showtimes at the Brattle, the Coolidge, the Harvard Film Archive and more, from repertory screenings "
             "to new releases, for the next month, aggregated from select theaters.",
             "Films around Boston, Cambridge, and Somerville, from repertory screenings to new releases, "
             "aggregated from select theaters."),
    "art": ("talks", f"Art, exhibitions and talks in Boston · {PUBLIC_NAME}",
            "Artist talks, lectures, exhibition openings and museum nights around Boston, Cambridge and Somerville "
            "for the next month, aggregated from select venues.",
            "Art and talks around Boston, Cambridge, and Somerville, aggregated from select venues."),
}
PUBLIC_DESCRIPTION = ("Concerts for the next two months, and film screenings and art talks for the next month, around "
                      "Boston, Cambridge and Somerville, on one page, from select venues.")

# Each venue's street address, town and ZIP, for the event listings search engines read; an event whose source
# gives its own (a library branch, an MIT building) uses that instead. Check a new venue's address when adding it.
VENUE_ADDRESSES = {
    "Roadrunner": ("89 Guest St", "Boston", "02135"),
    "The Sinclair": ("52 Church St", "Cambridge", "02138"),
    "The Middle East": ("472 Massachusetts Ave", "Cambridge", "02139"),
    "Middle East": ("472 Massachusetts Ave", "Cambridge", "02139"),  # As its own listings name it.
    "Sonia": ("10 Brookline St", "Cambridge", "02139"),
    "House of Blues": ("15 Lansdowne St", "Boston", "02215"),
    "Paradise": ("967 Commonwealth Ave", "Boston", "02215"),
    "Brighton Music Hall": ("158 Brighton Ave", "Boston", "02134"),
    "Crystal Ballroom": ("55 Davis Square", "Somerville", "02144"),
    "Somerville Theatre": ("55 Davis Square", "Somerville", "02144"),
    "Deep Cuts": ("21 Main St", "Medford", "02155"),
    "Midway Cafe": ("3496 Washington St", "Boston", "02130"),
    "Lizard Lounge": ("1667 Massachusetts Ave", "Cambridge", "02138"),
    "The Rockwell": ("255 Elm St", "Somerville", "02144"),
    "The Lilypad": ("1353 Cambridge St", "Cambridge", "02139"),
    "Club Passim": ("47 Palmer St", "Cambridge", "02138"),
    "Arts at the Armory": ("191 Highland Ave", "Somerville", "02143"),
    "MIT": ("77 Massachusetts Ave", "Cambridge", "02139"),
    "Boston Public Library": ("700 Boylston St", "Boston", "02116"),
    "MFA": ("465 Huntington Ave", "Boston", "02115"),
    "Harvard Art Museums": ("32 Quincy St", "Cambridge", "02138"),
    "ICA": ("25 Harbor Shore Dr", "Boston", "02210"),
    "SoWa": ("450 Harrison Ave", "Boston", "02118"),
    "French Library": ("53 Marlborough St", "Boston", "02116"),
    "ArtsEmerson": ("559 Washington St", "Boston", "02111"),
    "Brattle": ("40 Brattle St", "Cambridge", "02138"),
    "Coolidge Corner": ("290 Harvard St", "Brookline", "02446"),
    "Harvard Film Archive": ("24 Quincy St", "Cambridge", "02138"),
    "West Newton Cinema": ("1296 Washington St", "Newton", "02465"),
    "Kendall Square": ("355 Binney St", "Cambridge", "02142"),
    "Alamo Drafthouse": ("60 Seaport Blvd", "Boston", "02210"),
}


def postal(line):
    """An address written out, "134 Memorial Dr, Cambridge, MA 02139", as (street, town, ZIP); None otherwise."""
    found = re.match(r"\s*(\d[^,]*?),\s*([A-Za-z .]+?),?\s+(?:MA|Massachusetts)\b\.?\s*(\d{5})?", line or "")
    return (found.group(1).strip(), found.group(2).strip().title(), found.group(3) or "") if found else None


def read_sources():
    """sources.txt lines: how to read the source, its URL, a category, a name, and public=no for a source kept
    off the public page."""
    sources = []
    for line in SOURCES_FILE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            kind, url, category, *words = line.split()
            name = [word for word in words if word != "public=no"]
            sources.append({"kind": kind, "url": url, "category": category, "name": " ".join(name),
                            "public": len(name) == len(words)})
    return sources


def skipping():
    """skip.txt's words and phrases as one pattern, which finds any of them as whole words, in any case; None
    when there are none."""
    lines = SKIP_FILE.read_text().splitlines() if SKIP_FILE.exists() else []
    phrases = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return re.compile("|".join(rf"(?<!\w){re.escape(phrase)}(?!\w)" for phrase in phrases), re.I) if phrases else None


_fetched, _fetching = {}, threading.Lock()


def fetch(url, attempts=3, timeout=20):
    """The page or data at url, as text. Each address is downloaded once a build, however many times it's
    read (an ICA event's page, for each of its dates)."""
    address = url
    with _fetching:
        pending, first = _fetched.get(address), address not in _fetched
        if first:
            pending = _fetched[address] = Future()
    if first:
        try:
            pending.set_result(shared.fetch(address, USER_AGENT, attempts, timeout)[1].decode("utf-8", "replace"))
        except Exception as error:
            pending.set_exception(error)
    return pending.result()


def text(markup):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup or ""))).strip()


def event(source, title, day, start=None, link="", detail="", venue="", about=(), address=None):
    """One listing. start is a time of day in Boston, or None when the source gives only the date; about is
    what the source says about it, a few short paragraphs for its preview."""
    return {
        "title": title,
        "date": day,
        "times": [start] if start else [],
        "link": link or source["url"],
        "detail": detail,
        "venue": venue or source["name"],
        "category": source["category"],
        "source": source["name"],
        # Not a line that only repeats the name.
        "about": [paragraph for paragraph in about if paragraph.casefold() != title.casefold()],
        # Where it is, (street, town, ZIP), when the source says and it isn't the venue's usual address.
        "address": list(address) if address else None,
    }


ABOUT_CHARS = 400  # Roughly how much of what a source says about an event its preview shows.
# Short lines worth keeping, like "Doors 7pm", "21+" and "$10 cover": the rest are headings, names and labels.
FACT = re.compile(r"\d|\$|\b(free|ages?|doors?|cover|cash|sold out|cancel\w*|postponed|tickets?)\b", re.I)
NOT_ABOUT = re.compile(r"(buy|get)? ?(your )?tickets?( here| now)?|more info(rmation)?|learn more|rsvp( here)?|register( here)?", re.I)
LINKS = re.compile(r"https?://\S+|\b[\w-]+(\.[\w-]+)*\.(com|net|org|io|co|fm|me|us|bandcamp\.com)\b\S*", re.I)


def about(markup):
    """What a source says about an event, as its preview's paragraphs: its sentences, with short lines of facts
    ("Doors 7pm", "21+", "$10 cover") run together into one, and a sentence split by a line break made whole.
    Links, headings and "Buy tickets" are left out."""
    kept = []
    for paragraph in shared.excerpt(markup, ABOUT_CHARS * 2, min_words=1, max_paragraphs=12):
        paragraph = re.sub(r"\s+", " ", LINKS.sub("", paragraph.replace("**", ""))).strip(" ·|-")
        words = len(paragraph.split())
        if not paragraph or paragraph.endswith(":") or NOT_ABOUT.fullmatch(paragraph.strip(" .!")):
            continue
        if kept and not kept[-1][1] and not re.search(r"[.!?…:\"”)]$", kept[-1][0]) and words >= 3:
            kept[-1] = (f"{kept[-1][0]} {paragraph}", False)  # A sentence a line break cut in two.
        elif words >= 6:
            kept.append((paragraph, False))
        elif FACT.search(paragraph):
            if kept and kept[-1][1]:
                kept[-1] = (f"{kept[-1][0]} · {paragraph}", True)
            else:
                kept.append((paragraph, True))
    paragraphs, budget = [], ABOUT_CHARS
    for paragraph, _ in kept[:3]:
        if len(paragraph) > budget:
            paragraphs.append(paragraph[:budget].rsplit(" ", 1)[0] + "…")
            break
        paragraphs.append(paragraph)
        budget -= len(paragraph)
    return paragraphs


def at_boston(moment):
    """An aware datetime's date and time of day in Boston; a naive one is taken to be Boston time already."""
    local = moment.astimezone(BOSTON) if moment.tzinfo else moment
    return local.date(), local.time()


# Readers: each takes a source and returns its events.


def read_aeg(source):
    """AEG Presents venue sites (Roadrunner) load every listing from one events.json."""
    events = []
    for item in json.loads(fetch(source["url"]))["events"]:
        if not item.get("active") or item.get("private"):
            continue
        day, start = at_boston(datetime.fromisoformat(item["eventDateTimeISO"]))
        titles = item["title"]
        support = re.sub(r"\s*,\s*", ", ", titles.get("supportingText") or "").strip(" ,")
        events.append(event(
            source,
            titles.get("eventTitleText") or titles.get("headlinersText"),
            day,
            start,
            link=(item.get("ticketing") or {}).get("url", ""),
            detail=f"with {support}" if support else "",
            about=about(item.get("bio") or item.get("description")),  # Its description is mostly a ticket charity note.
        ))
    return events


def read_axs(source):
    """AXS venue sites' full listing, /events/all (The Sinclair): every show, with its supporting acts and door
    time. (Their RSS feed has only the next ten, with no times.) When the show itself starts is only on its own
    page, so those are read for the shows soon enough to be listed; a show whose page doesn't load keeps its
    door time, which its preview gives either way."""
    events = []
    for entry in fetch(source["url"]).split('<div class="entry')[1:]:
        def field(pattern):  # The date and time each come after an icon of their own.
            found = re.search(pattern, entry, re.S)
            return text(found.group(1)) if found else ""
        name = re.search(r'class="carousel_item_title_small">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', entry, re.S)
        day = re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", field(r'<span class="date">.*?</span>(.*?)</span>'))
        if not name or not day or field(r'class="btn-tickets[^"]*"[^>]*>(.*?)</a>').casefold() == "cancelled":
            continue
        doors = re.search(r"\d{1,2}:\d{2} [AP]M", field(r'<span class="time">.*?</span>(.*?)</span>'))
        start = datetime.strptime(doors.group(), "%I:%M %p").time() if doors else None
        support = field(r'class="supporting[^"]*">(.*?)</h4>')
        facts = [field(r'<h5 class="tour">(.*?)</h5>'), f"Doors {clock(start)}" if start else "", field(r'<span class="age">(.*?)</span>')]
        events.append(event(
            source,
            text(name.group(2)),
            datetime.strptime(day.group(), "%b %d, %Y").date(),
            start,
            link=html.unescape(name.group(1)),
            detail=f"with {support}" if support else "",
            about=[" · ".join(fact for fact in facts if fact)],
        ))
    soon = [listing for listing in events if listing["date"] <= window_end(source["category"])]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for listing, start in zip(soon, pool.map(lambda listing: axs_show_time(listing["link"]), soon)):
            if start:
                listing["times"] = [start]
    return events


def axs_show_time(link):
    """When a show starts, from its own page on an AXS venue site, or None if the page doesn't say or load. A
    second try, since one page in thirty now and then doesn't come the first time."""
    try:
        page = fetch(link, attempts=2, timeout=10)
    except Exception:
        return None
    found = re.search(r'<label class="event_time">\s*Time\s*</label>\s*<span>\s*(\d{1,2}:\d{2} [AP]M)', page)
    return datetime.strptime(found.group(1), "%I:%M %p").time() if found else None


def read_ticketweb(source):
    """Venue sites using TicketWeb's WordPress listing (The Middle East). Dates there leave out the year."""
    today = datetime.now(BOSTON).date()
    events = []
    for section in fetch(source["url"]).split('class="tw-section"')[1:]:
        name = re.search(r'class="tw-name">\s*<a[^>]*href="([^"]+)"[^>]*title="Event Name - (.*?) \| (\d{1,2} [A-Za-z]+) (\d{1,2}:\d{2} [AP]M)"', section)
        if not name:
            continue
        link, title, day_month, clock = name.groups()
        day = datetime.strptime(f"{day_month} {today.year}", "%d %B %Y").date()
        if day < today - timedelta(days=60):  # A January show listed in December.
            day = day.replace(year=today.year + 1)
        room = re.search(r'class="tw-venue-name">(.*?)</span>', section, re.S)
        events.append(event(
            source,
            html.unescape(title),
            day,
            datetime.strptime(clock, "%I:%M %p").time(),
            link=html.unescape(link),
            # "@ Middle East - Zuzu": just the venue, not the room; Sonia, next door, stays Sonia.
            venue=text(room.group(1)).lstrip("@ ").split(" - ")[0] if room else "",
        ))
    return events


def read_jsonld(source):
    """Pages that describe their screenings or shows as schema.org Events (the Brattle's Coming Soon page)."""
    found, works = [], {}

    def collect(data):
        if isinstance(data, list):
            for item in data:
                collect(item)
        elif isinstance(data, dict):
            kinds = data.get("@type") if isinstance(data.get("@type"), list) else [data.get("@type")]
            if any(isinstance(kind, str) and kind.endswith("Event") for kind in kinds) and data.get("startDate"):
                found.append(data)
            if data.get("@id") and len(data) > 1:
                works[data["@id"]] = data
            for value in data.values():
                if isinstance(value, (dict, list)):
                    collect(value)

    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', fetch(source["url"]), re.S):
        try:
            collect(json.loads(block))
        except ValueError:
            continue

    events = []
    for item in found:
        if str(item.get("eventStatus", "")).endswith("EventCancelled"):
            continue
        day, start = at_boston(datetime.fromisoformat(item["startDate"]))
        # The Brattle adds the showtime to each name: "Filipiñana - 9/12/26 @ 12:00 pm".
        title = re.sub(r"\s+-\s+\d{1,2}/\d{1,2}/\d{2,4}\s+@.*$", "", html.unescape(item.get("name", "")))
        work = item.get("workPresented") or {}
        work = works.get(work.get("@id"), work) if isinstance(work, dict) else {}
        events.append(event(source, title, day, start if "T" in item["startDate"] else None, link=item.get("url", ""),
                            about=jsonld_about(item, work)))
    return events


def jsonld_about(item, work):
    """An event's description, or its film's: who made it and who's in it, its genre, length and language. A
    description that's only its dates or showtime ("Filipiñana | Saturday, Sep 12, 4:30 PM | The Brattle") isn't
    one."""
    for description in (item.get("description"), work.get("description")):
        if isinstance(description, str) and not (len(description) < 90 and re.search(r"\d", description)):
            return about(description)

    def names(people):
        people = people if isinstance(people, list) else [people] if people else []
        return ", ".join(person.get("name", "") if isinstance(person, dict) else str(person) for person in people[:3])

    credits = []
    if names(work.get("director")):
        credits.append(f"Directed by {names(work['director'])}.")
    if names(work.get("actor")):
        credits.append(f"With {names(work['actor'])}.")
    facts = [", ".join(work["genre"]) if isinstance(work.get("genre"), list) else work.get("genre") or ""]
    length = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", work.get("duration") or "")
    if length and any(length.groups()):
        facts.append(" ".join(f"{n}{unit}" for n, unit in zip(length.groups(), ("h", "m")) if n))
    facts.append(work.get("inLanguage") if isinstance(work.get("inLanguage"), str) else "")
    facts = " · ".join(fact for fact in facts if fact)
    return [" ".join(credits)] * bool(credits) + [facts] * bool(facts)


def read_coolidge(source):
    """The Coolidge's showtimes page shows one day at a time (?date=2026-09-14) for about a week ahead."""
    first = fetch(source["url"])
    days = sorted(set(re.findall(r'data-date="(\d{4}-\d{2}-\d{2})"', first)))
    events = []
    for day_text in days:
        try:
            page = first if day_text == days[0] else fetch(f"{source['url']}?date={day_text}")
        except Exception as error:
            # One day's page failing shouldn't cost the rest of the week.
            print(f"  {source['name']}: skipped {day_text} ({error})", file=sys.stderr)
            continue
        day = date.fromisoformat(day_text)
        for card in page.split('<div class="film-card">')[1:]:
            film = re.search(r'class="film-card__link" title="([^"]+)" href="([^"]+)"', card)
            if not film:
                continue
            blurb = re.search(r'class="film-card__excerpt">(.*?)</div>', card, re.S)
            runtime = re.search(r'class="film-card__runtime">(.*?)</div>', card, re.S)
            facts = [text(runtime.group(1))] if runtime else []
            listing = event(source, html.unescape(film.group(1)), day, link=f"https://coolidge.org{film.group(2)}",
                            about=about(blurb.group(1) if blurb else "") + facts)
            listing["times"] = [
                datetime.strptime(clock.strip().upper(), "%I:%M%p").time()
                for clock in re.findall(r'class="showtime-ticket__time">([^<]+)<', card)
            ]
            events.append(listing)
    return events


def read_alamo(source):
    """Alamo Drafthouse loads a market's whole schedule (Boston: the Seaport) from one JSON file."""
    data = json.loads(fetch(source["url"]))["data"]
    titles = {item["slug"]: (item.get("show") or {}).get("title") for item in data["presentations"]}
    headlines = {item["slug"]: (item.get("show") or {}).get("headline") or "" for item in data["presentations"]}
    market = urlsplit(source["url"]).path.rstrip("/").rsplit("/", 1)[-1]
    events = []
    for session in data["sessions"]:
        title = titles.get(session["presentationSlug"])
        if session.get("isHidden") or session.get("status") in ("PAST", "CANCELED", "CANCELLED") or not title:
            continue
        # The theater's local time; a midnight show is dated the night it starts, not the business day.
        start = datetime.fromisoformat(session["showTimeClt"])
        link = f"https://drafthouse.com/{market}/show/{session['presentationSlug']}"
        events.append(event(source, title, start.date(), start.time(), link=link,
                            about=about(headlines.get(session["presentationSlug"]))))
    return events


def read_hfa(source):
    """The Harvard Film Archive's calendar page, about four weeks of screenings, each with its series, its
    director and year, and notes like "New 35mm print" or "Director in Person"."""
    events = []
    for block in fetch(source["url"]).split('class="grid-3 m-calendar__spot--event event"')[1:]:
        when = re.search(r'<time datetime="(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})', block)
        title = re.search(r'class="event__title">(.*?)</h5>', block, re.S)
        if not (when and title):
            continue
        link = re.search(r'<a href="([^"]+)" class="event__link"', block)
        series = re.search(r'class="event__series">(.*?)</div>', block, re.S)
        credit = re.search(r'class="event__info">(.*?)</div>', block, re.S)
        notes = [text(note) for note in re.findall(r'class="tooltip">(.*?)</span>', block, re.S)]
        name = text(title.group(1))
        # "Directed by João César Monteiro, 2000"; the series it's part of, and how it's shown.
        credit = re.sub(r"\s*,\s*", ", ", text(credit.group(1) if credit else ""))
        part = re.sub(r"\s*\.\.\.$", "…", text(series.group(1) if series else ""))  # The page cuts long ones short.
        facts = " · ".join(([f"Part of {part}"] if part and part != name else []) + notes)
        events.append(event(
            source, name, date.fromisoformat(when.group(1)),
            datetime.min.time().replace(hour=int(when.group(2)), minute=int(when.group(3))),
            link=urljoin(source["url"], html.unescape(link.group(1))) if link else "",
            about=[line for line in (credit, facts) if line],
        ))
    return events


# The ICA's event types, from the tags on each: its concerts go under Music, its screenings under Film, and its
# talks, dance and First Fridays under Art & talks. Anything for kids, teens or members, online, or only a
# workshop or party (like its gala) is left out.
ICA_SKIP = {"ICA Kids", "ICA Teens", "ICA Members", "Virtual events"}
ICA_KINDS = [("Music", "music"), ("Film", "film"), ("Talks", "art"), ("Talk", "art"), ("Dance", "art"),
             ("ICA Live", "art"), ("First Fridays", "art")]


def clock_range_start(when):
    """The start of "12–4 PM", "10 AM–5 PM", "5:30–9:30 PM" or "7 PM": a start without its own AM or PM takes
    the end's, unless that would put it after the end ("11–1 PM" starts at 11 AM)."""
    found = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?(?:\s*[–-]\s*(\d{1,2})(?::\d{2})?\s*([AP]M))?", when.strip(), re.I)
    if not found:
        return None
    hour, minute, meridiem, end_hour, end_meridiem = found.groups()
    if not meridiem:
        if not end_meridiem:
            return None
        meridiem = end_meridiem
        if int(hour) % 12 > int(end_hour) % 12:
            meridiem = "AM" if end_meridiem.upper() == "PM" else "PM"
    hour = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
    return datetime.min.time().replace(hour=hour, minute=int(minute or 0))


def read_ica(source):
    """The ICA's calendar page, a couple of months of events, dated like "Sun, Sep 20, 2 PM" (no year)."""
    today_ = today()
    events = []
    for node in fetch(source["url"]).split('class="ds-1col node node-event')[1:]:
        title = re.search(r'<h3 class="teaser-title">\s*<a href="([^"]+)">(.*?)</a>', node, re.S)
        when = re.search(r'class="event-date-display">(.*?)</div>', node, re.S)
        kinds = {text(tag) for tag in re.findall(r'rel="tag">(.*?)</a>', node, re.S)}
        category = next((category for kind, category in ICA_KINDS if kind in kinds), None)
        if not (title and when and category) or kinds & ICA_SKIP:
            continue
        day_text, _, clock_text = text(when.group(1)).partition(", ")[2].partition(", ")
        try:
            day = datetime.strptime(f"{day_text} {today_.year}", "%b %d %Y").date()
        except ValueError:
            continue
        if day < today_ - timedelta(days=60):  # January's events, listed in December.
            day = day.replace(year=today_.year + 1)
        name = text(re.sub(r"<[^>]+>", "", title.group(2)))  # Italics for a work's name, without a gap after.
        link = html.unescape(title.group(1))
        # Its own page for what it's about, only when it's soon enough to be listed.
        listing = event(source, name, day, clock_range_start(clock_text), link=link,
                        about=ica_about(link) if day <= window_end(category) else [])
        listing["category"] = category
        events.append(listing)
    return events


def ica_about(link):
    """What an ICA event's own page says about it, and what tickets cost; the calendar has neither. A page that
    doesn't load leaves the event without a preview, not the ICA without events."""
    try:
        page = fetch(link, attempts=1, timeout=10)
    except Exception:
        return []

    def field(name):
        start = page.find(f"field-name-{name} ")
        if start < 0:
            return ""
        end = page.find('class="field field-name-', start + 1)
        return page[page.find(">", start) + 1:end if end > 0 else None]

    price = re.sub(r"\s*Get Tickets.*$", "", text(field("field-ticket-price")), flags=re.I)
    # Not the notice of when tickets go on sale that many start with.
    described = [line for line in about(field("body")) if not re.match(r"(member |nonmember )?(presale|tickets)", line, re.I)]
    return described[:2] + ([price] if price else [])


# The French Library's event types, from the label on each, and where each goes: its screenings are Film,
# the rest of its programs Art & talks. Its classes, clubs and children's events are left out.
FRENCH_LIBRARY_KINDS = {"Talks & Discussions": "art", "Arts & Exhibitions": "art", "Performing Arts & Screenings": "art"}


def read_french_library(source):
    """The French Library's upcoming events page, all of them on one page."""
    events = []
    for card in fetch(source["url"]).split('class="new_event_card')[1:]:
        kind = re.search(r'class="event-type-pill">(.*?)<', card, re.S)
        category = FRENCH_LIBRARY_KINDS.get(text(kind.group(1)) if kind else "")
        title = re.search(r'<h3 class="subsection_title">\s*<a href="([^"]+)">(.*?)</a>', card, re.S)
        day = re.search(r'class="event_date">\w+, (\w+ \d{1,2}, \d{4})<', card)
        clock_text = re.search(r'class="event_time">(\d{1,2}):(\d{2}) ([AP])M', card)
        online = re.search(r'class="event_format">\s*Online', card)
        if not (category and title and day) or online:
            continue
        name = re.sub(r"\s+", " ", text(title.group(2)))
        excerpt = re.search(r'class="event_excerpt">(.*?)</p>', card, re.S)
        # A screening says so in its name ("Ciné-Club: …") or its description ("silent cinema, with live music").
        if text(kind.group(1)) == "Performing Arts & Screenings" and re.search(
                r"\bcin[ée]|screening|\bfilms?\b", f"{name} {text(excerpt.group(1)) if excerpt else ''}", re.I):
            category = "film"
        start = None
        if clock_text:
            hour = int(clock_text.group(1)) % 12 + (12 if clock_text.group(3) == "P" else 0)
            start = datetime.min.time().replace(hour=hour, minute=int(clock_text.group(2)))
        listing = event(source, name, datetime.strptime(day.group(1), "%B %d, %Y").date(), start,
                        link=html.unescape(title.group(1)), about=about(excerpt.group(1) if excerpt else ""))
        listing["category"] = category
        events.append(listing)
    return events


def read_veezi_site(source):
    """Theaters whose sites are Veezi's hosted ones (West Newton Cinema), from the data the site loads: each
    film playing now or coming soon, with its showtimes and synopsis."""
    films = {}
    for listing in ("playing-now", "coming-soon"):
        for film in json.loads(fetch(urljoin(source["url"], f"/api/movie/{listing}"))):
            films.setdefault(film["url"], film)
    events = []
    for film in films.values():
        credits = film.get("director") if isinstance(film.get("director"), dict) else {}
        directors = ", ".join(credits.get("director") or [])
        minutes = int(film["duration"]) if str(film.get("duration") or "").isdigit() else 0
        facts = " · ".join(fact for fact in (f"Directed by {directors}" if directors else "",
                                               f"{minutes // 60}h {minutes % 60}m" if minutes else "") if fact)
        described = about(film.get("synopsisShort") or film.get("tagline") or "") + ([facts] if facts else [])
        for session in film.get("sessionTimes") or []:
            clock_text = re.fullmatch(r"(\d{1,2}):(\d{2}) ?([ap])m", (session.get("time") or "").strip(), re.I)
            if not session.get("date") or not clock_text:
                continue
            hour = int(clock_text.group(1)) % 12 + (12 if clock_text.group(3).lower() == "p" else 0)
            events.append(event(source, film["title"], date.fromisoformat(session["date"][:10]),
                                datetime.min.time().replace(hour=hour, minute=int(clock_text.group(2))),
                                link=urljoin(source["url"], f"/movie/{film['url']}"), about=described))
    return events


LANDMARK_API = "https://www.landmarktheatres.com/api/gatsby-source-boxofficeapi"


def read_landmark(source):
    """Landmark theaters (Kendall Square), by theater id, through the schedule service their site uses."""
    today = datetime.now(BOSTON).date()
    theater = json.dumps({"id": source["url"], "timeZone": "America/New_York"}, separators=(",", ":"))
    query = urlencode({
        "from": f"{today}T03:00:00",
        "to": f"{today + timedelta(days=days_ahead(source['category']) + 1)}T03:00:00",
        "theaters": theater,
    })
    schedule = json.loads(fetch(f"{LANDMARK_API}/schedule?{query}"))[source["url"]]["schedule"]
    if not schedule:
        return []
    # The schedule has only film ids; titles come separately, all at once.
    films = json.loads(fetch(f"{LANDMARK_API}/movies?" + urlencode([("ids", film) for film in schedule])))
    titles = {film["id"]: film["title"] for film in films}
    events = []
    for film, days in schedule.items():
        for showings in days.values():
            for showing in showings:
                if showing.get("isExpired") or film not in titles:
                    continue
                start = datetime.fromisoformat(showing["startsAt"])
                tickets = next(
                    (entry["urls"][0] for entry in (showing.get("data") or {}).get("ticketing", [])
                     if entry.get("type") == "DESKTOP" and entry.get("urls")),
                    "",
                )
                events.append(event(source, titles[film], start.date(), start.time(), link=tickets))
    return events


# Ticketmaster's own type for each show ("segment"). Concerts and films are listed; other types, like the
# comedy, drag and theater nights (Arts & Theatre) some music venues host, are left out. A show without a
# type keeps the venue's category.
TICKETMASTER_SEGMENTS = {"Music": "music", "Film": "film"}


def read_ticketmaster(source):
    """Ticketmaster venues (the Paradise), by Discovery API venue id. The API needs a free key, read from the
    TICKETMASTER_KEY environment variable (a repository secret on GitHub); without one these are skipped."""
    key = (os.environ.get("TICKETMASTER_KEY") or "").strip()  # A pasted key can bring a space or line break.
    if not key:
        print(f"  {source['name']}: skipped, no TICKETMASTER_KEY", file=sys.stderr)
        return []
    query = urlencode({"apikey": key, "venueId": source["url"], "size": 100, "sort": "date,asc"})
    data = json.loads(fetch(f"https://app.ticketmaster.com/discovery/v2/events.json?{query}"))
    events = []
    for item in (data.get("_embedded") or {}).get("events", []):
        dates = item.get("dates") or {}
        start = dates.get("start") or {}
        if not start.get("localDate") or (dates.get("status") or {}).get("code") == "cancelled":
            continue
        # Ticketmaster gives the show's own local date and time, which for these venues is Boston's.
        clock = None
        if start.get("localTime") and not start.get("timeTBA"):
            clock = datetime.strptime(start["localTime"], "%H:%M:%S").time()
        types = item.get("classifications") or []
        primary = next((kind for kind in types if kind.get("primary")), types[0] if types else {})
        segment = (primary.get("segment") or {}).get("name")
        if segment and segment not in TICKETMASTER_SEGMENTS:
            continue
        notes = item.get("description") or item.get("info") or item.get("pleaseNote") or ""
        listing = event(source, item["name"], date.fromisoformat(start["localDate"]), clock, link=item.get("url", ""),
                        about=about(notes))
        listing["category"] = TICKETMASTER_SEGMENTS.get(segment, source["category"])
        events.append(listing)
    return events


# A line of a web page's script left in an event's description (a ticket button's tracking): not about it.
CODE = re.compile(r"\$\(|\bfunction\s*\(|\bfbq\(|^\s*[})\]]+\)?;?\s*$")


def read_ics(source, kinds=None):
    """iCalendar feeds. Only each event's first date; repeating events aren't expanded. With kinds, a map from
    the feed's categories to ours, each event goes in the first of its categories there, and one in none of
    them is left out."""
    def unescape(value):
        return re.sub(r"\\([,;\\])", r"\1", value).replace("\\n", " ").strip()

    lines = re.sub(r"\r?\n[ \t]", "", fetch(source["url"])).splitlines()  # Undo line folding.
    events, fields = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            fields = {}
        elif line == "END:VEVENT" and fields is not None:
            # Each field is (parameters, value), as in DTSTART;TZID=America/New_York:20260919T120000.
            tags = [unescape(tag) for tag in re.split(r"(?<!\\),", fields.get("CATEGORIES", ("", ""))[1])]
            category = next((kinds[tag] for tag in tags if tag in kinds), None) if kinds else source["category"]
            if not category or fields.get("STATUS", ("", ""))[1] == "CANCELLED":
                fields = None
                continue
            params, value = fields.get("DTSTART", ("", ""))
            if re.fullmatch(r"\d{8}", value):
                day, start = datetime.strptime(value, "%Y%m%d").date(), None
            else:
                moment = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
                zone = re.search(r"TZID=([^;]+)", params)
                if value.endswith("Z"):
                    moment = moment.replace(tzinfo=timezone.utc)
                elif zone:
                    moment = moment.replace(tzinfo=ZoneInfo(zone.group(1)))
                day, start = at_boston(moment)
            venue, _, place = unescape(fields.get("LOCATION", ("", ""))[1]).partition(",")
            summary = unescape(fields.get("SUMMARY", ("", ""))[1])
            description = re.sub(r"\\([,;\\])", r"\1", fields.get("DESCRIPTION", ("", ""))[1]).split("\\n")
            description = "\n\n".join(part for part in description if not CODE.search(part))
            listing = event(source, summary, day, start, link=fields.get("URL", ("", ""))[1], venue=venue,
                            about=about(html.escape(html.unescape(description))), address=postal(place))
            listing["category"] = category
            events.append(listing)
            fields = None
        elif fields is not None and ":" in line:
            name, value = line.split(":", 1)
            key, _, params = name.partition(";")
            fields.setdefault(key, (params, value))
    return events


# Arts at the Armory's kinds of event, from its calendar's categories, and where each goes; not its comedy,
# dance nights and classes, markets or community meetings.
ARMORY_KINDS = {"Music": "music", "Literary Art": "art", "Film": "film"}


def read_armory(source):
    """Arts at the Armory's calendar feed, kept to its concerts, readings and talks, and screenings; not a
    show it still lists after moving it to another venue, "[Moved to the Royale]"."""
    return [listing for listing in read_ics(source, ARMORY_KINDS) if not re.search(r"\[moved to", listing["title"], re.I)]


# What a Squarespace venue lists that isn't a show: its yoga classes and comedy nights.
SQUARESPACE_SKIP = {"Yoga", "Comedy"}


def read_squarespace(source):
    """A Squarespace site's events page (The Lilypad's), from the data it gives with ?format=json: every
    upcoming event, with when it starts and a few lines on it, admission first. Not a private event."""
    events = []
    for item in json.loads(fetch(source["url"] + "?format=json")).get("upcoming", []):
        title = re.sub(r"\s+", " ", html.unescape(item["title"])).strip()
        if SQUARESPACE_SKIP & set(item.get("categories") or []) or re.search(r"private event", title, re.I):
            continue
        day, start = at_boston(datetime.fromtimestamp(item["startDate"] // 1000, timezone.utc))
        described = re.sub(r"<(style|script)\b.*?</\1>", "", item.get("body") or item.get("excerpt") or "", flags=re.S)
        events.append(event(source, title, day, start, link=urljoin(source["url"], item["fullUrl"]), about=about(described)))
    return events


def read_passim(source):
    """Club Passim's calendar, which is in its page's script: for each show a title, a date, a showtime, a line
    on it ("album release with special guest …") and its ticket link. Only shows sold by Passim's own box
    office, which are in the club; not the ones it presents elsewhere (at the Crane Estate, on a hike) or
    cancelled. The page repeats a show for each month it draws, so each is kept once."""
    events, seen = [], set()
    for show in fetch(source["url"]).split("{title: ")[1:]:
        def field(key):
            found = re.search(rf'\b{key}: "(.*?)",', show)
            return html.unescape(found.group(1)) if found else ""
        title, day = html.unescape(show.split('",', 1)[0].lstrip('"')), field("date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or (field("postID"), day) in seen:
            continue
        seen.add((field("postID"), day))
        if "passim.my.salesforce-sites.com" not in show or re.match(r"cancel", title, re.I) or re.search(r" at (the )?[A-Z]", title):
            continue
        when = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)", field("showtime").strip(), re.I)
        start = datetime.strptime(f"{when.group(1)}:{when.group(2) or '00'} {when.group(3).upper()}", "%I:%M %p").time() if when else None
        events.append(event(source, title, date.fromisoformat(day), start, link=field("link"), detail=field("detail")))
    return events


# Calendar entries that aren't shows: Lizard Lounge's placeholders for its dark nights, and its weekly poetry jam.
NOT_SHOWS = re.compile(r"^No Event Tonight|Poetry Jam", re.I)


def read_tribe(source):
    """WordPress sites using The Events Calendar (Lizard Lounge, The Rockwell), through its REST API. The URL
    can pick a category, like ?categories=music for The Rockwell's music among its comedy and theater."""
    today = datetime.now(BOSTON).date()
    end = today + timedelta(days=days_ahead(source["category"]))
    window = {"start_date": today.isoformat(), "end_date": f"{end} 23:59:59", "per_page": 50}
    url = source["url"] + ("&" if "?" in source["url"] else "?") + urlencode(window)
    events = []
    while url:
        data = json.loads(fetch(url))
        for item in data.get("events", []):
            title = html.unescape(item["title"])
            if item.get("hide_from_listings") or NOT_SHOWS.search(title):
                continue
            # The venue's own local time, which for these is Boston's.
            moment = datetime.strptime(item["start_date"], "%Y-%m-%d %H:%M:%S")
            events.append(event(source, title, moment.date(), None if item.get("all_day") else moment.time(),
                                link=item.get("url", ""), about=about(item.get("description") or item.get("excerpt"))))
        url = data.get("next_rest_url")  # 50 to a page.
    return events


def today():
    return datetime.now(BOSTON).date()


def days_ahead(category):
    """How many days ahead a kind of event is listed: two months for concerts, a month for the rest."""
    return MUSIC_DAYS_AHEAD if category == "music" else DAYS_AHEAD


def window_end(category=None):
    """The last day a kind of event is listed; without one, the rest's."""
    return today() + timedelta(days=days_ahead(category))


def opening(source, title, first, last, link, start=None, about=()):
    """An exhibition, listed once, on the day it opens, with when it closes; one already open is left out, so it
    isn't at the top of every day for months."""
    if first < today():
        return []
    return [event(source, title, first, start, link=link, detail=f"through {last:%b} {last.day}", about=about)]


MIT_EXHIBITS, MIT_LECTURES = 102763, 102764  # Its event_types filter's ids for Exhibits, Conferences/Seminars/Lectures.


def read_mit(source):
    """MIT's events calendar (Localist), kept to what's open to the public and about art: exhibitions, and talks
    from its architecture and humanities schools or listed under the arts. MIT tags many public events for no
    audience at all, so only events tagged for some other audience alone are left out."""
    arts = {"School of Architecture and Planning (SA+P)", "School of Humanities, Arts, and Social Sciences (SHASS)"}
    events, seen, page = [], set(), 1
    while page:
        # Only its exhibits and its conferences, seminars and lectures (type[] ids, either of them), about
        # 180 events a month, not all 400-odd.
        query = urlencode([("days", DAYS_AHEAD + 1), ("pp", 100), ("page", page), ("type[]", MIT_EXHIBITS), ("type[]", MIT_LECTURES)])
        data = json.loads(fetch(f"{source['url']}?{query}"))
        for entry in data.get("events", []):
            item = entry["event"]
            tags = {key: {tag["name"] for tag in value} for key, value in (item.get("filters") or {}).items()}
            types, audience = tags.get("event_types", set()), tags.get("event_audience", set())
            state = (item.get("geo") or {}).get("state")
            if ((audience and "Public" not in audience) or item.get("experience") == "virtual"
                    or item.get("status") == "canceled" or (state and state != "MA")):  # A few are in New York.
                continue
            talk = "Conferences/Seminars/Lectures" in types and (
                tags.get("event_events_by_school", set()) & arts or tags.get("event_events_by_interest", set()) & {"Arts/Music/Film", "MIT Museum"})
            if not ("Exhibits" in types or talk):
                continue
            # A listing per day an event happens; a talk is each of them, an exhibition just its opening.
            instance = entry["event"]["event_instances"][0]["event_instance"]
            moment = datetime.fromisoformat(instance["start"])
            day, start = at_boston(moment)
            # Midnight is how an event with its time left off comes through.
            start = None if instance.get("all_day") or (start.hour, start.minute) == (0, 0) else start
            link = item.get("localist_url") or source["url"]
            described = about(item.get("description") or "")
            first, last = date.fromisoformat(item["first_date"]), date.fromisoformat(item["last_date"])
            if "Exhibits" in types and last > first:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    events += opening(source, item["title"], first, last, link, about=described)
                continue
            events.append(event(source, item["title"], day, start, link=link, about=described, address=postal(item.get("address"))))
        page = data.get("page", {}).get("next_page") if page < 20 else None
    return events


# A library's events (BiblioCommons) that aren't talks or exhibitions for grown-ups, by the tags they carry.
LIBRARY_SKIP = {
    "Kirstein Business Library & Innovation Center Classes", "Job & Career Success", "Computers/Technology Classes",
    "Financial Empowerment", "Small Business", "Workshops & Classes", "Story Time", "Early Literacy",
    "English for Speakers of Other Languages (ESOL)", "Artificial Intelligence (AI)", "Arts & Crafts", "Film",
    "Health / Fitness", "Human Services",
}
LIBRARY_YOUNG = {"Babies (0-24 months)", "Toddlers (Ages 2-3)", "Preschoolers (Ages 3-5)", "Children (Ages 6-12)",
                 "Tweens (Ages 9-12)", "Teens (Ages 13-18)", "Families"}
LIBRARY_AUDIENCES = LIBRARY_YOUNG | {"All Adults", "College Students", "Older Adults", "Young Adults (Ages 20-34)", "Visitors"}


def read_bibliocommons(source):
    """A library's events feed from BiblioCommons (the Boston Public Library's), filtered by type in its URL
    (?types=…), 25 to a page in date order, which the rest of its own filters can't narrow further."""
    end = window_end(source["category"])
    events = []
    for page in range(1, 21):
        feed = fetch(f"{source['url']}&page={page}")
        items = re.findall(r"<item>(.*?)</item>", feed, re.S)
        for item in items:
            def field(name):
                found = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", item, re.S)
                return html.unescape(found.group(1).strip()) if found else ""
            tags = {html.unescape(tag) for tag in re.findall(r"<category>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</category>", item)}
            audiences = tags & LIBRARY_AUDIENCES
            day, start = at_boston(datetime.fromisoformat(field("bc:start_date").replace("Z", "+00:00")))
            if (field("bc:is_cancelled") == "true" or field("bc:is_virtual") == "true" or tags & LIBRARY_SKIP
                    or (audiences and audiences <= LIBRARY_YOUNG)):
                continue
            branch = re.sub(r"\s+in\s+.*$", "", text(field("bc:name")))  # "Central Library in Copley Square"
            venue = source["name"] if branch.startswith("Central") or not branch else f"{branch} Library"
            title, link = text(field("title")), field("link")
            described = about(field("description"))
            street = " ".join(part for part in (field("bc:number"), field("bc:street")) if part)
            place = (street, field("bc:city") or "Boston", field("bc:zip")) if street else None
            last = date.fromisoformat((field("bc:end_date_local") or day.isoformat())[:10])
            if "Exhibitions" in tags and last > day:
                events += [dict(listing, venue=venue) for listing in opening(source, title, day, last, link, about=described)]
            else:
                events.append(event(source, title, day, start, link=link, venue=venue, about=described, address=place))
        if not items or day > end:
            break
    return events


# The MFA's programs, from the list of upcoming ones on each kind's own page, and where each kind goes; the
# rest (guided tours, studio classes, courses, member hours) aren't read at all.
MFA_SECTIONS = [("lectures", "art"), ("special-event", "art"), ("film", "film"), ("music", "music")]


def read_mfa(source):
    """The MFA's lectures, special events, films and concerts, 25 to a page, soonest first. A program spanning
    several days (a course, a festival) is a heading over its own dated programs, so only single days are kept."""
    events = []
    for section, category in MFA_SECTIONS:
        end = window_end(category)
        for page in range(10):
            markup = fetch(f"{source['url']}/{section}" + (f"?page={page}" if page else ""))
            programs = re.findall(
                r'<div\s+class="col-lg-8">.*?<h[23] class="field-content"><a href="([^"]+)">(.*?)</a></h[23]>'
                r'.*?<span class="date-display-range">(.*?)</span>', markup, re.S)
            day = None
            for link, title, when in programs:
                # "Saturday, September 12, 2026<br>10:00 am–11:15 am"; a span reads "Friday, October 2–Friday, …".
                single = re.fullmatch(r"\w+, (\w+ \d{1,2}, \d{4})(?:<br>\s*(\d{1,2})(?::(\d{2}))?\s*([ap])m.*)?", when.strip(), re.S)
                if not single:
                    continue
                day = datetime.strptime(single.group(1), "%B %d, %Y").date()
                start = None
                if single.group(2):
                    hour = int(single.group(2)) % 12 + (12 if single.group(4) == "p" else 0)
                    start = datetime.min.time().replace(hour=hour, minute=int(single.group(3) or 0))
                listing = event(source, text(title), day, start, link=f"https://www.mfa.org{html.unescape(link)}")
                listing["category"] = category
                events.append(listing)
            if 'rel="next"' not in markup or (day and day > end):
                break
    return events


# Harvard Art Museums' event types (its calendar page's EVENT_CATEGORIES) that are kept, and where each goes;
# the rest (student-led spotlight tours, workshops, supporter and special events like classes) are left out.
HARVARD_ART_KINDS = {2: "art", 3: "art", 4: "art", 9: "art", 10: "art", 13: "film"}


def harvard_art_listings(url, months):
    """Each month's events from Harvard Art Museums' calendar, which the page asks for with a form post that
    needs its session cookie and the token on the page."""
    import http.cookiejar
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    page = opener.open(url, timeout=20).read().decode("utf-8", "replace")
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    listings = []
    for year, month in months:
        body = urlencode({"year": year, "month": month - 1, "day": 0}).encode()  # Its months count from 0.
        request = urllib.request.Request(urljoin(url, "/events/calendar/listings"), data=body, headers={"X-CSRF-TOKEN": token})
        listings += json.loads(opener.open(request, timeout=20).read())
    return listings


def read_harvard_art(source):
    months = sorted({(day.year, day.month) for day in (today(), window_end(source["category"]))})
    events = []
    for item in harvard_art_listings(source["url"], months):
        category = HARVARD_ART_KINDS.get(int(item.get("type") or 0))
        # Its formatted title, with italics for works of art, and no tag leaving a space where it ends.
        formatted = (item.get("html_attributes") or {}).get("title") or item["title"].replace("_", "")
        title = text(re.sub(r"<[^>]+>", "", formatted))
        if not category or re.search(r"\bonline\b|\bcancel", title, re.I):
            continue
        moment = datetime.fromisoformat(item["date"].replace("Z", "+00:00")).replace(second=0, microsecond=0)
        day, start = at_boston(moment)
        listing = event(source, title, day, start, link=item.get("event_link") or source["url"],
                        about=about(item.get("summary") or ""))
        listing["category"] = category
        events.append(listing)
    return events


READERS = {
    "aeg": read_aeg,
    "axs": read_axs,
    "ticketweb": read_ticketweb,
    "jsonld": read_jsonld,
    "coolidge": read_coolidge,
    "ticketmaster": read_ticketmaster,
    "alamo": read_alamo,
    "landmark": read_landmark,
    "ics": read_ics,
    "armory": read_armory,
    "squarespace": read_squarespace,
    "passim": read_passim,
    "tribe": read_tribe,
    "hfa": read_hfa,
    "frenchlibrary": read_french_library,
    "ica": read_ica,
    "veezi": read_veezi_site,
    "mit": read_mit,
    "bibliocommons": read_bibliocommons,
    "mfa": read_mfa,
    "harvardart": read_harvard_art,
}


def load(source):
    try:
        return source, READERS[source["kind"]](source), None
    except Exception as error:
        return source, [], error


def merge_showings(events):
    """One row per film (or show) per day and place, with all its times, instead of a row per showing."""
    merged = {}
    for item in events:
        key = (item["date"], item["venue"], item["title"].casefold())
        if key in merged:
            merged[key]["times"] = sorted(set(merged[key]["times"] + item["times"]))
        else:
            merged[key] = dict(item, times=sorted(item["times"]))
    return list(merged.values())


FORMAT_NOTE = re.compile(r"\s*\([^)]*\b(digital|4k|35mm|70mm|16mm|imax|3d|dcp|restoration|restored)\b[^)]*\)", re.I)


def film_key(title):
    """A film's name as theaters' different spellings of it share: "Coyote vs. ACME" and "Coyote vs. Acme",
    "The Odyssey (Digital)" and "The Odyssey", "Tony (2026)" and "Tony". Only this and next year's dates go,
    which theaters add to new releases, so an older film of the same name stays apart."""
    year = datetime.now(BOSTON).year
    title = FORMAT_NOTE.sub("", title)
    title = re.sub(rf"\s*\(({year}|{year + 1})\)", "", title)
    return re.sub(r"\W+", " ", title).casefold().strip()


def combine_films(items):
    """A day's film playing at two or more places becomes one row, with each place's times inside it."""
    films = {}
    rows = []
    for item in items:
        if item["category"] == "film":
            films.setdefault(film_key(item["title"]), []).append(item)
        else:
            rows.append(item)
    for showings in films.values():
        if len({item["venue"] for item in showings}) < 2:
            rows += showings
            continue
        showings.sort(key=lambda item: item["times"][:1])
        rows.append({
            "showings": showings,
            # The plainest of the names: without a format note ("The Odyssey", not "The Odyssey (Digital)"),
            # then with the fewest capitals ("Coyote vs. Acme", not "Coyote vs. ACME"), the same every day.
            "title": min((item["title"] for item in showings), key=lambda title: (len(title), sum(map(str.isupper, title)))),
            "times": sorted(moment for item in showings for moment in item["times"])[:1],
            "category": "film",
            # The first theater's description that has one: most describe the film the same way.
            "about": next((item["about"] for item in showings if item.get("about")), []),
        })
    return rows


# Rendering.


def clock(moment):
    """8pm, 7:30pm."""
    hour = moment.hour % 12 or 12
    return f"{hour}{'' if moment.minute == 0 else f':{moment.minute:02d}'}{'am' if moment.hour < 12 else 'pm'}"


# Each row's mark for its category, in the category's color: two beamed eighth notes for music, a frame of
# film with sprocket holes down both sides for film, a framed picture of hills and a sun for art & talks. Drawn
# once in the page; rows point to the drawing.
ICON_DRAWINGS = {
    # A notice's mark: a circle with an exclamation point.
    "notice": '<circle cx="8" cy="8" r="6.25"/><path d="M8 4.75v3.5"/><circle cx="8" cy="11" r=".5" fill="currentColor"/>',
    "art": '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M4.5 11l2.75-3 2 2 1.25-1.25 1.5 2.25"/>'
           '<circle cx="10.5" cy="5.75" r="1" fill="currentColor"/>',
    "music": '<path d="M5.5 12.5V4l8-2v8.5"/><circle cx="3.75" cy="12.5" r="1.75" fill="currentColor"/>'
             '<circle cx="11.75" cy="10.5" r="1.75" fill="currentColor"/>',
    "film": '<rect x="2" y="2" width="12" height="12" rx="1.5"/>'
            '<path d="M5 2v12M11 2v12M2 5.5h3M2 10.5h3M11 5.5h3M11 10.5h3"/>',
}
ICON_SYMBOLS = shared.icon_symbols(ICON_DRAWINGS)


def icon(category, decorative=False):
    """A category's mark. Beside a label that already says it (the filter), it's hidden from screen readers."""
    return shared.icon(category, None if decorative else CATEGORIES[category])


def render_times(moments):
    # data-time lets the page drop today's showings once they've started. The commas between are their own,
    # so a time, which adds its showing to a calendar, lights up alone on hover.
    times = '<span class="sep">, </span>'.join(f'<time data-time="{moment:%H:%M}">{clock(moment)}</time>' for moment in moments)
    return f'<span class="times">{times}</span>' if times else ""


def written(address):
    """(street, town, ZIP) as one line, for a calendar event's location."""
    street, town, zip_code = address
    return f"{street}, {town}, MA {zip_code}".strip()


def address_attribute(item):
    """An event's own address, when the source gives one, for adding it to a calendar; otherwise the page's
    table of venues' addresses serves."""
    return f' data-address="{html.escape(written(item["address"]))}"' if item.get("address") else ""


def render_row(item):
    """Laid out like the newsfeed: the venue on the left, then the name with that day's times after it."""
    if "showings" in item:
        return render_combined(item)
    detail = f'<span class="detail">{html.escape(item["detail"])}</span>' if item["detail"] else ""
    return (
        f'<li class="row" data-category="{item["category"]}" data-sources="{html.escape(item["source"])}"{address_attribute(item)}>'
        f'<span class="source">{icon(item["category"])}'
        f'<span>{html.escape(item["venue"])}</span></span>'
        f'<div class="headline"><a class="title" href="{html.escape(item["link"])}">{html.escape(item["title"])}</a>'
        f'{render_times(item["times"])}{detail}{shared.preview(item["title"], item.get("about", []))}</div></li>'
    )


def render_combined(item):
    """A film at several places: one row reading "4 theaters … from 12:10pm" that opens to each place's times."""
    places = len({showing["venue"] for showing in item["showings"]})
    sources = "|".join(sorted({showing["source"] for showing in item["showings"]}))
    first = item["times"][0] if item["times"] else None
    start = f'<span class="times">from <time class="from" data-time="{first:%H:%M}">{clock(first)}</time></span>' if first else ""
    showings = "".join(
        f'<li data-source="{html.escape(showing["source"])}"{address_attribute(showing)}>'
        f'<a href="{html.escape(showing["link"])}">{html.escape(showing["venue"])}</a>'
        f'{render_times(showing["times"])}</li>'
        for showing in item["showings"]
    )
    return (
        f'<li class="row combined" data-category="{item["category"]}" data-sources="{html.escape(sources)}"><details><summary>'
        f'<span class="source">{icon(item["category"])}<span>{places} theaters</span></span>'
        f'<div class="headline"><span class="title">{html.escape(item["title"])}</span>'
        f'<span class="tail">{start}<span class="more" aria-hidden="true">›</span></span>'
        f'{shared.preview(item["title"], item["about"])}</div></summary>'
        f'<ul class="showings">{showings}</ul></details></li>'
    )


def render_index(events, sources, failed, stale, built_at, public=False, category=None, weekend=None, tonight=False):
    """The listings. For the public site, only its sources, with shorter previews; and with a category, its own
    page (music/, film/, talks/), holding only that kind's events, or with weekend (0 for this one, 1 for the
    next), only Friday to Sunday's, or tonight, only today's (and tomorrow's, for after midnight)."""
    root = PUBLIC_ROOT
    if public:
        shown = {source["name"] for source in sources if source.get("public", True)}
        events = [dict(item, about=shorter(item.get("about", []))) for item in events if item["source"] in shown]
    # The venue filter's choices: every venue with events coming up, whatever their kind (on a kind's page, a
    # choice goes to the home page, which has them all); on a weekend's or tonight's page, those with events then.
    venues = {item["source"] for item in events}
    if public:
        if category:
            events = [item for item in events if item["category"] == category]
            shown = {item["source"] for item in events}
        if weekend is not None:
            friday, sunday = weekend_days(built_at.astimezone(BOSTON).date(), weekend)
            events = [item for item in events if friday <= item["date"] <= sunday]
            shown = venues = {item["source"] for item in events}
        if tonight:
            day = built_at.astimezone(BOSTON).date()
            events = [item for item in events if day <= item["date"] <= day + timedelta(days=1)]
            shown = venues = {item["source"] for item in events}
        sources = [source for source in sources if source["name"] in shown]
        failed = [name for name in failed if name in shown]
        stale = [(name, fetched) for name, fetched in stale if name in shown]
    days = {}
    for item in events:
        days.setdefault(item["date"], []).append(item)

    sections = []
    for day in sorted(days):
        # Untimed listings first, then by time, then by name.
        rows = combine_films(days[day])
        rows.sort(key=lambda item: (bool(item["times"]), item["times"][:1], item["title"].casefold()))
        sections.append(
            f'<section class="day" data-date="{day.isoformat()}">'
            f'<h2><span class="relative"></span><span class="date">{day.strftime("%a, %b")} {day.day}</span></h2>\n'
            '<ul>\n' + "\n".join(render_row(item) for item in rows) + "\n</ul></section>"
        )

    buttons = '<button data-show="all">All</button>' + "".join(
        f'<button data-show="{key}">{icon(key, decorative=True)}{label}</button>' for key, label in CATEGORIES.items()
    )
    filter_attributes = ""
    if public and (weekend is not None or tonight):
        # Its buttons show a kind of the weekend's (or tonight's) events in place, not remembered; and the whole
        # weekend is on one page, its pager going between the weekends instead. Tonight's shows only today.
        filter_attributes = " data-here data-one-page" + " data-today" * tonight
    elif public:
        # Links to each kind's page, the current one marked; on the home page the script shows a kind in place.
        current = category or "all"
        links = [("all", root, "All")] + [(key, f"{root}{PUBLIC_PAGES[key][0]}/", f"{icon(key, decorative=True)}{label}")
                                                  for key, label in CATEGORIES.items()]
        marked = ' aria-current="page"'
        buttons = "".join(f'<a data-show="{key}" href="{href}"{marked if key == current else ""}>{label}</a>'
                          for key, href, label in links)
        filter_attributes = f' data-pages data-current="{current}"'
    names = ", ".join(html.escape(source["name"]) for source in sources)
    # Notices of sources that didn't load, marked as such, last in the footer.
    mark = shared.icon("notice")
    failed_note = f'<p class="notice">{mark}<span>Couldn’t load {html.escape(", ".join(failed))}.</span></p>\n' if failed else ""
    failed_note += "".join(
        f'<p class="notice">{mark}<span>Couldn’t reach {html.escape(name)}; its listings are from '
        f'<time class="ago" datetime="{fetched.isoformat()}"></time>.</span></p>\n'
        for name, fetched in stale
    )
    places = {item["venue"]: written(VENUE_ADDRESSES[item["venue"]]) for item in events if item["venue"] in VENUE_ADDRESSES}
    # Where this page is on the public site: a kind's page, a weekend's, or the home page.
    path = (PUBLIC_WEEKENDS[weekend][0] if weekend is not None else PUBLIC_TONIGHT[0] if tonight else
            f"{PUBLIC_PAGES[category][0]}/" if category else "")
    footer = (public_footer(path, failed_note, names) if public else
              f"<footer>\n<p>From {names}.</p>\n{failed_note}"
              f'<p><a href="{REPO_URL}/edit/main/events/sources.txt">Add a source</a></p>\n</footer>\n')
    others = ""
    if public and weekend is not None:
        # To the other weekend's page, with its dates.
        other = 1 - weekend
        days = weekend_span(*weekend_days(built_at.astimezone(BOSTON).date(), other))
        name, href = PUBLIC_WEEKENDS[other][1], root + PUBLIC_WEEKENDS[other][0]
        others = (f'<nav class="weekends"><span></span><a href="{href}">{name}, {days} →</a></nav>\n' if other else
                  f'<nav class="weekends"><a href="{href}">← {name}, {days}</a><span></span></nav>\n')
    body = (
        f'<nav class="filter" aria-label="Show"{filter_attributes}>{buttons}<span class="finders">{shared.menu(venues, "All venues", "Venue")}{shared.SEARCH}</span></nav>\n'
        + "\n".join(sections)
        + '\n<p class="empty" hidden>Nothing coming up.</p>\n<nav class="pager"></nav>\n' + others + footer
        # The address of each venue with events on this page, for adding one to a calendar.
        + f"<script>const PLACES = {json.dumps(places, ensure_ascii=False)};</script>\n"
        + f"<script>{INDEX_JS}</script>"
    )
    if public:
        title, description, tagline = PUBLIC_PAGES[category][1:] if category else (PUBLIC_TITLE, PUBLIC_DESCRIPTION, PUBLIC_TAGLINE)
        if weekend is not None:
            _, name, title, description = PUBLIC_WEEKENDS[weekend]
            tagline = (f"{name} around Boston, Cambridge, and Somerville: {friday:%A, %B} {friday.day} to "
                       f"{sunday:%A, %B} {sunday.day}.")
        if tonight:
            _, title, description, tagline = PUBLIC_TONIGHT
        return public_page(path, title, f'<h1 class="tagline">{tagline}</h1>\n' + body, built_at, description=description,
                           data={"@context": "https://schema.org", "@graph": [website_data()] + [event_data(item) for item in events]})
    return page("Events", body, built_at)


def website_data():
    return {"@type": "WebSite", "name": PUBLIC_NAME, "url": PUBLIC_URL, "description": PUBLIC_DESCRIPTION,
            "inLanguage": "en-US"}


SCHEMA_TYPES = {"music": "MusicEvent", "film": "ScreeningEvent", "art": "Event"}


def event_data(item):
    """A listing as schema.org Event data, which search engines read for their event listings: what, when,
    where (the venue's address, or the event's own), and the venue's page for it."""
    start = item["date"].isoformat()
    if item["times"]:
        start = datetime.combine(item["date"], item["times"][0], tzinfo=BOSTON).isoformat()
    street, town, zip_code = item.get("address") or VENUE_ADDRESSES.get(item["venue"]) or ("", "Boston", "")
    address = {"@type": "PostalAddress", "streetAddress": street, "addressLocality": town, "addressRegion": "MA",
               "postalCode": zip_code, "addressCountry": "US"}
    data = {
        "@type": SCHEMA_TYPES.get(item["category"], "Event"),
        "name": item["title"],
        "startDate": start,
        "url": item["link"],
        "eventStatus": "https://schema.org/EventScheduled",
        "location": {"@type": "Place", "name": item["venue"], "address": {k: v for k, v in address.items() if v}},
    }
    if item.get("about"):
        first = item["about"][0]
        data["description"] = first if len(first) <= 160 else first[:160].rsplit(" ", 1)[0] + "…"
    return data


def shorter(paragraphs):
    """A public preview: less of the venue's own words than this page shows, as an excerpt that sends
    readers to the venue for the rest."""
    kept, budget = [], PUBLIC_ABOUT_CHARS
    for paragraph in paragraphs[:2]:
        if len(paragraph) > budget:
            if budget > 60:
                kept.append(paragraph[:budget].rsplit(" ", 1)[0] + "…")
            break
        kept.append(paragraph)
        budget -= len(paragraph)
    return kept


def public_footer(path, notes="", names=""):
    """The foot of each of the public site's pages: the site's pages in three short lists (this one marked),
    and, under a list of events, where they come from, then any source it couldn't reach."""
    root = PUBLIC_ROOT
    groups = [
        ("Browse", [("All events", "")] + [(label, f"{PUBLIC_PAGES[key][0]}/") for key, label in CATEGORIES.items()]),
        ("When", [("Tonight", PUBLIC_TONIGHT[0])] + [(name, weekend_path) for weekend_path, name, *_ in PUBLIC_WEEKENDS]),
        (PUBLIC_NAME, [("About", "about.html"), ("Contact", "contact.html")]),
    ]
    marked = ' aria-current="page"'
    lists = "".join(
        f'<div><h2>{html.escape(heading)}</h2><ul>' + "".join(
            f'<li><a href="{root}{href}"{marked if href == path else ""}>{html.escape(label)}</a></li>' for label, href in links)
        + "</ul></div>"
        for heading, links in groups
    )
    where = f"<p>Listings from {names}, aggregated from their own calendars every few hours.</p>\n" if names else ""
    return f'<footer>\n<nav class="site-links" aria-label="{html.escape(PUBLIC_NAME)}">{lists}</nav>\n{where}{notes}</footer>\n'


def public_page(path, title, body, built_at=None, description=PUBLIC_DESCRIPTION, data=None):
    """A page of the public site, at path ("", "about.html", "film/"): its name, the way home, in the header,
    About and Contact in the footer; search engines welcome, told what the page is, where it lives, and (the
    listings) its events."""
    root = PUBLIC_ROOT
    links = [(PUBLIC_NAME, root, path == "")]
    if "<footer>" not in body:
        body += "\n" + public_footer(path)
    address = PUBLIC_URL + path
    head = "\n".join([
        f'<link rel="icon" href="{root}favicon.svg" type="image/svg+xml">',
        f'<link rel="canonical" href="{address}">',
        f'<meta name="description" content="{html.escape(description)}">',
        # What a link to it shows when shared.
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{html.escape(PUBLIC_NAME)}">',
        f'<meta property="og:title" content="{html.escape(title)}">',
        f'<meta property="og:description" content="{html.escape(description)}">',
        f'<meta property="og:url" content="{address}">',
        # Its card (events/share, made by share_cards.py): a kind's page its own, the rest the site's.
        f'<meta property="og:image" content="{PUBLIC_URL}share/{path.split("/")[0] if path.endswith("/") else "home"}.png">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:image:alt" content="{html.escape(PUBLIC_NAME)}: {html.escape(PUBLIC_TAGLINE)}">',
        '<meta name="twitter:card" content="summary_large_image">',
    ] + [f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False, separators=(",", ":"))}</script>'] * bool(data))
    return shared.page("events", title, body, css=CSS + PUBLIC_CSS, head=head, symbols=ICON_SYMBOLS,
                       updated=built_at, links=links, indexable=True)


def render_about(sources, built_at):
    """What the public page is, where its listings come from, and each venue by kind."""
    venues = {}
    for source in sources:
        if source.get("public", True):
            venues.setdefault(source["category"], []).append(source["name"])
    groups = "".join(
        f'<h2>{CATEGORIES[key]}</h2>\n<p>{html.escape(", ".join(sorted(set(venues[key]))))}</p>\n'
        for key in CATEGORIES if venues.get(key)
    )
    body = f"""<div class="prose">
<p>{PUBLIC_NAME} lists concerts for the next two months, and films, and art and talks for the next month, in
Boston, Cambridge and Somerville, on one page, grouped by day.</p>
<p>It’s gathered from select venues every few hours. Times and details can change, so check
with the venue before you go: every listing links to its page there. The descriptions are the venues’ own
words, in short.</p>
<p>No ads, no accounts, nothing to sign up for.</p>
{groups}
<p>Know a venue that should be here, or spotted a mistake? <a href="contact.html">Get in touch</a>.</p>
</div>"""
    return public_page("about.html", f"About · {PUBLIC_NAME}", body,
                       description=f"What {PUBLIC_NAME} is, and the Boston, Cambridge and Somerville venues it lists.")


def render_sitemap(built_at):
    """The public site's pages for search engines: the listings, changing every few hours, and the others."""
    pages = ([("", "hourly", "1.0"), (PUBLIC_TONIGHT[0], "hourly", "0.9")] + [(path, "hourly", "0.9") for path, *_ in PUBLIC_WEEKENDS] + [(f"{slug}/", "hourly", "0.9") for slug, *_ in PUBLIC_PAGES.values()]
             + [("about.html", "monthly", "0.5"), ("contact.html", "yearly", "0.3")])
    urls = "".join(
        f"  <url><loc>{PUBLIC_URL}{path}</loc><lastmod>{built_at:%Y-%m-%d}</lastmod>"
        f"<changefreq>{often}</changefreq><priority>{priority}</priority></url>\n"
        for path, often, priority in pages
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n'


def render_contact():
    """A form whose messages FormSubmit emails on; its first one asks the address's owner to confirm it."""
    body = f"""<div class="prose">
<p class="intro">A venue to add, a listing that’s wrong, or anything else: send a note.</p>
<form class="contact" action="https://formsubmit.co/{PUBLIC_CONTACT}" method="POST">
<input type="hidden" name="_subject" value="{PUBLIC_NAME}: a message">
<input type="hidden" name="_next" value="{PUBLIC_URL}contact.html?sent">
<input type="hidden" name="_template" value="box">
<input type="text" name="_honey" class="honey" tabindex="-1" autocomplete="off" aria-hidden="true">
<label>Your email, for a reply <input type="email" name="email" required></label>
<label>Message <textarea name="message" rows="6" required></textarea></label>
<button>Send</button>
</form>
<p class="sent" hidden>Thanks, your message is on its way. We’ll do our best to respond in a timely manner.</p>
</div>
<script>
  // Back from sending: only the thanks.
  if (new URLSearchParams(location.search).has("sent")) {{
    document.querySelector(".intro").hidden = true;
    document.querySelector(".contact").hidden = true;
    document.querySelector(".sent").hidden = false;
  }}
</script>"""
    return public_page("contact.html", f"Contact · {PUBLIC_NAME}", body,
                       description=f"Suggest a venue for {PUBLIC_NAME}, or tell us about a listing that’s wrong.")


INDEX_JS = """
  // Today and Tomorrow, from this device's clock, so an older build still reads right; days already
  // past are hidden until the next build drops them.
  const key = d => d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const today = key(new Date());
  const tomorrow = key(new Date(Date.now() + 86400000));
  const days = [...document.querySelectorAll(".day")];
  const todayOnly = document.querySelector(".filter").hasAttribute("data-today"); // The tonight page.
  for (const day of days) {
    if (day.dataset.date < today || (todayOnly && day.dataset.date !== today)) day.remove();
    else day.querySelector(".relative").textContent =
      day.dataset.date === today ? "Today" : day.dataset.date === tomorrow ? "Tomorrow" : "";
  }

  // Today lists only what's still to come: showings drop off once they've started, and an event goes
  // once its last one has. Events without a time stay all day.
  const now = new Date().toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" });
  const todays = document.querySelector(`.day[data-date="${today}"]`);
  if (todays) {
    for (const li of todays.querySelectorAll(":scope > ul > li")) {
      const times = li.querySelectorAll("time:not(.from)");
      for (const t of times) if (t.dataset.time < now) t.remove();
      // A comma no longer between two times goes too.
      for (const sep of li.querySelectorAll(".sep")) {
        if (sep.previousElementSibling?.tagName !== "TIME" || sep.nextElementSibling?.tagName !== "TIME") sep.remove();
      }
      // In a film at several places, a place with nothing left today goes too.
      for (const place of li.querySelectorAll(".showings li")) if (!place.querySelector("time")) place.remove();
      if (times.length && !li.querySelector("time:not(.from)")) { li.remove(); continue; }
      const from = li.querySelector("time.from");
      if (from) {
        const next = [...li.querySelectorAll(".showings time")].sort((a, b) => a.dataset.time < b.dataset.time ? -1 : 1)[0];
        from.dataset.time = next.dataset.time;
        from.textContent = next.textContent;
        const places = li.querySelectorAll(".showings li");
        li.querySelector(".source > span").textContent = places.length > 1 ? places.length + " theaters" : places[0].querySelector("a").textContent;
      }
    }
    // Reorder what's left by its next showing, as the build ordered everything by its first. Listings
    // without a time stay at the top.
    const next = el => (el.querySelector("time.from") || el.querySelector("time"))?.dataset.time || "";
    const byNext = (a, b) => next(a) < next(b) ? -1 : next(a) > next(b) ? 1 : 0;
    for (const list of todays.querySelectorAll(":scope > ul, .showings")) [...list.children].sort(byNext).forEach(el => list.append(el));
    if (!todays.querySelector("li")) todays.remove();
  }

  // The filter shows every category or just one. On the public site each is a page of its own (music/, film/,
  // talks/) its links go to; its home page, which has every event, shows one in place instead, and puts its
  // page's address in the address bar. Here, the choice is remembered in this browser. The search keeps the events
  // with every word typed somewhere in their name, place or description (accents aside), and is kept in the
  // address (?q=). A hundred of what's shown are on a page, however many days that takes; ?page=2 shows the
  // next hundred. A day split between two pages has its heading on both. A weekend's page shows all of it.
  const EVENTS_PER_PAGE = document.querySelector(".filter").hasAttribute("data-one-page") ? 100000 : 100; // A weekend, whole.
  const filter = document.querySelector(".filter");
  const search = document.querySelector(".search");
  const pager = document.querySelector(".pager");
  const empty = document.querySelector(".empty");
  const plain = text => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const allRows = [...document.querySelectorAll(".day > ul > li")];
  const searchable = new Map(allRows.map(li => [li, plain(li.textContent)]));
  const kindPages = filter.hasAttribute("data-pages");
  const everything = !kindPages || filter.dataset.current === "all";
  const remember = !kindPages && !filter.hasAttribute("data-here"); // The weekend page's choice isn't kept.
  let show = kindPages ? filter.dataset.current : "all";
  if (remember) try { show = localStorage.getItem("events-show") || "all"; } catch (error) {}
  // The venue menu shows one venue's events, of every kind, kept in the address too (?venue=).
  const venue = document.querySelector(".filter .pick");
  const combined = [...document.querySelectorAll(".combined")];
  const fromAddress = () => {
    const params = new URLSearchParams(location.search);
    search.value = params.get("q") || "";
    venue.value = params.get("venue") || "";
    if (venue.selectedIndex < 0) venue.value = ""; // A venue no longer listed.
  };
  fromAddress();
  const params = () => {
    const params = new URLSearchParams();
    if (search.offsetParent && search.value.trim()) params.set("q", search.value.trim());
    if (venue.value) params.set("venue", venue.value);
    return params;
  };
  const query = () => (params().toString() ? "?" + params() : "");
  for (const a of filter.querySelectorAll("a")) a.dataset.href = a.getAttribute("href");
  const address = page => {
    const kept = params();
    if (page > 1) kept.set("page", page);
    return kept.toString() ? "?" + kept : location.pathname;
  };
  // A film at several theaters, with a venue chosen, shows just that theater's times.
  function narrow() {
    for (const li of combined) {
      const places = [...li.querySelectorAll(".showings li")];
      for (const place of places) place.hidden = Boolean(venue.value) && place.dataset.source !== venue.value;
      const shown = places.filter(place => !place.hidden);
      if (!shown.length) continue;
      li.querySelector(".source > span").textContent = shown.length > 1 ? shown.length + " theaters" : shown[0].querySelector("a").textContent;
      const first = shown.flatMap(place => [...place.querySelectorAll("time")]).sort((a, b) => a.dataset.time < b.dataset.time ? -1 : 1)[0];
      const from = li.querySelector("time.from");
      if (first && from) { from.dataset.time = first.dataset.time; from.textContent = first.textContent; }
    }
  }
  function showEvents() {
    const words = plain(search.offsetParent ? search.value : "").split(/\\s+/).filter(Boolean);
    narrow();
    const rows = allRows.filter(li => (show === "all" || li.dataset.category === show)
      && (!venue.value || li.dataset.sources.split("|").includes(venue.value))
      && words.every(word => searchable.get(li).includes(word)));
    const pages = Math.max(1, Math.ceil(rows.length / EVENTS_PER_PAGE));
    const page = Math.min(pages, Math.max(1, parseInt(new URLSearchParams(location.search).get("page")) || 1));
    const onPage = new Set(rows.slice((page - 1) * EVENTS_PER_PAGE, page * EVENTS_PER_PAGE));
    for (const day of document.querySelectorAll(".day")) {
      const lis = day.querySelectorAll(":scope > ul > li");
      for (const li of lis) li.hidden = !onPage.has(li);
      day.hidden = ![...lis].some(li => !li.hidden);
    }
    const link = (n, text) => `<a href="${address(n)}">${text}</a>`;
    pager.innerHTML = pages < 2 ? "" :
      (page > 1 ? link(page - 1, "← Earlier") : "<span></span>") +
      `<span>Page ${page} of ${pages}</span>` +
      (page < pages ? link(page + 1, "Later →") : "<span></span>");
    const at = venue.value ? ` at ${venue.value}` : "";
    empty.textContent = words.length ? `Nothing coming up${at} matches “${search.value.trim()}”.` : `Nothing coming up${at}.`;
    venue.classList.toggle("chosen", Boolean(venue.value));
    empty.hidden = rows.length > 0;
    for (const b of filter.querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.show === show);
    for (const a of filter.querySelectorAll("a")) {
      if (a.dataset.show === show) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
      a.setAttribute("href", a.dataset.href + query()); // A search goes along to the other pages.
    }
    document.querySelector("main").classList.add("paged");
  }
  showEvents();
  filter.addEventListener("click", event => {
    const b = event.target.closest("[data-show]");
    // A kind's own page has only its events, so its links go to the others' pages; so does a click to open one
    // in a new tab.
    if (!b || !everything || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    show = b.dataset.show;
    if (kindPages) history.pushState(null, "", new URL(b.dataset.href, location.href).pathname + query());
    else {
      if (remember) try { localStorage.setItem("events-show", show); } catch (error) {}
      history.replaceState(null, "", address(1)); // Back to page one.
    }
    showEvents();
  });
  // Back and forward between the kinds (and venues) the home page has shown.
  if (kindPages && everything) addEventListener("popstate", () => {
    const here = [...filter.querySelectorAll("a")].find(a => new URL(a.dataset.href, location.href).pathname === location.pathname);
    show = here ? here.dataset.show : "all";
    fromAddress();
    showEvents();
  });
  // Choosing a venue shows all its events, whatever their kind. A kind's own page has only that kind's, so
  // there the choice goes to the home page, which has them all.
  venue.addEventListener("change", () => {
    const home = filter.querySelector('a[data-show="all"]');
    if (kindPages && !everything) { location.href = new URL(home.dataset.href, location.href).pathname + query(); return; }
    show = "all";
    if (remember) try { localStorage.setItem("events-show", show); } catch (error) {}
    if (kindPages) history.pushState(null, "", new URL(home.dataset.href, location.href).pathname + query());
    else history.replaceState(null, "", address(1));
    showEvents();
  });
  search.addEventListener("input", () => {
    history.replaceState(null, "", address(1));
    showEvents();
  });

  // A showtime adds that showing to a calendar: on an iPhone or iPad, Calendar's own sheet for adding it; on
  // Android, Google Calendar's page with it filled in; elsewhere a file any calendar app opens. It runs two
  // hours, since most listings give only when things start. The time looks just as before, until it's hovered.
  const pad = n => String(n).padStart(2, "0");
  const stamp = (day, time, hours = 0) => {
    const [h, m] = time.split(":").map(Number);
    const d = new Date(Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10), h + hours, m));
    return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}00`;
  };
  const calendarText = text => text.replace(/[\\\\;,]/g, "\\\\$&").replace(/\\n/g, "\\\\n");
  function showing(t) {
    const row = t.closest(".row");
    const place = t.closest(".showings li"); // One theater's times, in a film at several.
    const venue = place ? place.querySelector("a").textContent : row.querySelector(".source > span").textContent;
    const address = (place && place.dataset.address) || row.dataset.address || PLACES[venue] || "";
    const day = t.closest(".day").dataset.date;
    return {
      title: row.querySelector(".title").textContent.trim(),
      where: [venue, address].filter(Boolean).join(", "),
      link: (place ? place.querySelector("a") : row.querySelector("a.title")).href,
      start: stamp(day, t.dataset.time),
      end: stamp(day, t.dataset.time, 2),
    };
  }
  function addToCalendar(t) {
    const { title, where, link, start, end } = showing(t);
    if (/Android/i.test(navigator.userAgent)) {
      const details = { action: "TEMPLATE", text: title, dates: `${start}/${end}`, ctz: "America/New_York", location: where, details: link };
      open("https://calendar.google.com/calendar/render?" + new URLSearchParams(details), "_blank", "noopener");
      return;
    }
    const ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//" + location.hostname + "//EN", "BEGIN:VEVENT",
      `UID:${start}-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}@${location.hostname}`,
      "DTSTAMP:" + new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + "Z",
      `DTSTART;TZID=America/New_York:${start}`, `DTEND;TZID=America/New_York:${end}`,
      "SUMMARY:" + calendarText(title), "LOCATION:" + calendarText(where), "URL:" + link,
      "DESCRIPTION:" + calendarText(link), "END:VEVENT", "END:VCALENDAR"].join("\\r\\n");
    const apple = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    if (apple) { location.href = "data:text/calendar;charset=utf-8," + encodeURIComponent(ics); return; }
    const a = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(new Blob([ics], { type: "text/calendar" })),
      download: (title.replace(/[^\\w\\s-]+/g, "").trim().slice(0, 60) || "event") + ".ics",
    });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }
  const addable = "time[data-time]:not(.from)";
  for (const t of document.querySelectorAll(addable)) {
    t.tabIndex = 0;
    t.setAttribute("role", "button");
    t.title = "Add to calendar";
  }
  document.addEventListener("click", event => {
    const t = event.target.closest(addable);
    if (t) addToCalendar(t);
  });
  document.addEventListener("keydown", event => {
    const t = event.target.closest && event.target.closest(addable);
    if (t && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); addToCalendar(t); }
  });
"""


# The public site's About and Contact pages: plain text, and a form as quiet as the search.
PUBLIC_CSS = """
  /* What the site is, in a line under its name, as quiet as the rest. */
  .tagline { margin: -1rem 0 2.25rem; max-width: 34rem; color: #888; font-size: .9rem; font-weight: normal; line-height: 1.45; }
  .tagline + .filter { margin-top: 0; }
  /* A weekend page's way to the other weekend, where the pager goes on the others: next on the right, back on the left. */
  .weekends { display: flex; justify-content: space-between; margin-top: 2.5rem; color: #666; font-size: .8rem; }
  .weekends a, .weekends a:visited { color: #999; }
  .prose { max-width: 34rem; color: #ccc; }
  .prose p { margin: 0 0 1em; }
  .prose a { color: #fff; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  .prose h2 { margin-top: 2rem; }
  .contact { display: grid; gap: 1.1rem; margin-top: 1.5rem; }
  .contact label { display: grid; gap: .35rem; color: #888; font-size: .8rem; }
  .contact input, .contact textarea { padding: .5rem .6rem; border: 1px solid #333; border-radius: 4px; background: #0a0a0a;
                                      color: #fff; font: inherit; font-size: .95rem; }
  .contact input:focus, .contact textarea:focus { border-color: #777; outline: none; }
  .contact button { justify-self: start; padding: .45rem 1.2rem; border: 1px solid #555; border-radius: 999px;
                    background: none; color: #ddd; font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer;
                    transition: border-color .15s, color .15s, background-color .15s; }
  .contact button:hover, .contact button:focus-visible { border-color: #ccc; background: #151515; color: #fff; outline: none; }
  .contact button:active { background: #222; }
  .contact .honey { display: none; }
  /* The footer, set off from the page by a faint line. */
  footer { margin-top: 3.5rem; padding-top: 2.25rem; border-top: 1px solid rgba(255, 255, 255, .09); }
  /* The footer's lists of the site's pages: three short columns (two on a phone), under small, faint headings. */
  .site-links { display: grid; grid-template-columns: repeat(3, minmax(0, 11rem)); gap: 1.75rem 2.5rem; margin: .5rem 0 2rem; }
  .site-links h2 { margin: 0 0 .7rem; color: #555; font-size: .65rem; font-weight: 500; letter-spacing: .1em; }
  .site-links ul { display: grid; gap: .45rem; }
  footer .site-links a, footer .site-links a:visited { color: #999; font-size: .95rem; text-decoration: none; }
  footer .site-links a:hover { color: #fff; }
  footer .site-links a[aria-current="page"] { color: #fff; }
  @media (max-width: 34rem) { .site-links { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
"""


# The events page's own styles, on top of the ones it shares with the newsfeed (shared/site.py).
CSS = """
  /* Category colors, for the marks in the list and on the filter: violet music, amber film, blue art & talks. */
  :root { --music: #a78bfa; --film: #fbbf24; --art: #60a5fa; }
  .icon { color: var(--dot); }
  [data-show="music"], [data-category="music"] { --dot: var(--music); }
  [data-show="film"], [data-category="film"] { --dot: var(--film); }
  [data-show="art"], [data-category="art"] { --dot: var(--art); }
  h2 { margin: 2.25rem 0 .5rem; color: #777; font-size: .75rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
  /* Until the script picks the page, show the first two days, about a page, so the whole month never flashes up. */
  main:not(.paged) .day:nth-of-type(n+3) { display: none; }
  .relative:not(:empty) { color: #fff; margin-right: .6em; }
  /* A source that didn't load, at the foot of the page, marked by a small circled exclamation point. */
  .notice { display: flex; align-items: baseline; gap: .45em; }
  .notice .icon { flex: none; align-self: center; width: 11px; height: 11px; color: #777; }
  .times, .detail { margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }
  .times { flex: none; }
  /* A showtime adds that showing to a calendar: a pointer and a lighter shade on hover say so, nothing else. */
  .times time[data-time]:not(.from) { cursor: pointer; }
  .times time[data-time]:not(.from):hover, .times time[data-time]:not(.from):focus-visible { color: #ddd; outline: none; }
  .detail { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  /* A film at several places: the row opens to each place's times, with a › that turns when it's open. */
  .row.combined { display: block; }
  .combined summary { display: grid; grid-template-columns: 10rem 1fr; gap: 1.25rem; align-items: baseline;
                      list-style: none; cursor: pointer; }
  .combined summary::-webkit-details-marker { display: none; }
  .combined summary:hover .title { text-decoration: underline; }
  /* A faint dotted underline on a name with a description to preview, turning solid on hover; not on phones,
     which have no previews. */
  @media (hover: hover) and (min-width: 34.01rem) {
    .headline:has(> .preview:not(.title-only)) > .title {
      text-decoration: underline dotted #3a3a3a; text-decoration-thickness: 1px; text-underline-offset: .32em; }
    .headline:has(> .preview:not(.title-only)) > .title:hover,
    .combined summary:hover .headline:has(> .preview:not(.title-only)) > .title { text-decoration: underline; }
  }
  /* The first time and the ›, together, so a narrow screen never leaves the › alone on a line. */
  .tail, .tail .times { flex: none; white-space: nowrap; }
  .more { display: inline-block; margin-left: .5em; color: #666; font-size: .8em; transition: transform .15s; }
  .combined details[open] .more { transform: rotate(90deg); }
  .showings { margin: .35rem 0 .2rem calc(10rem + 1.25rem); }
  .showings li { display: flex; align-items: baseline; gap: .6em; padding: .15rem 0; font-size: .9em; }
  .showings a { flex: none; color: #ccc; }
  .showings .times { margin-left: 0; white-space: normal; }
  @media (max-width: 34rem) {
    .combined summary { grid-template-columns: 1fr; gap: 0; }
    .showings { margin-left: 0; }
    .times, .detail { white-space: normal; }
  }
"""


def page(title, body, built_at):
    head = '<link rel="icon" href="favicon.svg" type="image/svg+xml">'
    return shared.page("events", title, body, css=CSS, head=head, symbols=ICON_SYMBOLS, updated=built_at)


def saved(item):
    """A listing as listings.json keeps it."""
    return {
        "title": item["title"],
        "date": item["date"].isoformat(),
        "times": [moment.strftime("%H:%M") for moment in item["times"]],
        "link": item["link"],
        "detail": item["detail"],
        "venue": item["venue"],
        "category": item["category"],
        "about": item.get("about", []),
        "address": item.get("address"),
    }


def restored(kept, source):
    return dict(
        kept,
        date=date.fromisoformat(kept["date"]),
        times=[datetime.strptime(moment, "%H:%M").time() for moment in kept["times"]],
        source=source["name"],
    )


def previous_build():
    """The live page's listings.json: what it was built from, and which sources were failing."""
    return shared.previous_build(LISTINGS_URL, USER_AGENT)


def gather(results, previous, built_at):
    """Each source's upcoming listings: fresh when it loaded, else its last good ones from the previous build
    if they're recent enough. Returns the events; the sources that failed with nothing to fall back on; the
    ones shown from before, with when; the listings to save for next time; and why each failing one failed."""
    today = built_at.astimezone(BOSTON).date()
    def ahead(item):  # Soon enough to list: two months for a concert, a month for the rest.
        return today <= item["date"] <= today + timedelta(days=days_ahead(item["category"]))
    kept_sources = previous.get("sources", {})
    events, failed, stale, listings, errors = [], [], [], {}, {}
    for source, found, error in results:
        fetched = built_at
        kept = kept_sources.get(source["name"])
        if not error and not any(ahead(item) for item in found):
            # A source that had listings coming up last time and has none now is more likely broken for the
            # moment (a feed served empty) than suddenly without shows, so it gets the same fallback.
            if kept and any(date.fromisoformat(item["date"]) >= today for item in kept["events"]):
                error = "returned no listings"
        if error:
            errors[source["name"]] = str(error)
            print(f"✗ {source['name']}: {error}", file=sys.stderr)
            if not kept or built_at - datetime.fromisoformat(kept["fetched"]) > FALLBACK_LIMIT:
                failed.append(source["name"])
                continue
            # Keep its last good listings, and when they were fetched, so they still age out.
            fetched = datetime.fromisoformat(kept["fetched"])
            found = [restored(item, source) for item in kept["events"]]
            stale.append((source["name"], fetched))
            print(f"  {source['name']}: showing its listings from {kept['fetched']} instead", file=sys.stderr)
        upcoming = [item for item in found if ahead(item)]
        if fetched == built_at:
            print(f"✓ {source['name']}: {len(upcoming)} coming up ({len(found)} listed)")
        listings[source["name"]] = {"fetched": fetched.isoformat(), "events": [saved(item) for item in upcoming]}
        events += upcoming
    return events, failed, stale, listings, errors


still_failing = shared.still_failing


def due(previous, built_at):
    """Whether it's time for a new build. The workflow runs every 15 minutes, for the newsfeed; listings change
    slowly, and the theaters' sites are small, so on those runs the events page rebuilds only every few hours
    (EVENTS_EVERY_HOURS). A push sets it to 0, to build at once."""
    every = float(os.environ.get("EVENTS_EVERY_HOURS") or 0)
    if not every or not previous.get("built"):
        return True
    # A few minutes' grace, so a build that ran slightly early doesn't push the next one back a whole cycle.
    return built_at - datetime.fromisoformat(previous["built"]) >= timedelta(hours=every) - timedelta(minutes=10)


def main():
    built_at = datetime.now(timezone.utc)
    previous = previous_build()
    if not due(previous, built_at):
        print(f"Built at {previous['built']}; not due yet, so not rebuilding.")
        return

    sources = read_sources()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(load, sources))

    events, failed, stale, listings, errors = gather(results, previous, built_at)
    if len(failed) + len(stale) == len(sources):
        sys.exit("No source loaded — not writing the page.")

    # Here, not in the readers, so it covers a source's listings from before too, and skip.txt takes effect at once.
    # Not films, whose names are a work's title ("God's Comedy"), not what kind of event it is.
    skip = skipping()
    events = merge_showings([item for item in events if not (skip and item["category"] != "film" and skip.search(item["title"]))])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    (OUT_DIR / "index.html").write_text(render_index(events, sources, failed, stale, built_at))
    record = {"built": built_at.isoformat(), "sources": listings, "failing": still_failing(errors, previous, built_at)}
    (OUT_DIR / "listings.json").write_text(json.dumps(record, ensure_ascii=False))
    print(f"Wrote {OUT_DIR.relative_to(ROOT.parent)}/index.html with {len(events)} listings, and listings.json")

    # The public site, from the same listings.
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", PUBLIC_DIR, dirs_exist_ok=True)
    shutil.copytree(ROOT / "share", PUBLIC_DIR / "share", dirs_exist_ok=True)
    (PUBLIC_DIR / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True))
    (PUBLIC_DIR / "about.html").write_text(render_about(sources, built_at))
    (PUBLIC_DIR / "contact.html").write_text(render_contact())
    for category, (slug, *_) in PUBLIC_PAGES.items():
        (PUBLIC_DIR / slug).mkdir(exist_ok=True)
        (PUBLIC_DIR / slug / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True, category=category))
    (PUBLIC_DIR / PUBLIC_TONIGHT[0]).mkdir(parents=True, exist_ok=True)
    (PUBLIC_DIR / PUBLIC_TONIGHT[0] / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True, tonight=True))
    for ahead, (path, *_) in enumerate(PUBLIC_WEEKENDS):
        (PUBLIC_DIR / path).mkdir(parents=True, exist_ok=True)
        (PUBLIC_DIR / path / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True, weekend=ahead))
    (PUBLIC_DIR / "sitemap.xml").write_text(render_sitemap(built_at))
    (PUBLIC_DIR / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {PUBLIC_URL}sitemap.xml\n")
    print(f"Wrote {PUBLIC_DIR.relative_to(ROOT.parent)}: index.html, {', '.join(slug + '/' for slug, *_ in PUBLIC_PAGES.values())}, tonight/, weekend/, weekend/next/, "
          "about.html, contact.html, sitemap.xml and robots.txt")


if __name__ == "__main__":
    main()
