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
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from shared import site as shared

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.txt"
OUT_DIR = ROOT.parent / "dist" / "events"
REPO_URL = "https://github.com/grahamhagenah/news"
DAYS_AHEAD = 30  # How far ahead the page lists events.
# Each build publishes its listings beside the page; a source that fails next time falls back to its copy
# there, if it's no older than this.
LISTINGS_URL = "https://events.grahamhagenah.com/listings.json"
FALLBACK_LIMIT = timedelta(days=2)
BOSTON = ZoneInfo("America/New_York")
USER_AGENT = "Mozilla/5.0 (compatible; events-feed/1.0)"
CATEGORIES = {"music": "Music", "film": "Film", "art": "Art & talks"}

# The same listings, for anyone: its own name, About and Contact pages, shorter previews, and only the
# sources fine to republish (a line in sources.txt ending in public=no stays on this page only).
PUBLIC_NAME = "Boston, Weekly"
PUBLIC_DIR = ROOT.parent / "dist" / "public"
PUBLIC_URL = "https://grahamhagenah.github.io/boston-weekly/"
PUBLIC_CONTACT = "gwhagenah@gmail.com"  # Where the contact form's messages go, through FormSubmit.
PUBLIC_ABOUT_CHARS = 240  # Of the venue's own words in a preview.


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


_fetched, _fetching = {}, threading.Lock()


def fetch(url, attempts=3, timeout=20):
    """The page or data at url, as text. Each address is downloaded once a build, however many sources read
    it (Boston Film Hub's page, for two theaters); a #fragment, which isn't sent, doesn't make it another."""
    address = urlsplit(url)._replace(fragment="").geturl()
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


def event(source, title, day, start=None, link="", detail="", venue="", about=()):
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
    }


ABOUT_CHARS = 400  # Roughly how much of what a source says about an event its preview shows.
# Short lines worth keeping, like "Doors 7pm", "21+" and "$10 cover": the rest are headings, names and labels.
FACT = re.compile(r"\d|\$|\b(free|ages?|doors?|cover|cash|sold out|cancel\w*|postponed|tickets?)\b", re.I)
NOT_ABOUT = re.compile(r"(buy|get)? ?tickets?( here| now)?|more info(rmation)?|learn more|rsvp( here)?|register( here)?", re.I)
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


def read_rss(source):
    """AXS venue feeds (The Sinclair) give the show date only at the end of the title: "… on Sep 12, 2026"."""
    events = []
    for item in re.findall(r"<item>(.*?)</item>", fetch(source["url"]), re.S):
        title = text(re.search(r"<title>(.*?)</title>", item, re.S).group(1))
        link = text((re.search(r"<link>(.*?)</link>", item, re.S) or re.search(r"()", "")).group(1))
        match = re.fullmatch(r"(.*) on ([A-Z][a-z]{2} \d{1,2}, \d{4})", title)
        description = re.search(r"<description>(.*?)</description>", item, re.S)
        if match:
            events.append(event(source, match.group(1), datetime.strptime(match.group(2), "%b %d, %Y").date(), link=link,
                                about=about(html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", description.group(1))) if description else "")))
    return events


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
    """Pages that describe their screenings or shows as schema.org Events (the Brattle's Coming Soon page).
    A page listing several places' events (Boston Film Hub's) can be kept to one of them with #venue=<its name>
    at the end of the URL, which isn't sent to the site; the listings then show that place's name."""
    place = parse_qs(urlsplit(source["url"]).fragment).get("venue", [""])[0]
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
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        if place and location.get("name") != place:
            continue
        day, start = at_boston(datetime.fromisoformat(item["startDate"]))
        # The Brattle adds the showtime to each name: "Filipiñana - 9/12/26 @ 12:00 pm".
        title = re.sub(r"\s+-\s+\d{1,2}/\d{1,2}/\d{2,4}\s+@.*$", "", html.unescape(item.get("name", "")))
        work = item.get("workPresented") or {}
        work = works.get(work.get("@id"), work) if isinstance(work, dict) else {}
        events.append(event(source, title, day, start if "T" in item["startDate"] else None, link=item.get("url", ""),
                            about=jsonld_about(item, work), venue=place))
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
                        about=ica_about(link) if day <= window_end() else [])
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
        "to": f"{today + timedelta(days=DAYS_AHEAD + 1)}T03:00:00",
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


def read_ics(source):
    """iCalendar feeds. Only each event's first date; repeating events aren't expanded."""
    def unescape(value):
        return re.sub(r"\\([,;\\])", r"\1", value).replace("\\n", " ").strip()

    lines = re.sub(r"\r?\n[ \t]", "", fetch(source["url"])).splitlines()  # Undo line folding.
    events, fields = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            fields = {}
        elif line == "END:VEVENT" and fields is not None:
            # Each field is (parameters, value), as in DTSTART;TZID=America/New_York:20260919T120000.
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
            venue = unescape(fields.get("LOCATION", ("", ""))[1]).split(",")[0]
            summary = unescape(fields.get("SUMMARY", ("", ""))[1])
            description = re.sub(r"\\([,;\\])", r"\1", fields.get("DESCRIPTION", ("", ""))[1]).replace("\\n", "\n\n")
            events.append(event(source, summary, day, start, link=fields.get("URL", ("", ""))[1], venue=venue,
                                about=about(html.escape(description))))
            fields = None
        elif fields is not None and ":" in line:
            name, value = line.split(":", 1)
            key, _, params = name.partition(";")
            fields.setdefault(key, (params, value))
    return events


# Calendar entries that aren't shows: Lizard Lounge's placeholders for its dark nights, and its weekly poetry jam.
NOT_SHOWS = re.compile(r"^No Event Tonight|Poetry Jam", re.I)


def read_tribe(source):
    """WordPress sites using The Events Calendar (Lizard Lounge, The Rockwell), through its REST API. The URL
    can pick a category, like ?categories=music for The Rockwell's music among its comedy and theater."""
    today = datetime.now(BOSTON).date()
    window = {"start_date": today.isoformat(), "end_date": f"{today + timedelta(days=DAYS_AHEAD)} 23:59:59", "per_page": 50}
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


def window_end():
    return today() + timedelta(days=DAYS_AHEAD)


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
            events.append(event(source, item["title"], day, start, link=link, about=described))
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
    end = window_end()
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
            last = date.fromisoformat((field("bc:end_date_local") or day.isoformat())[:10])
            if "Exhibitions" in tags and last > day:
                events += [dict(listing, venue=venue) for listing in opening(source, title, day, last, link, about=described)]
            else:
                events.append(event(source, title, day, start, link=link, venue=venue, about=described))
        if not items or day > end:
            break
    return events


# The MFA's programs, from the list of upcoming ones on each kind's own page, and where each kind goes; the
# rest (guided tours, studio classes, courses, member hours) aren't read at all.
MFA_SECTIONS = [("lectures", "art"), ("special-event", "art"), ("film", "film"), ("music", "music")]


def read_mfa(source):
    """The MFA's lectures, special events, films and concerts, 25 to a page, soonest first. A program spanning
    several days (a course, a festival) is a heading over its own dated programs, so only single days are kept."""
    end = window_end()
    events = []
    for section, category in MFA_SECTIONS:
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
    months = sorted({(day.year, day.month) for day in (today(), window_end())})
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
    "rss": read_rss,
    "ticketweb": read_ticketweb,
    "jsonld": read_jsonld,
    "coolidge": read_coolidge,
    "ticketmaster": read_ticketmaster,
    "alamo": read_alamo,
    "landmark": read_landmark,
    "ics": read_ics,
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
    # data-time lets the page drop today's showings once they've started.
    times = "".join(f'<time data-time="{moment:%H:%M}">{clock(moment)}</time>' for moment in moments)
    return f'<span class="times">{times}</span>' if times else ""


def render_row(item):
    """Laid out like the newsfeed: the venue on the left, then the name with that day's times after it."""
    if "showings" in item:
        return render_combined(item)
    detail = f'<span class="detail">{html.escape(item["detail"])}</span>' if item["detail"] else ""
    return (
        f'<li class="row" data-category="{item["category"]}"><span class="source">{icon(item["category"])}'
        f'<span>{html.escape(item["venue"])}</span></span>'
        f'<div class="headline"><a class="title" href="{html.escape(item["link"])}">{html.escape(item["title"])}</a>'
        f'{render_times(item["times"])}{detail}{shared.preview(item["title"], item.get("about", []))}</div></li>'
    )


def render_combined(item):
    """A film at several places: one row reading "4 theaters … from 12:10pm" that opens to each place's times."""
    places = len({showing["venue"] for showing in item["showings"]})
    first = item["times"][0] if item["times"] else None
    start = f'<span class="times">from <time class="from" data-time="{first:%H:%M}">{clock(first)}</time></span>' if first else ""
    showings = "".join(
        f'<li><a href="{html.escape(showing["link"])}">{html.escape(showing["venue"])}</a>{render_times(showing["times"])}</li>'
        for showing in item["showings"]
    )
    return (
        f'<li class="row combined" data-category="{item["category"]}"><details><summary>'
        f'<span class="source">{icon(item["category"])}<span>{places} theaters</span></span>'
        f'<div class="headline"><span class="title">{html.escape(item["title"])}</span>{start}'
        f'<span class="more" aria-hidden="true">›</span>{shared.preview(item["title"], item["about"])}</div></summary>'
        f'<ul class="showings">{showings}</ul></details></li>'
    )


def render_index(events, sources, failed, stale, built_at, public=False):
    if public:
        shown = {source["name"] for source in sources if source.get("public", True)}
        sources = [source for source in sources if source["name"] in shown]
        events = [dict(item, about=shorter(item.get("about", []))) for item in events if item["source"] in shown]
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
    names = ", ".join(html.escape(source["name"]) for source in sources)
    failed_note = f"<p>Couldn’t load {html.escape(', '.join(failed))}.</p>\n" if failed else ""
    failed_note += "".join(
        f'<p>Couldn’t reach {html.escape(name)}; its listings are from <time class="ago" datetime="{fetched.isoformat()}"></time>.</p>\n'
        for name, fetched in stale
    )
    more = ("<p>Listings come from each venue’s own calendar, every few hours.</p>\n" + PUBLIC_LINKS
            if public else f'<p><a href="{REPO_URL}/edit/main/events/sources.txt">Add a source</a></p>\n')
    body = (
        f'<nav class="filter" aria-label="Show">{buttons}{shared.SEARCH}</nav>\n'
        + "\n".join(sections)
        + '\n<p class="empty" hidden>Nothing coming up.</p>\n<nav class="pager"></nav>\n'
        f"<footer>\n{failed_note}<p>From {names}.</p>\n{more}</footer>\n"
        f"<script>{INDEX_JS}</script>"
    )
    if public:
        return public_page("", PUBLIC_NAME, body, built_at)
    return page("Events", body, built_at)


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


# About and Contact, at the foot of each of the public site's pages.
PUBLIC_LINKS = '<p><a href="about.html">About</a> · <a href="contact.html">Contact</a></p>\n'


def public_page(current, title, body, built_at=None):
    """A page of the public site: its name, the way home, in the header, About and Contact in the footer;
    search engines welcome."""
    links = [(PUBLIC_NAME, "./", current == "")]
    if "<footer>" not in body:
        body += f"\n<footer>\n{PUBLIC_LINKS}</footer>"
    head = ('<link rel="icon" href="favicon.svg" type="image/svg+xml">\n'
            '<meta name="description" content="Concerts, films, and art and talks in Boston, Cambridge and '
            'Somerville over the next month, from each venue’s own calendar.">')
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
<p>{PUBLIC_NAME} lists concerts, films, and art and talks in Boston, Cambridge and Somerville for the next
month, on one page, grouped by day.</p>
<p>It’s gathered from each venue’s own calendar every few hours. Times and details can change, so check
with the venue before you go: every listing links to its page there. The descriptions are the venues’ own
words, in short.</p>
<p>No ads, no accounts, nothing to sign up for.</p>
{groups}
<p>Know a venue that should be here, or spotted a mistake? <a href="contact.html">Get in touch</a>.</p>
</div>"""
    return public_page("about", f"About · {PUBLIC_NAME}", body)


def render_contact():
    """A form whose messages FormSubmit emails on; its first one asks the address's owner to confirm it."""
    body = f"""<div class="prose">
<p>A venue to add, a listing that’s wrong, or anything else: send a note.</p>
<form class="contact" action="https://formsubmit.co/{PUBLIC_CONTACT}" method="POST">
<input type="hidden" name="_subject" value="{PUBLIC_NAME}: a message">
<input type="hidden" name="_next" value="{PUBLIC_URL}contact.html?sent">
<input type="hidden" name="_template" value="box">
<input type="text" name="_honey" class="honey" tabindex="-1" autocomplete="off" aria-hidden="true">
<label>Your email, for a reply <input type="email" name="email" required></label>
<label>Message <textarea name="message" rows="6" required></textarea></label>
<button>Send</button>
</form>
<p class="sent" hidden>Thanks, your message is on its way.</p>
</div>
<script>
  if (new URLSearchParams(location.search).has("sent")) {{
    document.querySelector(".contact").hidden = true;
    document.querySelector(".sent").hidden = false;
  }}
</script>"""
    return public_page("contact", f"Contact · {PUBLIC_NAME}", body)


INDEX_JS = """
  // Today and Tomorrow, from this device's clock, so an older build still reads right; days already
  // past are hidden until the next build drops them.
  const key = d => d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const today = key(new Date());
  const tomorrow = key(new Date(Date.now() + 86400000));
  const days = [...document.querySelectorAll(".day")];
  for (const day of days) {
    if (day.dataset.date < today) day.remove();
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

  // The filter shows every category or just one, and is remembered in this browser. The search keeps the events
  // with every word typed somewhere in their name, place or description (accents aside), and is kept in the
  // address (?q=). Fifty of what's shown are on a page, however many days that takes; ?page=2 shows the next
  // fifty. A day split between two pages has its heading on both.
  const EVENTS_PER_PAGE = 50;
  const filter = document.querySelector(".filter");
  const search = document.querySelector(".search");
  const pager = document.querySelector(".pager");
  const empty = document.querySelector(".empty");
  const plain = text => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const allRows = [...document.querySelectorAll(".day > ul > li")];
  const searchable = new Map(allRows.map(li => [li, plain(li.textContent)]));
  let show = "all";
  try { show = localStorage.getItem("events-show") || "all"; } catch (error) {}
  search.value = new URLSearchParams(location.search).get("q") || "";
  const address = page => {
    const params = new URLSearchParams();
    if (search.offsetParent && search.value.trim()) params.set("q", search.value.trim());
    if (page > 1) params.set("page", page);
    return params.toString() ? "?" + params : location.pathname;
  };
  function showEvents() {
    const words = plain(search.offsetParent ? search.value : "").split(/\s+/).filter(Boolean); // Hidden on phones.
    const rows = allRows.filter(li => (show === "all" || li.dataset.category === show)
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
    empty.textContent = words.length ? `Nothing coming up matches “${search.value.trim()}”.` : "Nothing coming up.";
    empty.hidden = rows.length > 0;
    for (const b of filter.querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.show === show);
    document.querySelector("main").classList.add("paged");
  }
  showEvents();
  filter.addEventListener("click", event => {
    const b = event.target.closest("button");
    if (!b) return;
    show = b.dataset.show;
    try { localStorage.setItem("events-show", show); } catch (error) {}
    history.replaceState(null, "", address(1)); // Back to page one.
    showEvents();
  });
  search.addEventListener("input", () => {
    history.replaceState(null, "", address(1));
    showEvents();
  });
"""


# The public site's About and Contact pages: plain text, and a form as quiet as the search.
PUBLIC_CSS = """
  .prose { max-width: 34rem; color: #ccc; }
  .prose p { margin: 0 0 1em; }
  .prose a { color: #fff; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  .prose h2 { margin-top: 2rem; }
  .contact { display: grid; gap: 1.1rem; margin-top: 1.5rem; }
  .contact label { display: grid; gap: .35rem; color: #888; font-size: .8rem; }
  .contact input, .contact textarea { padding: .5rem .6rem; border: 1px solid #333; border-radius: 4px; background: #0a0a0a;
                                      color: #fff; font: inherit; font-size: .95rem; }
  .contact input:focus, .contact textarea:focus { border-color: #777; outline: none; }
  .contact button { justify-self: start; padding: .45rem 1.1rem; border: 0; border-radius: 999px; background: #fff;
                    color: #000; font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer; }
  .contact .honey { display: none; }
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
  .times, .detail { margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }
  .times { flex: none; }
  .times time + time::before { content: ", "; }
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
  .more { flex: none; display: inline-block; margin-left: .5em; color: #666; font-size: .8em; transition: transform .15s; }
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
    last_day = today + timedelta(days=DAYS_AHEAD)
    kept_sources = previous.get("sources", {})
    events, failed, stale, listings, errors = [], [], [], {}, {}
    for source, found, error in results:
        fetched = built_at
        kept = kept_sources.get(source["name"])
        if not error and not any(today <= item["date"] <= last_day for item in found):
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
        upcoming = [item for item in found if today <= item["date"] <= last_day]
        if fetched == built_at:
            print(f"✓ {source['name']}: {len(upcoming)} in the next {DAYS_AHEAD} days ({len(found)} listed)")
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

    events = merge_showings(events)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    (OUT_DIR / "index.html").write_text(render_index(events, sources, failed, stale, built_at))
    record = {"built": built_at.isoformat(), "sources": listings, "failing": still_failing(errors, previous, built_at)}
    (OUT_DIR / "listings.json").write_text(json.dumps(record, ensure_ascii=False))
    print(f"Wrote {OUT_DIR.relative_to(ROOT.parent)}/index.html with {len(events)} listings, and listings.json")

    # The public site, from the same listings.
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", PUBLIC_DIR, dirs_exist_ok=True)
    (PUBLIC_DIR / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True))
    (PUBLIC_DIR / "about.html").write_text(render_about(sources, built_at))
    (PUBLIC_DIR / "contact.html").write_text(render_contact())
    print(f"Wrote {PUBLIC_DIR.relative_to(ROOT.parent)}: index.html, about.html and contact.html")


if __name__ == "__main__":
    main()
