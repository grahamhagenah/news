#!/usr/bin/env python3
"""Fetch every source in sources.txt and write the coming weeks' Boston-area events to dist/events. Run from
the repo's top folder: python3 -m events.build"""

import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from shared import site as shared

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.txt"  # Boston's; each city has its own (CITIES).
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
# sources fine to republish (a line in sources.txt ending in public=no stays on this page only). Pushpin has
# a site for each city (CITIES); these are Boston's, and use_city() points them at another's while its pages
# are written.
PUBLIC_NAME = "Pushpin Boston"
# Pushpin's site, at pushpin.city, with each city's listings at a path of their own: Boston's at /boston/.
# PUBLIC_DIR is the whole site, as published; the city's pages are in its boston/ folder.
PUBLIC_SITE = "https://pushpin.city/"
PUBLIC_URL = PUBLIC_SITE + "boston/"
PUBLIC_DIR = ROOT.parent / "dist" / "public"
# Its pages link to each other from the site's root, since the home page puts a kind's page's address in the
# address bar without loading it, and a relative link would then go astray.
PUBLIC_ROOT = urlsplit(PUBLIC_URL).path
CITY_DIR = PUBLIC_DIR / PUBLIC_ROOT.strip("/")
# Where Pushpin Boston's visits are counted: GoatCounter, which uses no cookies and keeps nothing personal.
# The personal events page isn't counted.
GOATCOUNTER = "https://hagenah.goatcounter.com/count"
PUBLIC_CONTACT = "gwhagenah@gmail.com"  # Where the contact form's messages go, through FormSubmit.
PUBLIC_ABOUT_CHARS = 240  # Of the venue's own words in a preview.
PUBLIC_TITLE = f"{PUBLIC_NAME} · Concerts, films and talks around Boston"
PUBLIC_TAGLINE = "Concerts, films, and talks around Boston, Cambridge, and Somerville, aggregated from select venues."
PUBLIC_AROUND = "Boston, Cambridge, and Somerville"  # Where it covers, as its pages say: "… around Boston, Cambridge, and Somerville".
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
    # Western Mass.
    "Mass MoCA": ("1040 Mass MoCA Way", "North Adams", "01247"),
    "Amherst Cinema": ("28 Amity St", "Amherst", "01002"),
    "Images Cinema": ("50 Spring St", "Williamstown", "01267"),
    "Triplex Cinema": ("70 Railroad St", "Great Barrington", "01230"),
}


def postal(line):
    """An address written out, "134 Memorial Dr, Cambridge, MA 02139", as (street, town, ZIP); None otherwise."""
    found = re.match(r"\s*(\d[^,]*?),\s*([A-Za-z .]+?),?\s+(?:MA|Massachusetts)\b\.?\s*(\d{5})?", line or "")
    return (found.group(1).strip(), found.group(2).strip().title(), found.group(3) or "") if found else None


def read_sources(path=None, city="boston"):
    """A city's sources file's lines (sources.txt, Boston's): how to read the source, its URL, a category, a
    name, and public=no for a source kept off the public page."""
    sources = []
    for line in (path or SOURCES_FILE).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            kind, url, category, *words = line.split()
            name = [word for word in words if word != "public=no"]
            sources.append({"kind": kind, "url": url, "category": category, "name": " ".join(name),
                            "public": len(name) == len(words), "city": city})
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


# What a listing costs and who it's for, as its view shows them up top: "$18–$20", "Free"; "All ages", "18+", "21+".
# A line that says the event itself is free ("Free", "Free admission", "This talk is free"), not something at it
# ("Free pizza", "18 and under free", "Free for members").
FREE = re.compile(r"^free\b(?! (with|for|to members|pizza|food|drinks|parking))|^no cover\b|\bfree (admission|entry|event|show|concert|"
                  r"screening|to attend|and open)\b|\badmission( is)?:? free\b|\bthis (event|program|show|screening|talk|"
                  r"lecture|concert) is free\b", re.I)
# An amount, or a range of them ("$5-20"), not a sum raised ("$5 million") or the ages after it ("$10-21+").
PRICE = re.compile(r"\$\s?(\d{1,4}(?:\.\d{2})?)(?:\s*[-–]\s*\$?(\d{1,4}(?:\.\d{2})?)(?![\d.+]))?(?![\d,]*\s*(?:million|billion|k\b))", re.I)
NOT_PRICE = re.compile(r"donat|proceeds|raised|from (every|each)\b", re.I)  # "$4 from every ticket goes to …"
AGES = re.compile(r"\b(all[ -]ages|(18|21)\s*(?:\+|and over|& over|and up|and older|or older))", re.I)
# Ages as a venue states them in its description, the one place prose is read for them: "Ages: This event is 21+".
AGES_STATED = re.compile(r"\bage(?:s| limit| restriction)?:\s*(?:this (?:event|show) is\s*)?(all[ -]ages|18\+|21\+)", re.I)
FACT_LINE_CHARS = 140  # A line this short is a line of facts ("$15 / $10 students · seated"); a longer one, prose.


def money(amount):
    """15.5 as $15.50, 18.0 as $18."""
    amount = float(amount)
    return f"${amount:.2f}" if amount % 1 else f"${amount:.0f}"


def price_of(low, high=None):
    """A price, or a range of them: $18, $18–$20; none for nothing, or for $0, which some sources give
    for a price they don't have."""
    amounts = sorted({float(amount) for amount in (low, high) if amount not in (None, "") and float(amount) > 0})
    return "–".join(money(amount) for amount in (amounts[:1] + amounts[1:][-1:])) if amounts else ""


def price_from(text_):
    """A price in a line of text: every amount in it as a range ("$15 / $10 students" is $10–$15), or Free
    when it says so and has none."""
    parts = [part.strip() for part in re.split(r"\s[·|–-]\s|\n", text_ or "") if not NOT_PRICE.search(part)]
    amounts = [amount for part in parts for found in PRICE.findall(part) for amount in found if amount]
    if amounts:
        return price_of(min(amounts, key=float), max(amounts, key=float))
    return "Free" if any(FREE.search(part) for part in parts) else ""


def ages_from(text_):
    """Who it's for, as a line of text says: All ages, 18+ or 21+."""
    found = AGES.search(text_ or "")
    if not found:
        return ""
    return f"{found.group(2)}+" if found.group(2) else "All ages"


def image_of(value):
    """A schema.org image: a URL, a list of them, or an ImageObject."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or ""
    return value if isinstance(value, str) and value.startswith("http") else ""


# "Sold out" in a listing's name, as some venues mark one ("Yana – SOLD OUT!", "SOLD OUT: …"): taken out of the
# name, and the listing marked.
SOLD_OUT_NAME = re.compile(r"\s*(?:[-–—|:]\s*|\(\s*)sold[ -]?out!*\s*\)?\s*$|^\s*sold[ -]?out!*\s*[-–—|:]\s*", re.I)


def sold_times(item):
    """Which of a listing's times ("19:15") are sold out: all of them, for one sold out altogether."""
    times = {f"{moment:%H:%M}" for moment in item["times"]}
    return times if item.get("sold_out") else times & set(item.get("sold_out_times") or [])


def is_sold_out(item):
    """A listing with nothing left: marked so, or every one of its times sold out."""
    return bool(item.get("sold_out") or (item["times"] and len(sold_times(item)) == len(set(item["times"]))))


def event(source, title, day, start=None, link="", detail="", venue="", about=(), address=None, image="", price="",
          ages="", sold_out=False):
    """One listing. start is a time of day in Boston, or None when the source gives only the date; about is
    what the source says about it, a few short paragraphs for its preview. image, price and ages when the source
    gives them; otherwise a price and ages from its short lines of facts ("$10 cover · 21+"), not its prose,
    where a "$1 from every ticket" isn't one."""
    facts = " · ".join([detail] + [paragraph for paragraph in list(about)[:3] if len(paragraph) <= FACT_LINE_CHARS])
    named_sold_out = bool(SOLD_OUT_NAME.search(title))
    title = SOLD_OUT_NAME.sub("", title).strip() or title
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
        "image": image or "",
        "price": price or price_from(facts),
        "ages": ages_from(ages) or ages_from(facts) or ages_from(" ".join(AGES_STATED.findall(" ".join(about)))),
        # Sold out altogether, as the source says; or only some of its times ("19:15"), as a theater's do.
        "sold_out": bool(sold_out or named_sold_out),
        "sold_out_times": [],
    }


ABOUT_CHARS = 400  # Roughly how much of what a source says about an event its preview shows.
ABOUT_FULL_CHARS = 2400  # And how much is kept, for a listing's own view, which can show all of it.
# Short lines worth keeping, like "Doors 7pm", "21+" and "$10 cover": the rest are headings, names and labels.
FACT = re.compile(r"\d|\$|\b(free|ages?|doors?|cover|cash|sold out|cancel\w*|postponed|tickets?)\b", re.I)
NOT_ABOUT = re.compile(r"(buy|get)? ?(your )?tickets?( here| now)?|more info(rmation)?|learn more|rsvp( here)?|register( here)?", re.I)
LINKS = re.compile(r"https?://\S+|\b[\w-]+(\.[\w-]+)*\.(com|net|org|io|co|fm|me|us|bandcamp\.com)\b\S*", re.I)


def about(markup):
    """What a source says about an event, as its preview's paragraphs: its sentences, with short lines of facts
    ("Doors 7pm", "21+", "$10 cover") run together into one, and a sentence split by a line break made whole.
    Links, headings and "Buy tickets" are left out. All of it, up to ABOUT_FULL_CHARS; a preview shows less
    (clip)."""
    kept = []
    for paragraph in shared.excerpt(markup, ABOUT_FULL_CHARS * 2, min_words=1, max_paragraphs=24):
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
    return clip([paragraph for paragraph, _ in kept], ABOUT_FULL_CHARS, 12)


def clip(paragraphs, budget, most=3, least=0):
    """The first few paragraphs, cut to about budget characters, the last one ending "…" where it's cut, unless
    less than least would be left of it."""
    kept = []
    for paragraph in paragraphs[:most]:
        if len(paragraph) > budget:
            if budget > least:
                kept.append(paragraph[:budget].rsplit(" ", 1)[0] + "…")
            break
        kept.append(paragraph)
        budget -= len(paragraph)
    return kept


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
            image=aeg_image(item.get("relatedMedia")),
            price=price_from(f"{item.get('ticketPriceLow') or ''} {item.get('ticketPriceHigh') or ''}"),  # "$0" for none.
            ages=item.get("age") or "",
            sold_out=((item.get("ticketing") or {}).get("status") or "").casefold() == "sold out",
        ))
    return events


def aeg_image(media):
    """The widest of an AEG listing's pictures up to 800 pixels (its 678 by 399, usually); they come in a
    dozen sizes and shapes, down to thumbnails."""
    pictures = [picture for picture in (media or {}).values() if isinstance(picture, dict) and picture.get("file_name")]
    width = lambda picture: int(picture.get("width") or 0) if str(picture.get("width") or 0).isdigit() else 0
    fitting = [picture for picture in pictures if width(picture) <= 800] or pictures
    return max(fitting, key=width)["file_name"] if fitting else ""


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
        button = field(r'class="btn-tickets[^"]*"[^>]*>(.*?)</a>').casefold()
        if not name or not day or button == "cancelled":
            continue
        doors = re.search(r"\d{1,2}:\d{2} [AP]M", field(r'<span class="time">.*?</span>(.*?)</span>'))
        start = datetime.strptime(doors.group(), "%I:%M %p").time() if doors else None
        support = field(r'class="supporting[^"]*">(.*?)</h4>')
        age = field(r'<span class="age">(.*?)</span>')
        facts = [field(r'<h5 class="tour">(.*?)</h5>'), f"Doors {clock(start)}" if start else "", age]
        picture = re.search(r'<img[^>]+src="(https?://[^"]+)"', entry)
        events.append(event(
            source,
            text(name.group(2)),
            datetime.strptime(day.group(), "%b %d, %Y").date(),
            start,
            link=html.unescape(name.group(1)),
            detail=f"with {support}" if support else "",
            about=[" · ".join(fact for fact in facts if fact)],
            image=html.unescape(picture.group(1)) if picture else "",
            ages=age,
            sold_out=button == "sold out",
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
        picture = re.search(r'<img[^>]+class="event-img[^"]*"[^>]+src="([^"]+)"', section)
        events.append(event(
            source,
            html.unescape(title),
            day,
            datetime.strptime(clock, "%I:%M %p").time(),
            link=html.unescape(link),
            # "@ Middle East - Zuzu": just the venue, not the room; Sonia, next door, stays Sonia.
            venue=text(room.group(1)).lstrip("@ ").split(" - ")[0] if room else "",
            image=html.unescape(picture.group(1)).replace("_Original.", "_Edp.") if picture else "",  # 800 pixels, not 3000.
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

    page = fetch(source["url"])
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            collect(json.loads(block))
        except ValueError:
            continue

    # DICE says which of its shows are sold out only in its page's own data, by name.
    sold = {json.loads(f'"{name}"') for name in re.findall(r'"name":"((?:[^"\\]|\\.)*)","status":"sold-out"', page)}
    events = []
    for item in found:
        if str(item.get("eventStatus", "")).endswith("EventCancelled"):
            continue
        day, start = at_boston(datetime.fromisoformat(item["startDate"]))
        # The Brattle adds the showtime to each name: "Filipiñana - 9/12/26 @ 12:00 pm".
        title = re.sub(r"\s+-\s+\d{1,2}/\d{1,2}/\d{2,4}\s+@.*$", "", html.unescape(item.get("name", "")))
        work = item.get("workPresented") or {}
        work = works.get(work.get("@id"), work) if isinstance(work, dict) else {}
        offers = item.get("offers") or {}
        offers = offers[0] if isinstance(offers, list) and offers else offers if isinstance(offers, dict) else {}
        sold_out = str(offers.get("availability", "")).endswith("SoldOut") or html.unescape(item.get("name", "")) in sold
        events.append(event(source, title, day, start if "T" in item["startDate"] else None, link=item.get("url", ""),
                            about=jsonld_about(item, work), image=lighter(image_of(item.get("image")) or image_of(work.get("image")), page),
                            price=price_of(offers.get("lowPrice") or offers.get("price"), offers.get("highPrice")), sold_out=sold_out))
    return events


def lighter(image, page=""):
    """A smaller copy of a picture where its host makes one, rather than the full size schema.org gives (up to
    2560 pixels and a megabyte or two): Ticketmaster's 1024-pixel one, 800 pixels from imgix, and from a
    WordPress site, the size nearest 800 pixels wide its page uses (poster-2-768x1138.jpg for poster-2-scaled.jpg)."""
    if "ticketm.net/" in image:
        return image.replace("_TABLET_LANDSCAPE_LARGE_16_9", "_TABLET_LANDSCAPE_16_9")
    if ".imgix.net/" in image and not re.search(r"[?&]w=", image):
        return image + ("&" if "?" in image else "?") + "w=800"
    stem = re.match(r"(.+?)(?:-scaled|-\d+x\d+)?\.(jpe?g|png|webp)$", image, re.I)
    if stem and page:
        sizes = {(int(width), found) for found, width in re.findall(rf"({re.escape(stem.group(1))}-(\d+)x\d+\.{stem.group(2)})", page)}
        fitting = [size for size in sizes if 600 <= size[0] <= 1100]
        if fitting:
            return min(fitting, key=lambda size: abs(size[0] - 800))[1]
    return image


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
            picture = re.search(r'<img[^>]+src="([^"]+)"', card)
            listing = event(source, html.unescape(film.group(1)), day, link=f"https://coolidge.org{film.group(2)}",
                            about=about(blurb.group(1) if blurb else "") + facts,
                            image=urljoin("https://coolidge.org/", html.unescape(picture.group(1))) if picture else "")
            listing["times"] = [coolidge_clock(clock) for clock in re.findall(r'class="showtime-ticket__time">([^<]+)<', card)]
            listing["sold_out_times"] = sorted(f"{coolidge_clock(clock):%H:%M}" for state, clock in COOLIDGE_SHOWTIME.findall(card)
                                               if state in COOLIDGE_SOLD_OUT)
            events.append(listing)
    # The showtimes pages can be an hour behind a film's own, which says a showing's sold out as soon as it is.
    films = sorted({listing["link"] for listing in events})
    with ThreadPoolExecutor(max_workers=4) as pool:
        states = dict(zip(films, pool.map(coolidge_film_states, films)))
    for listing in events:
        known = states.get(listing["link"]) or {}
        listing["sold_out_times"] = sorted(
            clock for clock in (f"{moment:%H:%M}" for moment in listing["times"])
            if (known[listing["date"], clock] in COOLIDGE_SOLD_OUT if (listing["date"], clock) in known else clock in listing["sold_out_times"]))
    return events


# A Coolidge showtime and its state: DuringSales on sale, AfterSalesBeforeEvent no longer sold online, and
# DisplayCustomMessage what its site's key calls "Sold out/unavailable".
COOLIDGE_SHOWTIME = re.compile(r'sales-state--(\w+)"[^>]*>\s*<a[^>]*>\s*<span class="showtime-ticket">\s*'
                               r'<span class="showtime-ticket__time">([^<]+)<')
COOLIDGE_SOLD_OUT = {"DisplayCustomMessage", "SoldOut"}


def coolidge_clock(text_):
    return datetime.strptime(text_.strip().upper(), "%I:%M%p").time()


def coolidge_film_states(link):
    """Each showtime's state on a Coolidge film's own page, by (date, "19:15"): its days, each "Tue 9/15" and
    no year. Nothing, if the page doesn't load; the showtimes pages' states stand then."""
    try:
        page = fetch(link, attempts=1, timeout=10)
    except Exception:
        return {}
    today_, states = today(), {}
    for block in page.split('class="film-showtime-list"')[1:]:
        day = re.search(r'class="datepicker__date">(\d{1,2})/(\d{1,2})<', block)
        if not day:
            continue
        when = date(today_.year, int(day.group(1)), int(day.group(2)))
        if when < today_ - timedelta(days=60):  # January's, in December.
            when = when.replace(year=today_.year + 1)
        for state, clock in COOLIDGE_SHOWTIME.findall(block):
            states[when, f"{coolidge_clock(clock):%H:%M}"] = state
    return states


def read_alamo(source):
    """Alamo Drafthouse loads a market's whole schedule (Boston: the Seaport) from one JSON file."""
    data = json.loads(fetch(source["url"]))["data"]
    titles = {item["slug"]: (item.get("show") or {}).get("title") for item in data["presentations"]}
    headlines = {item["slug"]: (item.get("show") or {}).get("headline") or "" for item in data["presentations"]}
    pictures = {item["slug"]: alamo_image(item.get("show") or {}) for item in data["presentations"]}
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
                            about=about(headlines.get(session["presentationSlug"])), image=pictures.get(session["presentationSlug"], "")))
    return events


def alamo_image(show):
    """A film's wide still, asked for at 800 by 450 rather than the 1920 by 1080 it comes as; else its poster."""
    wide = (show.get("landscapeHeroImage") or {}).get("uri") or ""
    if wide:
        return re.sub(r"\bh=\d+", "h=450", re.sub(r"\bw=\d+", "w=800", wide))
    posters = show.get("posterImages") or []
    return (posters[0].get("uri") or "") if posters and isinstance(posters[0], dict) else ""


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
        picture = re.search(r'<img[^>]+src="([^"]+)"', block)
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
            image=urljoin(source["url"], html.unescape(picture.group(1))) if picture else "",
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
        picture = re.search(r'<img[^>]+src="([^"]+)"', node)
        # Its own page for what it's about, only when it's soon enough to be listed.
        listing = event(source, name, day, clock_range_start(clock_text), link=link,
                        about=ica_about(link) if day <= window_end(category) else [],
                        image=html.unescape(picture.group(1)) if picture else "")
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
        picture = re.search(r'<img[^>]+src="([^"]+)"', card)
        listing = event(source, name, datetime.strptime(day.group(1), "%B %d, %Y").date(), start,
                        link=html.unescape(title.group(1)), about=about(excerpt.group(1) if excerpt else ""),
                        image=html.unescape(picture.group(1)) if picture else "")
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
        picture = film.get("imageHorizontalUrl") or film.get("imageVerticalUrl") or ""
        for session in film.get("sessionTimes") or []:
            clock_text = re.fullmatch(r"(\d{1,2}):(\d{2}) ?([ap])m", (session.get("time") or "").strip(), re.I)
            if not session.get("date") or not clock_text:
                continue
            hour = int(clock_text.group(1)) % 12 + (12 if clock_text.group(3).lower() == "p" else 0)
            events.append(event(source, film["title"], date.fromisoformat(session["date"][:10]),
                                datetime.min.time().replace(hour=hour, minute=int(clock_text.group(2))),
                                link=urljoin(source["url"], f"/movie/{film['url']}"), about=described, image=picture))
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
    # Its picture: a wide still, else its poster; and what it's about, with its length (in seconds there).
    pictures = {film["id"]: next(iter(film.get("heroImages") or []), "")
                or next((still.get("url") for still in film.get("images") or [] if isinstance(still, dict) and still.get("url")), "")
                or film.get("poster") or "" for film in films}
    described = {film["id"]: about(film.get("synopsis") or "") + (
        [f"{film['runtime'] // 3600}h {film['runtime'] % 3600 // 60}m"] if isinstance(film.get("runtime"), int) and film["runtime"] >= 60 else [])
        for film in films}
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
                events.append(event(source, titles[film], start.date(), start.time(), link=tickets,
                                    about=described.get(film, []), image=pictures.get(film, "")))
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
        prices = [band for band in item.get("priceRanges") or [] if isinstance(band, dict)]
        listing = event(source, item["name"], date.fromisoformat(start["localDate"]), clock, link=item.get("url", ""),
                        about=about(notes), image=ticketmaster_image(item.get("images")),
                        price=price_of(min((band.get("min") or 0 for band in prices), default=None),
                                       max((band.get("max") or 0 for band in prices), default=None)))
        listing["category"] = TICKETMASTER_SEGMENTS.get(segment, source["category"])
        events.append(listing)
    return events


def ticketmaster_image(images):
    """A show's wide picture about 640 to 1024 pixels across, of the dozen sizes Ticketmaster gives."""
    images = [image for image in images or [] if isinstance(image, dict) and image.get("url")]
    wide = [image for image in images if image.get("ratio") == "16_9"] or images
    fitting = [image for image in wide if 600 <= (image.get("width") or 0) <= 1100] or wide
    return min(fitting, key=lambda image: abs((image.get("width") or 0) - 800))["url"] if fitting else ""


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
            params, value = fields.get("ATTACH", ("", ""))
            picture = value if value.startswith("http") and ("image/" in params or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", value, re.I)) else ""
            listing = event(source, summary, day, start, link=fields.get("URL", ("", ""))[1], venue=venue,
                            about=about(html.escape(html.unescape(description))), address=postal(place), image=picture)
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
        picture = item.get("assetUrl") or ""
        events.append(event(source, title, day, start, link=urljoin(source["url"], item["fullUrl"]), about=about(described),
                            image=f"{picture}?format=750w" if picture.startswith("http") else ""))
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
        picture = field("imageurl")
        events.append(event(source, title, date.fromisoformat(day), start, link=field("link"), detail=field("detail"),
                            image=urljoin("https://www.passim.org/", picture) if picture else "",
                            sold_out='class="tickets sold-out"' in show))
    return events


# Calendar entries that aren't shows: Lizard Lounge's placeholders for its dark nights, and its weekly poetry jam.
NOT_SHOWS = re.compile(r"^No Event Tonight|Poetry Jam", re.I)


def tribe_title(markup):
    """An Events Calendar event's name, and any subtitle its site sets in a lighter span after it (Mass MoCA's
    "Madison Cunningham <span class="title-light">Ace Tour</span>"), or before it ("FreshGrass Presents |"),
    as its detail. The span is sometimes left open at the end."""
    light = r'<span class="title-light[^"]*">(.*?)(?:</span>|$)'
    subtitle = " · ".join(filter(None, (text(part).strip(" |") for part in re.findall(light, markup, re.S))))
    return text(re.sub(light, " ", markup, flags=re.S)), subtitle


def read_tribe(source, kinds=None):
    """WordPress sites using The Events Calendar (Lizard Lounge, The Rockwell), through its REST API. The URL
    can pick a category, like ?categories=music for The Rockwell's music among its comedy and theater. With
    kinds, a map from the site's categories to ours, each event goes in the first of its categories there, and
    one in none of them is left out; and one spanning days, an exhibition, is listed on the day it opens."""
    today = datetime.now(BOSTON).date()
    end = today + timedelta(days=days_ahead("music" if kinds else source["category"]))
    window = {"start_date": today.isoformat(), "end_date": f"{end} 23:59:59", "per_page": 50}
    url = source["url"] + ("&" if "?" in source["url"] else "?") + urlencode(window)
    events = []
    while url:
        data = json.loads(fetch(url))
        for item in data.get("events", []):
            title, subtitle = tribe_title(item["title"])
            if item.get("hide_from_listings") or NOT_SHOWS.search(title):
                continue
            tags = [html.unescape(tag.get("name", "")) for tag in item.get("categories") or []]
            category = next((kinds[tag] for tag in tags if tag in kinds), None) if kinds else source["category"]
            if not category:
                continue
            # The venue's own local time, which for these is Boston's.
            moment = datetime.strptime(item["start_date"], "%Y-%m-%d %H:%M:%S")
            last = datetime.strptime(item["end_date"], "%Y-%m-%d %H:%M:%S").date() if item.get("end_date") else moment.date()
            picture = item.get("image") if isinstance(item.get("image"), dict) else {}
            described = about(item.get("description") or item.get("excerpt"))
            image = next((size["url"] for name in ("medium_large", "large") for size in [(picture.get("sizes") or {}).get(name) or {}]
                          if size.get("url")), picture.get("url") or "")
            if kinds and last - moment.date() > timedelta(days=1):
                found = opening(source, title, moment.date(), last, item.get("url", ""), about=described, image=image)
            else:
                found = [event(source, title, moment.date(), None if item.get("all_day") else moment.time(),
                               link=item.get("url", ""), detail=subtitle, about=described, image=image,
                               price=price_from(text(str(item.get("cost") or ""))))]
            events += [dict(listing, category=category) for listing in found]
        url = data.get("next_rest_url")  # 50 to a page.
    return events


# Mass MoCA's kinds of event, from its calendar's categories, and where each goes: its concerts, and its talks,
# readings, performances and exhibition openings; not its events for kids, or its long-running exhibitions,
# which opened years ago.
MASS_MOCA_KINDS = {"Concert": "music", "Film": "film", "Book Talk": "art", "Artist Talk": "art", "Dance": "art",
                   "Theater": "art", "Performing Arts": "art", "Performance": "art", "Exhibition": "art", "Public Program": "art"}


def read_mass_moca(source):
    """Mass MoCA's calendar (The Events Calendar), sorted into our kinds by its own; not Kidspace's."""
    return [listing for listing in read_tribe(source, MASS_MOCA_KINDS) if not re.search(r"\bstorytime\b|\bkidspace\b", listing["title"], re.I)]


def read_amherst_cinema(source):
    """Amherst Cinema's calendar, a page for each day (/calendar/month/2026-09-16, despite its name), each
    film with its series and its times; and each film's own page, for its picture and what it's about."""
    days = [today() + timedelta(days=n) for n in range(DAYS_AHEAD + 1)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda day: amherst_day(source["url"], day), days))
    events = []
    for day, page in zip(days, pages):
        for row in page.split('<div class="views-row">')[1:]:
            film = re.search(r'<div class="title"><a href="([^"]+)">(.*?)</a>', row, re.S)
            if not film:
                continue
            series = re.search(r'<div class="series"><a[^>]*>(.*?)</a>', row, re.S)
            times = [datetime.strptime(clock_text.strip().upper(), "%I:%M %p").time()
                     for clock_text in re.findall(r'class="date-display-single">([^<]+)<', row)]
            listing = event(source, text(film.group(2)), day, link=urljoin(source["url"], html.unescape(film.group(1))),
                            detail=text(series.group(1)) if series else "")
            listing["times"] = times
            events.append(listing)
    films = sorted({listing["link"] for listing in events})
    with ThreadPoolExecutor(max_workers=4) as pool:
        details = dict(zip(films, pool.map(amherst_film, films)))
    return [dict(listing, **details.get(listing["link"], {})) for listing in events]


def amherst_day(url, day):
    """One day's page of Amherst Cinema's calendar; nothing, if it doesn't load, not the rest of its days."""
    try:
        return fetch(urljoin(url, f"/calendar/month/{day.isoformat()}"), attempts=2, timeout=15)
    except Exception:
        return ""


def amherst_film(link):
    """A film's picture and what it's about (who directed it, its rating), from its own page at Amherst Cinema."""
    try:
        page = fetch(link, attempts=1, timeout=15)
    except Exception:
        return {}
    picture = re.search(r'<img[^>]+src="([^"]*/styles/field_image_front/[^"]+)"', page)

    def field(name):
        found = re.search(rf'field-name-field-{name}\b[^>]*>(.*?)</div>\s*</div>', page, re.S)
        return text(found.group(1)) if found else ""
    body = re.search(r'field-name-body\b[^>]*>(.*?)</div>\s*</div>\s*</div>', page, re.S)
    facts = " · ".join(filter(None, [field("director").replace("\xa0", " "), field("rating")]))
    # Not its notes for particular days ("On Tuesday 9/15, the 2:10 showtime is presented with Open Captions",
    # "Last day Thursday 9/17"), which read wrong on the rest.
    described = [line for line in about(body.group(1) if body else "") if not re.match(r"(On \w+ \d{1,2}/\d{1,2}|Last day)\b", line)]
    return {"image": html.unescape(picture.group(1)) if picture else "", "about": described + ([facts] if facts else [])}


def post_json(url, body, headers=None):
    """What a JSON API answers a POST with (a GraphQL query), as data."""
    import urllib.request
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


INDY_SHOWINGS = """query ($date: String, $siteIds: [ID]) { showingsForDate(date: $date, siteIds: $siteIds) {
  data { time published private seatsRemaining movie { name urlSlug bannerImage posterImage synopsis directedBy duration rating } } } }"""
INDY_DATES = "query ($siteIds: [ID]) { datesWithShowing(siteIds: $siteIds) { value } }"


def read_indy(source):
    """Cinemas whose sites run on Indy Systems (Images Cinema, the Triplex), through the API their pages use,
    which answers only for the theater its site and circuit say (in the URL: ?site=57&circuit=49). Each day
    with showings ahead, each with its film, its picture and what it's about; a showing with no seats left is
    sold out."""
    address = urlsplit(source["url"])
    ids = dict(pair.split("=", 1) for pair in address.query.split("&") if "=" in pair)
    api = f"{address.scheme}://{address.netloc}/graphql"
    headers = {"client-type": "consumer", "site-id": ids["site"], "circuit-id": ids["circuit"]}
    dates = json.loads(post_json(api, {"query": INDY_DATES, "variables": {"siteIds": [ids["site"]]}}, headers)
                       ["data"]["datesWithShowing"]["value"])
    days = [day for day in map(date.fromisoformat, dates) if today() <= day <= window_end("film")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda day: post_json(api, {"query": INDY_SHOWINGS, "variables": {
            "date": day.isoformat(), "siteIds": [ids["site"]]}}, headers), days))
    events = []
    for answer in answers:
        for showing in (answer.get("data") or {}).get("showingsForDate", {}).get("data") or []:
            film = showing.get("movie") or {}
            if not showing.get("published") or showing.get("private") or not film.get("name"):
                continue
            day, start = at_boston(datetime.fromisoformat(showing["time"].replace("Z", "+00:00")))
            minutes = film.get("duration") or 0
            facts = " · ".join(filter(None, [f"Directed by {film['directedBy']}" if film.get("directedBy") else "",
                                             f"{minutes // 60}h {minutes % 60}m" if minutes else "", film.get("rating") or ""]))
            # Its wide picture at 800 by 450, or else its poster, whole.
            picture = (f"https://indy-systems.imgix.net/{film['bannerImage']}?w=800&h=450&fit=crop&auto=format,compress" if film.get("bannerImage")
                       else f"https://indy-systems.imgix.net/{film['posterImage']}?w=600&auto=format,compress" if film.get("posterImage") else "")
            listing = event(source, film["name"], day, start, link=f"{address.scheme}://{address.netloc}/movie/{film.get('urlSlug', '')}",
                            about=about(film.get("synopsis") or "") + ([facts] if facts else []), image=picture,
                            sold_out=showing.get("seatsRemaining") == 0)
            events.append(listing)
    return events


def today():
    return datetime.now(BOSTON).date()


def days_ahead(category):
    """How many days ahead a kind of event is listed: two months for concerts, a month for the rest."""
    return MUSIC_DAYS_AHEAD if category == "music" else DAYS_AHEAD


def window_end(category=None):
    """The last day a kind of event is listed; without one, the rest's."""
    return today() + timedelta(days=days_ahead(category))


def opening(source, title, first, last, link, start=None, about=(), image=""):
    """An exhibition, listed once, on the day it opens, with when it closes; one already open is left out, so it
    isn't at the top of every day for months."""
    if first < today():
        return []
    return [event(source, title, first, start, link=link, detail=f"through {last:%b} {last.day}", about=about, image=image)]


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
            picture = item.get("photo_url") or ""
            first, last = date.fromisoformat(item["first_date"]), date.fromisoformat(item["last_date"])
            if "Exhibits" in types and last > first:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    events += opening(source, item["title"], first, last, link, about=described, image=picture)
                continue
            events.append(event(source, item["title"], day, start, link=link, about=described, address=postal(item.get("address")),
                                image=picture, price=price_from(item.get("ticket_cost") or ("Free" if item.get("free") else ""))))
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
            picture = re.search(r'<enclosure url="([^"]+)"[^>]*type="image/', item)
            picture = html.unescape(picture.group(1)).replace("http://", "https://", 1) if picture else ""
            street = " ".join(part for part in (field("bc:number"), field("bc:street")) if part)
            place = (street, field("bc:city") or "Boston", field("bc:zip")) if street else None
            last = date.fromisoformat((field("bc:end_date_local") or day.isoformat())[:10])
            if "Exhibitions" in tags and last > day:
                events += [dict(listing, venue=venue) for listing in opening(source, title, day, last, link, about=described, image=picture)]
            else:
                events.append(event(source, title, day, start, link=link, venue=venue, about=described, address=place, image=picture))
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
            # Each program's picture, then its name, link and when.
            programs = [re.search(
                r'(?:<img[^>]+src="([^"]+)".*?)?<div\s+class="col-lg-8">.*?<h[23] class="field-content"><a href="([^"]+)">(.*?)</a></h[23]>'
                r'.*?<span class="date-display-range">(.*?)</span>', block, re.S) for block in markup.split('<div class="well">')[1:]]
            day = None
            for picture, link, title, when in (program.groups() for program in programs if program):
                # "Saturday, September 12, 2026<br>10:00 am–11:15 am"; a span reads "Friday, October 2–Friday, …".
                single = re.fullmatch(r"\w+, (\w+ \d{1,2}, \d{4})(?:<br>\s*(\d{1,2})(?::(\d{2}))?\s*([ap])m.*)?", when.strip(), re.S)
                if not single:
                    continue
                day = datetime.strptime(single.group(1), "%B %d, %Y").date()
                start = None
                if single.group(2):
                    hour = int(single.group(2)) % 12 + (12 if single.group(4) == "p" else 0)
                    start = datetime.min.time().replace(hour=hour, minute=int(single.group(3) or 0))
                listing = event(source, text(title), day, start, link=f"https://www.mfa.org{html.unescape(link)}",
                                image=html.unescape(picture or ""))
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
                        about=about(item.get("summary") or ""), image=(item.get("image_styles") or {}).get("list") or "")
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
    "massmoca": read_mass_moca,
    "amherstcinema": read_amherst_cinema,
    "indy": read_indy,
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
            kept = merged[key]
            sold = sold_times(kept) | sold_times(item)
            kept["times"] = sorted(set(kept["times"] + item["times"]))
            kept["sold_out"] = bool(kept.get("sold_out") and item.get("sold_out"))
            kept["sold_out_times"] = sorted(sold)  # Each showing's own, whichever it came with.
        else:
            merged[key] = dict(item, times=sorted(item["times"]), sold_out_times=sorted(sold_times(item)))
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
            "more": next((item.get("more", False) for item in showings if item.get("about")), False),
            "image": next((item["image"] for item in showings if item.get("image")), ""),
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


def tabler(paths, css_class):
    """One of Tabler Icons' outline icons (MIT license), from its paths, verbatim."""
    drawn = "".join(f'<path d="{d}"/>' for d in paths)
    return (f'<svg class="{css_class}" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{drawn}</g></svg>')


# A listing's copy-link button's icon (Tabler's "link").
LINK_MARK = tabler(["M9 15l6 -6", "M11 6l.463 -.536a5 5 0 0 1 7.071 7.072l-.534 .464",
                    "M13 18l-.397 .534a5.068 5.068 0 0 1 -7.127 0a4.972 4.972 0 0 1 0 -7.071l.524 -.463"], "link-mark")
# And its "check", which takes the link icon's place once the link is copied.
CHECK_MARK = tabler(["M5 12l5 5l10 -10"], "check-mark")
CLOSE_MARK = ('<svg class="close-mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" fill="none" '
              'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>')
# A listing's own view, opened by clicking it: its picture, when, its price and ages, each place it's at, a
# link to its page (tickets), with its times, what it's about, the address, its other
# dates, and a way to copy its link. The script fills it in from the listing.
EVENT_VIEW = f"""<dialog class="event" aria-labelledby="event-title">
<button class="event-close" type="button" aria-label="Close">{CLOSE_MARK}</button>
<div class="event-image" hidden><img alt="" decoding="async" referrerpolicy="no-referrer"></div>
<p class="event-where"><span class="event-icon"></span><span class="event-day"></span></p>
<h2 class="event-title" id="event-title"></h2>
<p class="event-facts"></p>
<p class="event-detail"></p>
<p class="event-note"></p>
<ul class="event-places"></ul>
<div class="event-about"></div>
<button class="event-more" type="button" hidden aria-expanded="false">Read more</button>
<p class="event-place"></p>
<p class="event-also"></p>
<button class="event-copy" type="button">{LINK_MARK}{CHECK_MARK}<span aria-live="polite">Copy link</span></button>
</dialog>
"""
# Pushpin's mark, before its name in the header: Tabler Icons' "pin" (outline, MIT license), verbatim, the
# same as the favicon's (events/pushpin).
PIN_MARK = ('<svg class="pin" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M15 4.5l-4 4l-4 1.5l-1.5 1.5l7 7l1.5 -1.5l1.5 -4l4 -4"/>'
            '<path d="M9 15l-4.5 4.5"/><path d="M14.5 4l5.5 5.5"/></g></svg>')


def icon(category, decorative=False):
    """A category's mark. Beside a label that already says it (the filter), it's hidden from screen readers."""
    return shared.icon(category, None if decorative else CATEGORIES[category])


def render_times(moments, sold=()):
    # data-time lets the page drop today's showings once they've started. The commas between are their own,
    # so one goes with a time that's gone. A sold-out one is struck through.
    struck = ' class="sold"'
    times = '<span class="sep">, </span>'.join(
        f'<time data-time="{moment:%H:%M}"{struck if f"{moment:%H:%M}" in sold else ""}>{clock(moment)}</time>' for moment in moments)
    return f'<span class="times">{times}</span>' if times else ""


def written(address):
    """(street, town, ZIP) as one line, for a calendar event's location."""
    street, town, zip_code = address
    return f"{street}, {town}, MA {zip_code}".strip()


def address_attribute(item):
    """An event's own address, when the source gives one, for its view; otherwise the page's table of venues'
    addresses serves."""
    return f' data-address="{html.escape(written(item["address"]))}"' if item.get("address") else ""


def facts_attributes(item):
    """What the listing's view shows that the row doesn't: its picture, price and ages, when known; that
    there's more of what it's about on its own page (data-more); and that it's sold out altogether (data-sold),
    which its times, struck through, don't say where it has none."""
    return ("".join(f' data-{key}="{html.escape(item[key])}"' for key in ("image", "price", "ages") if item.get(key))
            + ' data-more=""' * bool(item.get("more")) + ' data-sold=""' * bool(item.get("sold_out") and not item["times"]))


def listing_id(day, title, venue=""):
    """A listing's own address on the page (?event=…), the same from build to build: its date, then its series
    (its name, and its venue's, which the same show on other days shares): 2026-09-15-akira-4k-restoration-
    coolidge-corner. A film at several theaters has no venue in it."""
    series = "-".join(filter(None, [calendar_slug(title)[:60].strip("-"), calendar_slug(venue)]))
    return f"{day.isoformat()}-{series}", series


def render_row(item):
    """Laid out like the newsfeed: the venue on the left, then the name with that day's times after it."""
    if "showings" in item:
        return render_combined(item)
    detail = f'<span class="detail">{html.escape(item["detail"])}</span>' if item["detail"] else ""
    ident, series = listing_id(item["date"], item["title"], item["venue"])
    return (
        f'<li class="row" data-id="{ident}" data-series="{series}" data-category="{item["category"]}" '
        f'data-sources="{html.escape(item["source"])}"{address_attribute(item)}{facts_attributes(item)}>'
        f'<span class="source">{icon(item["category"])}'
        f'<span>{html.escape(item["venue"])}</span></span>'
        f'<div class="headline"><a class="title" href="{html.escape(item["link"])}">{html.escape(item["title"])}</a>'
        f'{render_times(item["times"], sold_times(item))}{detail}{shared.preview(item["title"], clip(item.get("about", []), ABOUT_CHARS))}</div></li>'
    )


def render_combined(item):
    """A film at several places: one row reading "4 theaters … from 12:10pm" that opens to each place's times."""
    places = len({showing["venue"] for showing in item["showings"]})
    sources = "|".join(sorted({showing["source"] for showing in item["showings"]}))
    first = item["times"][0] if item["times"] else None
    start = f'<span class="times">from <time class="from" data-time="{first:%H:%M}">{clock(first)}</time></span>' if first else ""
    showings = "".join(
        f'<li data-source="{html.escape(showing["source"])}">'
        f'<a href="{html.escape(showing["link"])}">{html.escape(showing["venue"])}</a>'
        f'{render_times(showing["times"], sold_times(showing))}</li>'
        for showing in item["showings"]
    )
    ident, series = listing_id(item["showings"][0]["date"], item["title"])
    return (
        f'<li class="row combined" data-id="{ident}" data-series="{series}" data-category="{item["category"]}" '
        f'data-sources="{html.escape(sources)}"{facts_attributes(item)}><details><summary>'
        f'<span class="source">{icon(item["category"])}<span>{places} theaters</span></span>'
        f'<div class="headline"><span class="title">{html.escape(item["title"])}</span>'
        f'<span class="tail">{start}<span class="more" aria-hidden="true">›</span></span>'
        f'{shared.preview(item["title"], clip(item["about"], ABOUT_CHARS))}</div></summary>'
        f'<ul class="showings">{showings}</ul></details></li>'
    )


def render_index(events, sources, failed, stale, built_at, public=False, category=None, weekend=None, tonight=False):
    """The listings. For the public site, only its sources, with shorter previews; and with a category, its own
    page (music/, film/, talks/), holding only that kind's events, or with weekend (0 for this one, 1 for the
    next), only Friday to Sunday's, or tonight, only today's (and tomorrow's, for after midnight)."""
    root = PUBLIC_ROOT
    if public:
        shown = {source["name"] for source in sources if source.get("public", True)}
        # With more, where there's more of it on the listing's own page, for its view.
        events = [dict(item, about=shorter(item.get("about", [])), more=shorter(item.get("about", [])) != item.get("about", []))
                  for item in events if item["source"] in shown]
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
            f'<h2><span class="relative"></span><span class="date"><span class="weekday">{day:%a}</span>, {day:%b} {day.day}</span></h2>\n'
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
        + EVENT_VIEW
        + f"<script>const PLACES = {json.dumps(places, ensure_ascii=False)}, SHARE_URL = {json.dumps(PUBLIC_URL)};</script>\n"
        + f"<script>{INDEX_JS}</script>"
    )
    if public:
        title, description, tagline = PUBLIC_PAGES[category][1:] if category else (PUBLIC_TITLE, PUBLIC_DESCRIPTION, PUBLIC_TAGLINE)
        if weekend is not None:
            _, name, title, description = PUBLIC_WEEKENDS[weekend]
            tagline = (f"{name} around {PUBLIC_AROUND}: {friday:%A, %B} {friday.day} to "
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
    readers to the venue for the rest. A listing's view shows all of it, from its own page."""
    return clip(paragraphs, PUBLIC_ABOUT_CHARS, 2, least=60)


def public_footer(path, notes="", names=""):
    """The foot of each of the public site's pages: the site's pages in three short lists (this one marked),
    and, under a list of events, where they come from, then any source it couldn't reach."""
    root = PUBLIC_ROOT
    groups = [
        ("Browse", [("All events", "")] + [(label, f"{PUBLIC_PAGES[key][0]}/") for key, label in CATEGORIES.items()]),
        ("When", [("Tonight", PUBLIC_TONIGHT[0])] + [(name, weekend_path) for weekend_path, name, *_ in PUBLIC_WEEKENDS]),
        (PUBLIC_NAME, [("About", "about/"), ("Calendars", "about/#calendars"), ("Contact", "contact/")]
         + [("All cities", "/")] * (len(CITIES) > 1)),  # pushpin.city itself, which lists them.
    ]
    marked = ' aria-current="page"'
    lists = "".join(
        f'<div><h2>{html.escape(heading)}</h2><ul>' + "".join(
            f'<li><a href="{href if href.startswith("/") else root + href}"{marked if href == path else ""}>{html.escape(label)}</a></li>'
            for label, href in links)
        + "</ul></div>"
        for heading, links in groups
    )
    where = f"<p>Listings from {names}, aggregated from their own calendars every few hours.</p>\n" if names else ""
    return f'<footer>\n<nav class="site-links" aria-label="{html.escape(PUBLIC_NAME)}">{lists}</nav>\n{where}{notes}</footer>\n'


# Each page's name, after the site's in the header ("Pushpin Boston / Film"); the home page has none.
PAGE_NAMES = ({f"{slug}/": CATEGORIES[key] for key, (slug, *_) in PUBLIC_PAGES.items()} | {PUBLIC_TONIGHT[0]: "Tonight"}
              | {path: name for path, name, *_ in PUBLIC_WEEKENDS} | {"about/": "About", "contact/": "Contact"})
# The pages with a share card of their own (events/share); the rest show the home page's.
SHARE_CARDS = {slug for slug, *_ in PUBLIC_PAGES.values()} | {"tonight", "weekend"}


def public_page(path, title, body, built_at=None, description=None, data=None):
    """A page of the public site, at path ("", "about/", "film/"): its name, the way home, in the header,
    About and Contact in the footer; search engines welcome, told what the page is, where it lives, and (the
    listings) its events."""
    root = PUBLIC_ROOT
    description = description or PUBLIC_DESCRIPTION
    links = [(PUBLIC_NAME, root, path == "")]
    card = path.split("/")[0]
    if "<footer>" not in body:
        body += "\n" + public_footer(path)
    address = PUBLIC_URL + path
    head = "\n".join([
        # The pin (events/pushpin). ?pin3, since browsers keep a site's old icon long after it changes.
        f'<link rel="icon" href="{root}favicon.svg?pin3" type="image/svg+xml">',
        f'<link rel="apple-touch-icon" href="{root}apple-touch-icon.png?pin3">',
        f'<link rel="canonical" href="{address}">',
        f'<meta name="description" content="{html.escape(description)}">',
        # What a link to it shows when shared.
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{html.escape(PUBLIC_NAME)}">',
        f'<meta property="og:title" content="{html.escape(title)}">',
        f'<meta property="og:description" content="{html.escape(description)}">',
        f'<meta property="og:url" content="{address}">',
        # Its card (events/share, made by share_cards.py): a kind's page its own, the rest the site's.
        # ?pin: apps keep a card they've seen, however long it's been replaced, until its address changes.
        f'<meta property="og:image" content="{PUBLIC_URL}share/{card if card in SHARE_CARDS else "home"}.png?pin">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:image:alt" content="{html.escape(PUBLIC_NAME)}: {html.escape(PUBLIC_TAGLINE)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<script data-goatcounter="{GOATCOUNTER}" async src="https://gc.zgo.at/count.js"></script>',
    ] + [f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False, separators=(",", ":"))}</script>'] * bool(data))
    brand, _, city = PUBLIC_NAME.partition(" ")
    return shared.page("events", title, body, css=CSS + PUBLIC_CSS, head=head, symbols=ICON_SYMBOLS,
                       updated=built_at, links=links, indexable=True,
                       marked={PUBLIC_NAME: f'{PIN_MARK}{html.escape(brand)} <span class="city">{html.escape(city)}</span>'},
                       here=PAGE_NAMES.get(path, ""))


# The About page's questions, each with its answer (markup, with {root} for the site's root), in Graham's words.
FAQS = [
    ("Who makes this?",
     "I’m <a href=\"https://grahamhagenah.com\">Graham Hagenah</a>. I live in Somerville and love supporting my local theaters and concert venues, and I "
     "wanted an easier way to track what’s coming up than relying on Google or visiting each venue’s website."),
    ("Why isn’t the Somerville Theatre on here?",
     "Its concerts are, but not its films. The theater’s website has no public calendar feed and doesn’t allow "
     "automated web crawling, so its screenings can’t be gathered. I’m trying to get in touch with its managers "
     "to find a way around this. The same goes for the Capitol Theatre in Arlington."),
    ("Why isn’t my favorite venue here?",
     "It may not publish its calendar in a way that can be read automatically, or I may not have found it yet. "
     "<a href=\"{root}contact/\">Tell me about it</a> and I’ll take a look."),
    ("Will you make this for my city?",
     "I’d rather keep it to places I know well. I add every venue one by one, and curating a list of cool spots "
     "would be challenging if I’m not a local. But if there’s enough interest in another city or area, I’ll look into "
     "starting one there, and it would go much faster with help from someone who knows its venues. If that’s you, "
     "<a href=\"{root}contact/\">get in touch</a>."),
    ("How often is it updated?",
     "Every few hours, from each venue’s own calendar. Shows added or changed since then appear with the next "
     "update, so check the venue’s page before you go."),
    ("Why do some listings have no time?",
     "Some venues post only the date, or only when their doors open, not when the show starts. The listing links "
     "to the venue’s page, which usually has the rest."),
    ("Does it track me?",
     "Only anonymously. It counts visits, and which listings are opened, followed to their venues, and shared, with "
     "<a href=\"https://www.goatcounter.com\">GoatCounter</a>, which uses no cookies and keeps nothing personal, "
     "so I can tell whether people are using it. There are no ads and no accounts, and your venue and search "
     "choices live only in the page’s address."),
]


@dataclass
class City:
    """A city's Pushpin: its folder on pushpin.city and its sources file (both named for it), what it's called,
    and what its pages say (as the PUBLIC_ names above say them for Boston)."""
    slug: str
    name: str
    title: str
    tagline: str
    description: str
    around: str
    tonight: tuple
    weekends: list
    pages: dict
    faqs: list

    @property
    def sources(self):
        return SOURCES_FILE if self.slug == "boston" else ROOT / f"sources-{self.slug}.txt"


BOSTON_CITY = City("boston", PUBLIC_NAME, PUBLIC_TITLE, PUBLIC_TAGLINE, PUBLIC_DESCRIPTION, PUBLIC_AROUND, PUBLIC_TONIGHT,
                   PUBLIC_WEEKENDS, PUBLIC_PAGES, FAQS)


def city_texts(slug, name, place, around, film_description, who):
    """A city's page names and descriptions, in the words Boston's use: place in titles ("Concerts in Western
    Mass"), around in the rest ("around the Pioneer Valley and the Berkshires"); and who makes it, as its
    About page's answer says."""
    return City(
        slug, name, f"{name} · Concerts, films and talks in {place}",
        f"Concerts, films, and talks around {around}, aggregated from select venues.",
        f"Concerts for the next two months, and film screenings and art talks for the next month, around {around}, "
        f"on one page, from select venues.",
        around,
        ("tonight/", f"Things to do in {place} tonight · {name}",
         f"Concerts, films and talks still to come today around {around}, aggregated from select venues.",
         f"Tonight around {around}: everything still to come today, aggregated from select venues."),
        [("weekend/", "This weekend", f"Things to do in {place} this weekend · {name}",
          f"Concerts, films and talks around {around} this weekend, Friday to Sunday, aggregated from select venues."),
         ("weekend/next/", "Next weekend", f"Things to do in {place} next weekend · {name}",
          f"Concerts, films and talks around {around} next weekend, Friday to Sunday, aggregated from select venues.")],
        {"music": ("music", f"Concerts in {place} · {name}",
                   f"Concerts around {around} for the next two months, aggregated from select venues.",
                   f"Concerts around {around}, aggregated from select venues."),
         "film": ("film", f"Movie showtimes in {place} · {name}", film_description,
                  f"Films around {around}, aggregated from select theaters."),
         "art": ("talks", f"Art, exhibitions and talks in {place} · {name}",
                 f"Talks, readings, performances and exhibition openings around {around} for the next month, aggregated "
                 f"from select venues.",
                 f"Art and talks around {around}, aggregated from select venues.")},
        # Boston's questions, with its own answer to who makes it, but not the one about Boston's theaters.
        [(question, who if question == "Who makes this?" else answer) for question, answer in FAQS if "Somerville Theatre" not in question])


WESTERN_MASS = city_texts("westernma", "Pushpin Western Mass", "Western Mass", "the Pioneer Valley and the Berkshires",
                          "Showtimes at Amherst Cinema, Images Cinema and the Triplex, for the next month, aggregated from "
                          "select theaters.",
                          "I’m <a href=\"https://grahamhagenah.com\">Graham Hagenah</a>. I grew up in Western Mass, and I love "
                          "supporting its theaters and concert venues. I wanted an easier way to track what’s coming up than "
                          "relying on Google or visiting each venue’s website.")
# Each city's Pushpin, at pushpin.city/<its slug>/, from sources-<its slug>.txt (Boston's, sources.txt).
CITIES = [BOSTON_CITY, WESTERN_MASS]


def use_city(city):
    """Point the public site's names (PUBLIC_NAME, PUBLIC_URL, and the rest) at a city's: its pages are written
    one city at a time, and read these as they go."""
    global PUBLIC_NAME, PUBLIC_URL, PUBLIC_ROOT, CITY_DIR, PUBLIC_TITLE, PUBLIC_TAGLINE, PUBLIC_DESCRIPTION, PUBLIC_AROUND
    global PUBLIC_TONIGHT, PUBLIC_WEEKENDS, PUBLIC_PAGES, FAQS
    PUBLIC_NAME, PUBLIC_TITLE, PUBLIC_TAGLINE, PUBLIC_DESCRIPTION, PUBLIC_AROUND = (
        city.name, city.title, city.tagline, city.description, city.around)
    PUBLIC_TONIGHT, PUBLIC_WEEKENDS, PUBLIC_PAGES, FAQS = city.tonight, city.weekends, city.pages, city.faqs
    PUBLIC_URL = f"{PUBLIC_SITE}{city.slug}/"
    PUBLIC_ROOT = urlsplit(PUBLIC_URL).path
    CITY_DIR = PUBLIC_DIR / city.slug


def render_about(sources, built_at, events=()):
    """What the public site is, how to use it, and every venue it reads, by kind: each a link to its events,
    with how many it has coming up. A venue is under each kind it has events of (the MFA's concerts and films
    as well as its talks); one with none right now, under its own kind."""
    root = PUBLIC_ROOT
    public = [source for source in sources if source.get("public", True)]
    names = {source["name"] for source in public}
    counts = Counter((item["source"], item["category"]) for item in events if item["source"] in names)
    kinds = {key: {name for name, category in counts if category == key} for key in CATEGORIES}
    for source in public:
        if not any(name == source["name"] for name, _ in counts):
            kinds.setdefault(source["category"], set()).add(source["name"])

    def venue(name, key):
        count = counts.get((name, key))
        return (f'<li><a href="{root}?{urlencode({"venue": name})}">{html.escape(name)}</a>'
                + (f' <span class="count">{count}</span>' if count else "") + "</li>")

    kind_feeds = "".join(
        f'<li data-category="{key}">{icon(key, decorative=True)}{html.escape(CATEGORIES[key])}: <a class="subscribe-kind" href="{calendar_address(f"calendar/{slug}.ics")}">Apple Calendar</a>'
        f' · <a href="{calendar_address(f"calendar/{slug}.ics", google=True)}">Google Calendar</a></li>'
        for key, (slug, *_) in PUBLIC_PAGES.items())

    groups = "".join(
        f'<h2 data-category="{key}">{icon(key, decorative=True)}{html.escape(CATEGORIES[key])}</h2>\n<ul class="venues">{"".join(venue(name, key) for name in shared.as_said(kinds[key]))}</ul>\n'
        for key in CATEGORIES if kinds.get(key)
    )
    faq = "\n".join(f"<details><summary>{html.escape(question)}</summary><p>{answer.replace('{root}', root)}</p></details>"
                    for question, answer in FAQS)
    body = f"""<div class="prose">
<p>{PUBLIC_NAME} puts concerts, films, and talks from venues across {PUBLIC_AROUND} on one page,
day by day: concerts two months ahead, films and talks one month.</p>
<p>This is a curated feed, with an emphasis on independent venues.</p>
<p>Event info is gathered from select venues every few hours. Times and details can change, so check with the
venue before you go: every listing links back to the original source where you can confirm times or buy
tickets.</p>
<p>No algorithms, no ads, no accounts.</p>
<h2>How to use it</h2>
<ul class="tips">
<li>Tap a listing for more: a picture, the price and ages, every showtime, what it’s about, the address, its other
dates, and a link to copy and send.</li>
<li><a href="{root}tonight/">Tonight</a> and <a href="{root}weekend/">This weekend</a> filter the list down to just
those days.</li>
<li>Pick a venue from the menu to show only its events. The browser’s address keeps your choice, so you can share
it with friends.</li>
<li>Search matches names, venues, and descriptions: a band, a director, “35mm”. On a keyboard, press slash to jump
to it.</li>
<li>A showtime struck through is sold out, and a listing with nothing left says so.</li>
<li>Subscribe to a calendar (below) to have new listings show up on their own.</li>
</ul>
<h2 id="calendars">Calendars</h2>
<p>Subscribe in your calendar app, and new listings appear there on their own, updated every few hours.</p>
<ul class="tips feeds">
{kind_feeds}
</ul>
<h2>Venues</h2>
<p>Every venue in the feed, with how many listings each one has coming up.</p>
{groups}
<h2>Questions</h2>
<div class="faq">
{faq}
</div>
<p>Know a venue that should be here, or spotted a mistake? <a href="{root}contact/">Get in touch</a>.</p>
</div>"""
    # The questions, for search engines too, which can show them in their results.
    answers = [(question, answer.replace("{root}", root)) for question, answer in FAQS]
    data = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": question,
         "acceptedAnswer": {"@type": "Answer", "text": html.unescape(re.sub(r"<[^>]+>", "", answer))}}  # Links' words kept.
        for question, answer in answers]}
    return public_page("about/", f"About · {PUBLIC_NAME}", body,
                       description=f"What {PUBLIC_NAME} is, how to use it, the venues it lists around "
                                   f"{PUBLIC_AROUND.replace(', and', ' and')}, and questions about it.", data=data)


def render_redirect(address, paths=False):
    """A page that sends its visitor on to address at once, keeping any ?venue= or ?q= and #, and tells search
    engines that's where it lives: the site's root (Boston's, until there's another city), and the pages left
    at the site's first address. With paths (a page not found), the rest of the address it was asked for comes
    along: /music/ goes to /boston/music/; one already under /boston/ that isn't there, to Boston's home page."""
    link = html.escape(address)
    target = json.dumps(address)
    if paths:
        target += f' + (location.pathname.startsWith({json.dumps(PUBLIC_ROOT)}) ? "" : location.pathname.slice(1))'
        # A listing's page that's gone (it's passed): its view, which opens the next of its series instead.
        listing = json.dumps(PUBLIC_ROOT + "e/")
        target = (f'(location.pathname.startsWith({listing}) ? {json.dumps(address)} + "?event=" + '
                  f'location.pathname.slice({len(PUBLIC_ROOT) + 2}).split("/")[0] : {target})')
    return (f'<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n<title>{html.escape(PUBLIC_NAME)}</title>\n'
            f'<meta http-equiv="refresh" content="0; url={link}">\n<link rel="canonical" href="{link}">\n'
            f'<meta name="color-scheme" content="dark">\n<style>html {{ background: #000; }}</style>\n'
            f"<script>location.replace({target} + location.search + location.hash);</script>\n"
            f'<p><a href="{link}">{html.escape(PUBLIC_NAME)}</a></p>\n</html>\n')


def render_cities(cities):
    """pushpin.city itself: each city's Pushpin, a link to it with what it covers."""
    links = "".join(
        f'<li><a href="/{city.slug}/">{html.escape(city.name.partition(" ")[2])}</a><p>{html.escape(city.tagline)}</p></li>\n'
        for city in cities)
    description = f"Concerts, films, and talks, aggregated from select venues: {', '.join(city.name.partition(' ')[2] for city in cities)}."
    return (f'<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>Pushpin</title>\n<meta name="description" content="{html.escape(description)}">\n'
            f'<link rel="canonical" href="{PUBLIC_SITE}">\n<link rel="icon" href="/favicon.svg?pin3" type="image/svg+xml">\n'
            f'<link rel="apple-touch-icon" href="/apple-touch-icon.png?pin3">\n'
            f'<meta property="og:type" content="website">\n<meta property="og:site_name" content="Pushpin">\n'
            f'<meta property="og:title" content="Pushpin">\n<meta property="og:description" content="{html.escape(description)}">\n'
            f'<meta property="og:url" content="{PUBLIC_SITE}">\n<meta property="og:image" content="{PUBLIC_SITE}share/home.png?pin">\n'
            f'<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">\n'
            f'<meta name="twitter:card" content="summary_large_image">\n<meta name="color-scheme" content="dark">\n'
            f'<script data-goatcounter="{GOATCOUNTER}" async src="https://gc.zgo.at/count.js"></script>\n'
            '<style>\n'
            '  html { background: #000; color: #fff; font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }\n'
            '  body { max-width: 34rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }\n'
            '  h1 { display: flex; align-items: center; gap: .45rem; margin: 0 0 .5rem; font-size: 1.35rem; letter-spacing: -.01em; }\n'
            '  h1 .pin { width: 1.1rem; height: 1.1rem; }\n'
            '  .intro { margin: 0 0 2.5rem; color: #8c8c8c; }\n'
            '  ul { margin: 0; padding: 0; list-style: none; }\n'
            '  li { padding: 1.1rem 0; border-top: 1px solid #1c1c1c; }\n'
            '  li:last-child { border-bottom: 1px solid #1c1c1c; }\n'
            '  li a { color: #fff; font-size: 1.5rem; font-weight: 700; letter-spacing: -.01em; text-decoration: none; }\n'
            '  li a::after { content: " →"; color: #555; font-weight: 400; }\n'
            '  li a:hover { text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }\n'
            '  li p { margin: .3rem 0 0; color: #8c8c8c; }\n'
            '</style>\n'
            f'<h1>{PIN_MARK}Pushpin</h1>\n<p class="intro">Concerts, films, and talks, aggregated from select venues. No algorithms, no ads, no accounts.</p>\n'
            f'<ul>\n{links}</ul>\n</html>\n')


def render_not_found(cities):
    """pushpin.city's page for an address it doesn't have. One under a city's folder goes to that city's home
    page, or a listing's page that's gone (it's passed), to its view, which opens the next of its series; any
    other, from before there were cities (/music/, /about.html), to the same under Boston's."""
    slugs = json.dumps([city.slug for city in cities])
    return (f'<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n<title>Pushpin</title>\n'
            f'<meta name="color-scheme" content="dark">\n<style>html {{ background: #000; }}</style>\n'
            '<script>\n'
            '  const path = location.pathname;\n'
            f'  const city = {slugs}.find(slug => path.startsWith("/" + slug + "/"));\n'
            '  const rest = city ? path.slice(city.length + 2) : "";\n'
            '  location.replace(!city ? "/boston/" + path.slice(1) + location.search + location.hash\n'
            '    : rest.startsWith("e/") ? "/" + city + "/?event=" + rest.slice(2).split("/")[0]\n'
            '    : "/" + city + "/" + location.search + location.hash);\n'
            '</script>\n'
            f'<p><a href="/" style="color: #fff">Pushpin</a></p>\n</html>\n')


# Calendar feeds to subscribe to: one for each kind, and one for each venue, under the city's calendar/ folder.
def calendar_slug(name):
    """A venue's name as its feed's file name: "The Sinclair" is the-sinclair.ics."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def calendar_address(path, google=False):
    """A feed's address to subscribe with: webcal://, which Apple's Calendar (and Outlook) open to subscribe; or
    Google Calendar's page for adding it."""
    webcal = "webcal://" + (PUBLIC_URL + path).removeprefix("https://")
    return f"https://calendar.google.com/calendar/r?{urlencode({'cid': webcal})}" if google else webcal


def ics_text(value):
    """Text as an iCalendar value: its backslashes, commas, semicolons and line breaks escaped."""
    return re.sub(r"([\\,;])", r"\\\1", value).replace("\n", "\\n")


def ics_fold(line):
    """An iCalendar line folded at 75 bytes, as the format asks, the rest on lines starting with a space."""
    out, current = [], b""
    for character in line:
        encoded = character.encode()
        if len(current) + len(encoded) > (75 if not out else 74):
            out.append(current.decode())
            current = b""
        current += encoded
    out.append(current.decode())
    return "\r\n ".join(out)


def render_calendar(name, description, items, built_at):
    """A calendar feed of items, which a calendar app subscribes to and checks again every few hours: one event
    for each listing, with its first time (two hours, as most give only when they start) and the rest of its
    times in its notes, or all day when it has none. Its id stays the same from build to build, so an app
    updates it rather than adding it again."""
    stamp = f"{built_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//Pushpin//{PUBLIC_NAME}//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_text(name)}", f"X-WR-CALDESC:{ics_text(description)}",
             "X-WR-TIMEZONE:America/New_York", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for item in sorted(items, key=lambda item: (item["date"], item["times"][:1], item["title"].casefold())):
        key = "|".join([item["source"], item["venue"], item["title"], item["date"].isoformat()])
        lines += ["BEGIN:VEVENT", f"UID:{hashlib.sha1(key.encode()).hexdigest()}@pushpin.city", f"DTSTAMP:{stamp}"]
        if item["times"]:
            start = datetime.combine(item["date"], item["times"][0], BOSTON).astimezone(timezone.utc)
            lines += [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{start + timedelta(hours=2):%Y%m%dT%H%M%SZ}"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{item['date']:%Y%m%d}", f"DTEND;VALUE=DATE:{item['date'] + timedelta(days=1):%Y%m%d}"]
        place = item.get("address") or VENUE_ADDRESSES.get(item["venue"])
        notes = ([f"Times: {', '.join(clock(moment) for moment in item['times'])}"] if len(item["times"]) > 1 else []) + \
                [item["detail"]] * bool(item["detail"]) + item.get("about", [])[:1] + [item["link"], f"From {PUBLIC_NAME}: {PUBLIC_URL}"]
        lines += [f"SUMMARY:{ics_text(item['title'])}",
                  f"LOCATION:{ics_text(item['venue'] + (', ' + written(place) if place else ''))}",
                  f"DESCRIPTION:{ics_text(chr(10).join(notes))}", f"URL:{item['link']}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


def calendar_feeds(sources, events, built_at):
    """Each feed's path under the city's folder and what's in it: each kind's (calendar/music.ics), and each
    venue's (calendar/the-sinclair.ics), of the public site's venues."""
    public = {source["name"] for source in sources if source.get("public", True)}
    events = [item for item in events if item["source"] in public]
    feeds = {f"calendar/{slug}.ics": render_calendar(f"{PUBLIC_NAME} · {CATEGORIES[key]}",
                                                     f"{CATEGORIES[key]} around {PUBLIC_AROUND}, from {PUBLIC_NAME}.",
                                                     [item for item in events if item["category"] == key], built_at)
             for key, (slug, *_) in PUBLIC_PAGES.items()}
    for name in sorted({item["source"] for item in events}):
        feeds[f"calendar/{calendar_slug(name)}.ics"] = render_calendar(
            f"{PUBLIC_NAME} · {name}", f"What’s coming up at {name}, from {PUBLIC_NAME}.",
            [item for item in events if item["source"] == name], built_at)
    return feeds


def render_event_page(item, day):
    """A listing's own small page (/boston/e/<its id>/), for the preview a shared link shows: what it is, when
    and where (share_title), its price and ages, and its picture (or its kind's card); and all of what it's about, which the
    listing's view reads from it. A link's preview comes from the page it points to, and the apps that show one
    don't run the page's script, so the home page's ?event= address would show only the home page's. Someone
    who opens it goes straight on to the listing's view there."""
    combined = "showings" in item
    ident = listing_id(day, item["title"], "" if combined else item["venue"])[0]
    target = f"{PUBLIC_URL}?{urlencode({'event': ident})}"
    if combined:
        places = sorted({showing["venue"] for showing in item["showings"]})
        where = f"{len(places)} theaters ({', '.join(places)})"
        when = f"from {clock(item['times'][0])}" if item["times"] else ""
    else:
        where, when = item["venue"], ", ".join(clock(moment) for moment in item["times"])
    sold_out = all(is_sold_out(showing) for showing in item["showings"]) if combined else is_sold_out(item)
    description = " · ".join(filter(None, [where, f"{day:%a, %b} {day.day}", when, "Sold out" if sold_out else item.get("price"), item.get("ages")]))
    headline = share_title(item, day)
    title = f"{headline} · {PUBLIC_NAME}"
    # Its own picture when the venue gives one; else its kind's card, whose size is known.
    image = item.get("image") or f"{PUBLIC_URL}share/{PUBLIC_PAGES[item['category']][0]}.png?pin"
    size = [] if item.get("image") else ['<meta property="og:image:width" content="1200">', '<meta property="og:image:height" content="630">']
    tags = "\n".join([
        f'<meta property="og:{key}" content="{html.escape(value)}">' for key, value in [
            ("type", "website"), ("site_name", PUBLIC_NAME), ("title", headline), ("description", description),
            ("url", f"{PUBLIC_URL}e/{ident}/"), ("image", image)]
    ] + size + ['<meta name="twitter:card" content="summary_large_image">'])
    return (f'<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{html.escape(title)}</title>\n<meta name="description" content="{html.escape(description)}">\n'
            f'<meta name="robots" content="noindex">\n{tags}\n<meta name="color-scheme" content="dark">\n'
            f'<style>html {{ background: #000; color: #ddd; font: 17px/1.45 -apple-system, BlinkMacSystemFont, sans-serif; }} '
            f'body {{ margin: 2rem 1.25rem; }} a {{ color: #fff; }}</style>\n'
            f"<script>location.replace({json.dumps(target)});</script>\n"
            f'<h1>{html.escape(item["title"])}</h1>\n<p>{html.escape(description)}</p>\n'
            f'<div class="about">{"".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in item.get("about", []))}</div>\n'
            f'<p><a href="{html.escape(target)}">See it on {html.escape(PUBLIC_NAME)}</a></p>\n</html>\n')


def share_title(item, day):
    """What a shared link's preview calls a listing, with what it is, where and when, since iMessage shows only
    that and the picture: "Showtimes for Akira (4K Restoration) at Coolidge Corner · Tue, Sep 15" (a film at
    several theaters, just "Showtimes for Hope"), "Aldous Harding at The Sinclair · Mon, Sep 14, 8pm". A venue
    already in the name isn't said again."""
    combined = "showings" in item
    name, venue = item["title"], "" if combined else item["venue"]
    at = f" at {venue}" if venue and venue.casefold() not in name.casefold() else ""
    when = f"{day:%a, %b} {day.day}"
    if item["category"] == "film":
        return f"Showtimes for {name}{at} · {when}"
    return f"{name}{at} · {when}" + (f", {clock(item['times'][0])}" if len(item["times"]) == 1 else "")


def event_pages(sources, events):
    """Each listing's page (render_event_page), by its path under the city's folder, for the public site's
    listings, as its pages show them: a film at several theaters as one."""
    public = {source["name"] for source in sources if source.get("public", True)}
    days = {}
    for item in events:
        if item["source"] in public:
            days.setdefault(item["date"], []).append(item)
    pages = {}
    for day, items in days.items():
        for item in combine_films(items):
            ident = listing_id(day, item["title"], "" if "showings" in item else item["venue"])[0]
            pages[f"e/{ident}/index.html"] = render_event_page(item, day)
    return pages


def render_sitemap(built_at):
    """The public site's pages for search engines: the listings, changing every few hours, and the others."""
    pages = ([("", "hourly", "1.0"), (PUBLIC_TONIGHT[0], "hourly", "0.9")] + [(path, "hourly", "0.9") for path, *_ in PUBLIC_WEEKENDS] + [(f"{slug}/", "hourly", "0.9") for slug, *_ in PUBLIC_PAGES.values()]
             + [("about/", "monthly", "0.5"), ("contact/", "yearly", "0.3")])
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
<input type="hidden" name="_next" value="{PUBLIC_URL}contact/?sent">
<input type="hidden" name="_template" value="box">
<input type="text" name="_honey" class="honey" tabindex="-1" autocomplete="off" aria-hidden="true">
<label>Your email, for a reply <input type="email" name="email" required></label>
<label>Message <textarea name="message" rows="6" required></textarea></label>
<button>Send</button>
</form>
<div class="sent" hidden>
<p>Thanks, your message is on its way. We’ll do our best to respond in a timely manner.</p>
<a class="home" href="{PUBLIC_ROOT}">Back to home</a>
</div>
</div>
<script>
  // Back from sending: only the thanks.
  if (new URLSearchParams(location.search).has("sent")) {{
    document.querySelector(".intro").hidden = true;
    document.querySelector(".contact").hidden = true;
    document.querySelector(".sent").hidden = false;
  }}
</script>"""
    return public_page("contact/", f"Contact · {PUBLIC_NAME}", body,
                       description=f"Suggest a venue for {PUBLIC_NAME}, or tell us about a listing that’s wrong.")


INDEX_JS = """
  // What's used on Pushpin Boston, counted with GoatCounter along with its visits (no cookies, nothing personal):
  // listings followed to their venue, showtimes added to a calendar, venues chosen. The personal events page
  // has no counter, so there these do nothing.
  const tally = (path, title) => window.goatcounter?.count?.({ path, title, event: true });
  const venueOf = el => el.closest(".showings li")?.dataset.source || el.closest(".day > ul > li")?.dataset.sources || "";
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

  // A day's heading pinned to the top of the window gets a faint line under it: the one at the top whose day
  // is still on screen. Checked as the page scrolls, once a frame at most, and whenever the list changes.
  const headings = [...document.querySelectorAll(".day > h2")];
  function markPinned() {
    for (const h2 of headings) {
      const top = h2.getBoundingClientRect().top;
      h2.classList.toggle("pinned", h2.offsetParent !== null && top < 1 && h2.parentElement.getBoundingClientRect().bottom > 0);
    }
  }
  let pinning = false;
  addEventListener("scroll", () => {
    if (!pinning) requestAnimationFrame(() => { pinning = false; markPinned(); });
    pinning = true;
  }, { passive: true });

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
  // A listing with nothing left says so after its times: every time it has struck through, or none, and marked
  // sold out altogether.
  for (const li of document.querySelectorAll(".day > ul > li")) {
    const times = li.querySelectorAll(":scope time:not(.from)");
    if ("sold" in li.dataset || (times.length && [...times].every(t => t.classList.contains("sold")))) {
      li.classList.add("sold-out");
      li.querySelector(".headline").insertBefore(Object.assign(document.createElement("span"), { className: "sold-tag", textContent: "Sold out" }),
        li.querySelector(".headline > .preview"));
    }
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
    markPinned();
    // The home page, showing a kind in place, names it in the header, as that kind's own page does.
    const here = document.querySelector(".sites .here");
    if (here && kindPages && everything) {
      here.textContent = show === "all" ? "" : filter.querySelector(`a[data-show="${show}"]`).textContent;
      here.hidden = show === "all";
      const name = document.querySelector(".sites a");
      if (show === "all") name.setAttribute("aria-current", "page");
      else name.removeAttribute("aria-current");
    }
  }
  showEvents();
  filter.addEventListener("click", event => {
    const b = event.target.closest("[data-show]");
    // A kind's own page has only its events, so its links go to the others' pages; so does a click to open one
    // in a new tab.
    if (!b || !everything || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    show = b.dataset.show;
    if (kindPages) {
      history.pushState(null, "", new URL(b.dataset.href, location.href).pathname + query());
      window.goatcounter?.count?.({ path: location.pathname }); // A kind's page, shown in place, is a visit to it.
    } else {
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
    if (venue.value) tally("venue/" + venue.value);
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

  // A note at the foot of the window, for a moment: that a listing sent in a link has passed.
  const toast = message => {
    const note = document.querySelector(".toast") || document.body.appendChild(Object.assign(document.createElement("p"), { className: "toast" }));
    note.textContent = message;
    note.classList.add("shown");
    clearTimeout(note.timer);
    note.timer = setTimeout(() => note.classList.remove("shown"), 2200);
  };

  // A listing opens its own view: when; each place it's at, a link to its page, for tickets, with its times; what it's about; the address and a map; its other dates; and a way
  // to copy its link. The view has an address of its own (?event=…), so Back closes it and a shared link opens it.
  // The listing's name is still a link to the venue's page: a click with a modifier key, or a middle click,
  // goes straight there.
  const view = document.querySelector("dialog.event");
  const part = name => view.querySelector(".event-" + name);
  const rowWith = id => [...document.querySelectorAll(".day > ul > li[data-id]")].find(li => li.dataset.id === id);
  const dayName = date => date === today ? "Today" : date === tomorrow ? "Tomorrow"
    : new Date(date + "T12:00:00Z").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  const eventInAddress = () => new URLSearchParams(location.search).get("event");
  const withEvent = id => {
    const url = new URL(location.href);
    if (id) url.searchParams.set("event", id); else url.searchParams.delete("event");
    return url.pathname + url.search;
  };
  let shown = null, pushed = false;
  for (const opener of document.querySelectorAll(".day a.title, .combined summary")) opener.setAttribute("aria-haspopup", "dialog");
  // Its times, as the listing has them.
  const timeLabels = times => group([...times].map(t => {
    const label = Object.assign(document.createElement("span"), { className: "event-time", textContent: t.textContent });
    if (t.classList.contains("sold")) { label.classList.add("sold"); label.title = "Sold out"; label.setAttribute("aria-label", t.textContent + ", sold out"); }
    return label;
  }));
  // Together, to the right of what they're for, going on to another line there when there are more than fit.
  const group = labels => {
    const times = Object.assign(document.createElement("span"), { className: "event-group" });
    times.append(...labels);
    return times;
  };
  // All of what a listing's about, from its own page, which has it (a row has only an excerpt): fetched once,
  // when it's pressed or opened, and only where there's more (data-more).
  const abouts = new Map();
  const aboutOf = li => {
    const id = li.dataset.id;
    if (!("more" in li.dataset)) return null;
    if (!abouts.has(id)) abouts.set(id, fetch(new URL(SHARE_URL).pathname + "e/" + id + "/")
      .then(response => response.ok ? response.text() : Promise.reject())
      .then(page => [...new DOMParser().parseFromString(page, "text/html").querySelectorAll(".about p")].map(p => p.textContent))
      .catch(() => { abouts.delete(id); return null; }));
    return abouts.get(id);
  };
  // Shown up to a few lines, fading out, with Read more for the rest, when it's longer than that.
  function showAbout(paragraphs) {
    const box = part("about"), more = part("more");
    box.replaceChildren(...paragraphs.map(text => Object.assign(document.createElement("p"), { textContent: text })));
    box.classList.add("clamped");
    more.hidden = true;
    requestAnimationFrame(() => { // Once the view's open, to measure it.
      const long = box.scrollHeight > box.clientHeight + 40; // Not to hide just a line or two.
      box.classList.toggle("clamped", long);
      more.hidden = !long;
      more.textContent = "Read more";
      more.setAttribute("aria-expanded", "false");
    });
  }
  function fill(li, note = "") {
    shown = li;
    const combined = li.classList.contains("combined");
    const title = li.querySelector(".title").textContent.trim();
    const venue = li.querySelector(".source > span").textContent;
    view.dataset.category = li.dataset.category;
    part("icon").replaceChildren(li.querySelector(".source .icon").cloneNode(true));
    part("day").textContent = dayName(li.closest(".day").dataset.date);
    part("title").textContent = title;
    // Its picture, in a frame kept its size while it loads, faintly shimmering, and gone if it doesn't; a
    // poster or a square flyer shown whole, not cropped to the frame.
    const picture = part("image"), img = picture.firstElementChild;
    img.removeAttribute("src");
    picture.classList.remove("whole", "loaded");
    picture.hidden = !li.dataset.image;
    if (li.dataset.image) img.src = li.dataset.image;
    // Sold out, all of it or some of its times, before its price and ages.
    const struck = li.querySelectorAll("time.sold:not(.from)").length;
    const sold = li.classList.contains("sold-out") ? "Sold out" : struck ? (struck > 1 ? "Some showings sold out" : "One showing sold out") : "";
    part("facts").replaceChildren(...[sold, li.dataset.price, li.dataset.ages].filter(Boolean).map(fact =>
      Object.assign(document.createElement("span"), { className: "event-fact" + (fact === sold ? " sold" : ""), textContent: fact })));
    part("detail").textContent = li.querySelector(".detail")?.textContent || "";
    part("note").textContent = note;
    // Where it is, the same way whether it's at one place or a film at several: each place, a link to its page
    // (for tickets), with its times, each adding that showing to a calendar.
    const places = combined
      ? [...li.querySelectorAll(".showings li")].filter(place => !place.hidden)
          .map(place => ({ name: place.querySelector("a").textContent, href: place.querySelector("a").href, source: place.dataset.source,
                           times: place.querySelectorAll("time[data-time]") }))
      : [{ name: venue, href: li.querySelector("a.title").href, source: li.dataset.sources, times: li.querySelectorAll("time[data-time]:not(.from)") }];
    part("places").replaceChildren(...places.map(place => {
      const item = document.createElement("li");
      const a = Object.assign(document.createElement("a"), { href: place.href, textContent: place.name + " ↗",
        title: (li.dataset.category === "art" ? "Details at " : "Tickets at ") + place.name });
      a.dataset.source = place.source;
      item.append(a, timeLabels(place.times));
      return item;
    }));
    // What it's about: the row's excerpt at once, then all of it, from its own page, where there's more.
    showAbout([...li.querySelectorAll(".preview p:not(.full-title)")].map(p => p.textContent));
    aboutOf(li)?.then(full => { if (full?.length && shown === li) showAbout(full); });
    const address = combined ? "" : li.dataset.address || PLACES[venue] || "";
    part("place").replaceChildren(...(address ? [address, " · ", Object.assign(document.createElement("a"), {
      href: "https://www.google.com/maps/search/?" + new URLSearchParams({ api: 1, query: venue + ", " + address }), textContent: "Map ↗" })] : []));
    // The same show (or film, anywhere) on its other days.
    const others = [...document.querySelectorAll(".day > ul > li[data-series]")].filter(other => other.dataset.series === li.dataset.series && other !== li);
    part("also").replaceChildren(...(others.length ? ["Also ", ...others.slice(0, 8).flatMap((other, i) => [i ? " · " : "",
      Object.assign(document.createElement("a"), { href: withEvent(other.dataset.id), textContent: dayName(other.closest(".day").dataset.date) })])] : []));
    for (const [i, a] of part("also").querySelectorAll("a").entries()) a.addEventListener("click", event => {
      event.preventDefault();
      history.replaceState(null, "", withEvent(others[i].dataset.id));
      fill(others[i]);
    });
    copySays("Copy link");
    view.scrollTop = 0;
  }
  function openEvent(li, push, note) {
    fill(li, note);
    if (push) { history.pushState(null, "", withEvent(li.dataset.id)); pushed = true; }
    if (!view.open) view.showModal();
    tally("open/" + (li.classList.contains("combined") ? "several theaters" : li.dataset.sources), shown.querySelector(".title").textContent);
  }
  // Closing it takes its address away: Back, where opening it added one; otherwise in place. Its own ways of
  // closing (×, Esc, a click outside it) do so at once; the close event, which comes a moment later, for any
  // other.
  let closing = false;
  const restore = () => {
    if (!eventInAddress()) return;
    if (pushed) { pushed = false; history.back(); } else history.replaceState(null, "", withEvent(null));
  };
  function closeEvent() {
    if (!view.open) return;
    closing = true;
    view.close();
    restore();
  }
  view.addEventListener("close", () => { if (closing) closing = false; else restore(); });
  view.addEventListener("cancel", event => { event.preventDefault(); closeEvent(); }); // Esc.
  part("close").addEventListener("click", closeEvent);
  part("more").addEventListener("click", () => {
    const open = part("about").classList.toggle("clamped") === false;
    part("more").textContent = open ? "Show less" : "Read more";
    part("more").setAttribute("aria-expanded", open);
  });
  // It fades in once it's all there and decoded, rather than drawing itself top to bottom.
  part("image").firstElementChild.addEventListener("load", async event => {
    const img = event.target, src = img.src;
    await img.decode().catch(() => {});
    if (img.src !== src) return; // Another listing's, by now.
    part("image").classList.toggle("whole", img.naturalWidth / img.naturalHeight < 1.3);
    part("image").classList.add("loaded");
  });
  // Its picture and what it's about, fetched as a listing's pressed, a moment before its view opens.
  document.addEventListener("pointerdown", event => {
    const li = event.target.closest?.(".day > ul > li.row");
    if (!li) return;
    if (li.dataset.image) Object.assign(new Image(), { referrerPolicy: "no-referrer", src: li.dataset.image });
    aboutOf(li);
  });
  part("image").firstElementChild.addEventListener("error", event => { if (event.target.getAttribute("src")) part("image").hidden = true; });
  view.addEventListener("click", event => { if (event.target === view) closeEvent(); }); // Outside it, on the backdrop.
  document.addEventListener("click", event => {
    const li = event.target.closest(".day > ul > li.row");
    if (!li) return;
    const a = event.target.closest("a");
    if (a && (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return; // Straight to the venue.
    event.preventDefault(); // Also keeps a film at several theaters from opening in place.
    openEvent(li, true);
  });
  addEventListener("popstate", () => {
    const id = eventInAddress();
    const li = id && rowWith(id);
    if (li) { pushed = false; openEvent(li, false); }
    else if (view.open) { pushed = false; closing = true; view.close(); } // Back, with it open: its address is gone already.
  });
  // Opened from its address, as a shared link: that listing; if it's gone (it's over), the next of its series.
  {
    const id = eventInAddress();
    if (id) {
      const li = rowWith(id);
      const next = li || [...document.querySelectorAll(".day > ul > li[data-series]")].find(other => id.endsWith("-" + other.dataset.series) && id.slice(0, 10) <= other.closest(".day").dataset.date);
      if (next) openEvent(next, false, li ? "" : "The showing you were sent has passed; this is the next one.");
      else { history.replaceState(null, "", withEvent(null)); toast("That listing has passed"); }
    }
  }

  // Copy the listing's link: its own page, which gives the link its preview (the name, when and where, and
  // its picture) wherever it's pasted. The button says so for a moment, with a check for its icon. Where the
  // clipboard can't be written to directly (some apps' own browsers), a selected copy of the link is copied.
  function copySays(label, state = "") {
    const button = part("copy");
    clearTimeout(button.timer);
    button.classList.toggle("copied", state === "copied");
    button.lastElementChild.textContent = label;
  }
  function copyText(text) {
    const field = Object.assign(document.createElement("textarea"), { value: text, readOnly: true });
    field.style.cssText = "position: fixed; opacity: 0";
    view.append(field); // In the view, which is all a page can focus while it's open.
    field.select();
    const done = document.execCommand("copy");
    field.remove();
    if (!done) throw new Error("not copied");
  }
  part("copy").addEventListener("click", async () => {
    const li = shown, button = part("copy");
    const url = SHARE_URL + "e/" + li.dataset.id + "/";
    tally("share/" + (li.classList.contains("combined") ? "several theaters" : li.dataset.sources), li.querySelector(".title").textContent.trim());
    try {
      try { await navigator.clipboard.writeText(url); } catch (error) { copyText(url); }
      copySays("Link copied", "copied");
    } catch (error) { copySays("Couldn’t copy it"); }
    button.timer = setTimeout(() => copySays("Copy link"), 2500);
  });

  // A listing followed to its venue's page, counted as the venue's: from its view, or straight from its name
  // with a modifier key or a middle click.
  const followed = event => {
    const a = event.target.closest(".event-places a, .day a.title");
    if (!a || event.button > 1 || (a.matches(".day a.title") && event.type === "click" && !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey))) return;
    const venue = a.dataset.source || (a.closest(".day") ? venueOf(a) : shown.dataset.sources);
    tally("listing/" + venue, (a.closest(".day > ul > li") || shown).querySelector(".title").textContent);
  };
  document.addEventListener("click", followed);
  document.addEventListener("auxclick", followed);
"""


# The public site's About and Contact pages: plain text, and a form as quiet as the search.
PUBLIC_CSS = """
  /* Its name in the header: Pushpin as the site's, the city after it lighter. */
  .sites .city { font-weight: 400; }
  /* The page it is, after the name: "/ Film", in white. */
  .sites .here { color: #fff; font-size: 1.15rem; font-weight: 700; letter-spacing: -.01em; }
  .sites .here::before { content: "/"; margin-right: .5em; color: #444; font-weight: 400; }
  /* Each whole, never broken over a line: on the narrowest phones the page's name goes under the site's. On a
     phone, a page with its name there leaves out when it was updated, which the home page still says. */
  .sites { flex-wrap: wrap; gap: .15rem .55rem; }
  .sites a, .sites .here { white-space: nowrap; }
  @media (max-width: 34rem) { header:has(.here:not([hidden])) .header-note { display: none; } }
  .sites .pin { width: 1.05em; height: 1.05em; margin-right: .3em; vertical-align: -.16em; }  /* In the name's own color. */
  .sites a[aria-current] .city { color: #8c8c8c; }
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
  .prose h2 + p { margin-top: -.25rem; }
  /* How to use it: short lines, bulleted, a little apart. */
  .tips { padding-left: 1.15em; list-style: disc; }
  .tips li { margin: 0 0 .6em; padding-left: .2em; }
  .tips li::marker { color: #555; }
  /* Each kind's mark before its name, as in the filter: over its venues, and in place of a bullet before its calendar. */
  .prose h2 .icon, .feeds .icon { margin-right: .45em; vertical-align: -1px; }
  .tips.feeds { padding-left: 0; list-style: none; }
  /* The venues, each a link to its events, with how many it has coming up after it in gray; in two columns,
     down the first and then the second, so they read in order. */
  .venues { columns: 13rem 2; column-gap: 1.5rem; }
  .venues li { margin-bottom: .35rem; break-inside: avoid; }
  .venues + p { margin-top: 2rem; }
  /* The questions: one to a line between faint rules, each opening to its answer, with a › that turns. */
  .faq { margin: .75rem 0 2rem; border-bottom: 1px solid #1c1c1c; }
  .faq details { border-top: 1px solid #1c1c1c; }
  .faq summary { display: flex; justify-content: space-between; gap: 1rem; padding: .75rem 0; color: #fff;
                 list-style: none; cursor: pointer; }
  .faq summary::-webkit-details-marker { display: none; }
  .faq summary::after { content: "›"; color: #666; transition: transform .15s; }
  .faq details[open] summary::after { transform: rotate(90deg); }
  .faq summary:hover::after { color: #bbb; }
  .faq details p { margin: -.25rem 0 .9rem; color: #bbb; }
  .prose .venues a { text-decoration: none; }
  .prose .venues a:hover { text-decoration: underline; text-decoration-color: #555; }
  .venues .count { margin-left: .35em; color: #666; font-size: .8em; }
  .contact { display: grid; gap: 1.1rem; margin-top: 1.5rem; }
  .contact label { display: grid; gap: .35rem; color: #888; font-size: .8rem; }
  .contact input, .contact textarea { padding: .5rem .6rem; border: 1px solid #333; border-radius: 4px; background: #0a0a0a;
                                      color: #fff; font: inherit; font-size: .95rem; }
  .contact input:focus, .contact textarea:focus { border-color: #777; outline: none; }
  /* The form's button, and after sending, the way home, which looks the same. */
  .contact button, .sent .home { justify-self: start; padding: .45rem 1.2rem; border: 1px solid #555; border-radius: 999px;
                    background: none; color: #ddd; font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer;
                    transition: border-color .15s, color .15s, background-color .15s; }
  .contact button:hover, .contact button:focus-visible, .sent .home:hover, .sent .home:focus-visible {
    border-color: #ccc; background: #151515; color: #fff; text-decoration: none; outline: none; }
  .contact button:active, .sent .home:active { background: #222; }
  .sent .home { display: inline-block; margin-top: .6rem; text-decoration: none; }
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
  /* A day's heading stays at the top of the window while its events scroll under it, until the next day's
     pushes it up and takes its place. The space around it is padding, where it's black, so the events passing
     under are hidden there too; the same space as before, in all. */
  .day > h2 { position: sticky; top: 0; z-index: 1; margin: 1.6rem 0 0; padding: .65rem 0 .5rem; background: #000;
              transition: box-shadow .15s; }
  /* While it's pinned there, a faint line under it (the same as above the footer), marking where the events
     go under; the script tells when it's pinned. */
  .day > h2.pinned { box-shadow: 0 1px 0 rgba(255, 255, 255, .09); }
  /* Until the script picks the page, show the first two days, about a page, so the whole month never flashes up. */
  main:not(.paged) .day:nth-of-type(n+3) { display: none; }
  .relative:not(:empty) { color: #fff; margin-right: .6em; }
  /* The day of the week in white, as Today and Tomorrow are; after those, it's in gray with the date. */
  .weekday { color: #fff; }
  .relative:not(:empty) + .date .weekday { color: inherit; }
  /* A source that didn't load, at the foot of the page, marked by a small circled exclamation point. */
  .notice { display: flex; align-items: baseline; gap: .45em; }
  .notice .icon { flex: none; align-self: center; width: 11px; height: 11px; color: #777; }
  .times, .detail { margin-left: .6em; color: #666; font-size: .8em; white-space: nowrap; }
  .times { flex: none; }
  /* A sold-out time, struck through; a listing with nothing left, a quiet tag after its times. */
  .times time.sold { color: #555; text-decoration: line-through; text-decoration-color: #555; }
  .sold-tag { flex: none; display: inline-block; align-self: center; margin-left: .6em; padding: .05rem .45rem; border: 1px solid #333; border-radius: 999px; color: #999;
              font-size: .72rem; font-weight: 500; line-height: 1.4; white-space: nowrap; }
  /* A listing opens its own view (below) wherever it's clicked. */
  .day > ul > li.row { cursor: pointer; }
  .detail { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  /* A listing's own view: over the list on a wide screen, a sheet up from the bottom on a phone. Its way to the
     venue's page (for tickets) first and plainest; its times, each adding that showing to a calendar. */
  body:has(dialog.event[open]) { overflow: hidden; }
  dialog.event { width: min(34rem, calc(100% - 2rem)); max-height: min(85vh, 46rem); box-sizing: border-box; overflow: auto;
                 padding: 1.4rem 1.5rem 1.5rem; border: 1px solid #262626; border-radius: 14px; background: #0b0b0b; color: #ddd;
                 font-size: .95rem; line-height: 1.5; }
  dialog.event::backdrop { background: rgba(0, 0, 0, .72); }
  dialog.event[open] { animation: rise .18s ease-out; }
  @keyframes rise { from { opacity: 0; transform: translateY(.75rem); } }
  .event-close { position: absolute; top: .85rem; right: .85rem; display: grid; place-items: center; width: 2rem; height: 2rem;
                 padding: 0; border: 0; border-radius: 50%; background: none; color: #888; cursor: pointer;
                 transition: background-color .15s, color .15s; }
  .event-close:hover, .event-close:focus-visible { background: #262626; color: #fff; outline: none; }
  .event-close:focus-visible { box-shadow: 0 0 0 2px #888; }
  .close-mark { width: 14px; height: 14px; }
  /* Its picture across the top, edge to edge; a tall or square one whole, on grey. */
  .event-image { aspect-ratio: 16 / 9; margin: -1.4rem -1.5rem 1.1rem; overflow: hidden; border-radius: 13px 13px 0 0; background: #151515; }
  .event-image img { display: block; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity .35s ease-out; }
  .event-image.loaded img { opacity: 1; }
  .event-image:not(.loaded) { background: linear-gradient(100deg, #151515 40%, #1d1d1d 50%, #151515 60%) 0 0 / 250% 100%;
                              animation: shimmer 1.6s linear infinite; }
  @keyframes shimmer { from { background-position: 100% 0; } to { background-position: 0 0; } }
  @media (prefers-reduced-motion: reduce) {
    .event-image img { transition: none; }
    .event-image:not(.loaded) { animation: none; }
  }
  .event-image.whole img { object-fit: contain; }
  dialog.event:has(.event-image:not([hidden])) .event-close { z-index: 1; background: rgba(0, 0, 0, .6); color: #fff; }
  dialog.event:has(.event-image:not([hidden])) .event-close:hover,
  dialog.event:has(.event-image:not([hidden])) .event-close:focus-visible { background: rgba(0, 0, 0, .9); }
  .event-facts { display: flex; flex-wrap: wrap; gap: .4rem; margin: .45rem 0 .5rem; }
  .event-facts:empty { display: none; }
  .event-fact { padding: .15rem .6rem; border-radius: 999px; background: #1c1c1c; color: #eee; font-size: .8rem; font-weight: 600;
                font-variant-numeric: tabular-nums; }
  .event-where { display: flex; align-items: center; gap: .45em; margin: 0 2.5rem .5rem 0; color: #888; font-size: .8rem; }
  .event-where .icon { width: 12px; height: 12px; }
  .event-title { margin: 0 2rem .3rem 0; color: #fff; font-size: 1.35rem; font-weight: 700; letter-spacing: -.01em;
                 line-height: 1.25; text-transform: none; }
  .event-detail, .event-note { margin: 0 0 .3rem; color: #999; font-size: .85rem; }
  .event-detail:empty, .event-note:empty, .event-place:empty, .event-also:empty, .event-places:empty { display: none; }
  .event-note { color: #bbb; }
  /* Each place it's at, a link to its page (for tickets), with its times to the right, going on to another line
     there, not under it, when there are more than fit. */
  .event-time { color: #ddd; font-size: .9rem; font-variant-numeric: tabular-nums; }
  .event-time.sold { color: #666; text-decoration: line-through; }
  .event-fact.sold { background: none; box-shadow: inset 0 0 0 1px #3a3a3a; color: #bbb; }
  .event-places { margin: 1.1rem 0 1rem; }
  .event-group { display: flex; flex-wrap: wrap; gap: .2rem .9rem; }
  .event-places li { display: grid; grid-template-columns: auto 1fr; align-items: baseline; gap: .45rem .75rem; padding: .55rem 0;
                     border-top: 1px solid #1c1c1c; }
  .event-places .event-group { justify-content: flex-end; }
  .event-places li:last-child { border-bottom: 1px solid #1c1c1c; }
  .event-places a { color: #fff; font-weight: 600; }
  .event-about p { margin: 0 0 .8em; color: #bbb; }
  .event-about.clamped { max-height: 7.5em; overflow: hidden; /* About five lines. */
                         -webkit-mask-image: linear-gradient(#000 calc(100% - 3em), transparent);
                         mask-image: linear-gradient(#000 calc(100% - 3em), transparent); }
  .event-more { display: block; margin: -.1rem 0 .2rem; padding: 0; border: 0; background: none; color: #ccc; font: inherit;
                font-size: .85rem; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; cursor: pointer; }
  .event-more:hover, .event-more:focus-visible { color: #fff; text-decoration-color: #aaa; outline: none; }
  .event-place, .event-also { margin: .9rem 0 0; color: #888; font-size: .85rem; }
  .event-place a, .event-also a { color: #ccc; text-decoration: underline; text-decoration-color: #555; text-underline-offset: .2em; }
  .event-copy { display: inline-flex; align-items: center; gap: .35rem; margin-top: 1.2rem; padding: .4rem .9rem; border: 1px solid #333;
                border-radius: 999px; background: none; color: #ddd; font: inherit; font-size: .9rem; cursor: pointer; }
  .event-copy:hover, .event-copy:focus-visible, .event-copy.copied { border-color: #888; color: #fff; outline: none; }
  .link-mark, .check-mark { width: 15px; height: 15px; }
  .event-copy .check-mark, .event-copy.copied .link-mark { display: none; }
  .event-copy.copied .check-mark { display: block; color: #4ade80; }
  @media (max-width: 34rem) {
    dialog.event { width: 100%; max-width: 100%; max-height: 88vh; margin: auto 0 0; padding: 1.25rem 1.25rem 1.5rem;
                   border-width: 1px 0 0; border-radius: 16px 16px 0 0; }
    .event-image { margin: -1.25rem -1.25rem 1rem; border-radius: 15px 15px 0 0; }
    .event-close { top: .75rem; right: .75rem; width: 2.5rem; height: 2.5rem; } /* A bigger target for a thumb. */
    .close-mark { width: 17px; height: 17px; }
    dialog.event[open] { animation: sheet .22s ease-out; }
    @keyframes sheet { from { transform: translateY(100%); } }
  }
  /* That a listing sent in a link has passed: a note at the foot of the window for a moment. */
  .toast { position: fixed; z-index: 3; left: 50%; bottom: 1.5rem; margin: 0; padding: .5rem .9rem; transform: translate(-50%, .5rem);
           border: 1px solid #333; border-radius: 999px; background: #111; color: #ddd; font-size: .85rem;
           opacity: 0; pointer-events: none; transition: opacity .15s, transform .15s; }
  .toast.shown { opacity: 1; transform: translate(-50%, 0); }
  /* A film at several places: the row opens to each place's times, with a › that turns when it's open. */
  .row.combined { display: block; }
  .combined summary { display: grid; grid-template-columns: 10rem 1fr; gap: 1.25rem; align-items: baseline;
                      list-style: none; cursor: pointer; }
  .combined summary::-webkit-details-marker { display: none; }
  .combined summary:hover .title { text-decoration: underline; }
  /* No preview on hover: a click opens the listing's view, which has it all. Rows keep theirs, unseen, for the
     view to show at once and for the search. */
  .day .preview { display: none; }
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
        "image": item.get("image", ""),
        "price": item.get("price", ""),
        "ages": item.get("ages", ""),
        "sold_out": item.get("sold_out", False),
        "sold_out_times": item.get("sold_out_times", []),
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

    sources = [source for city in CITIES for source in read_sources(city.sources, city.slug)]
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
    # The personal events page: Boston's. Its listings.json keeps every city's, for falling back on.
    personal = by_city(BOSTON_CITY, events, sources, failed, stale)
    (OUT_DIR / "index.html").write_text(render_index(*personal, built_at))
    record = {"built": built_at.isoformat(), "sources": listings, "failing": still_failing(errors, previous, built_at)}
    (OUT_DIR / "listings.json").write_text(json.dumps(record, ensure_ascii=False))
    print(f"Wrote {OUT_DIR.relative_to(ROOT.parent)}/index.html with {len(personal[0])} listings, and listings.json")

    # Each city's public site, from the same listings; then the site's own root.
    for city in CITIES:
        use_city(city)
        write_city(city, *by_city(city, events, sources, failed, stale), built_at)
    use_city(BOSTON_CITY)
    shutil.copytree(ROOT / "pushpin", PUBLIC_DIR, dirs_exist_ok=True)
    shutil.copytree(ROOT / "share" / "site", PUBLIC_DIR / "share", dirs_exist_ok=True)
    (PUBLIC_DIR / "index.html").write_text(render_cities(CITIES))
    (PUBLIC_DIR / "404.html").write_text(render_not_found(CITIES))
    # robots.txt, which only works at the site's root: each city's sitemap.
    (PUBLIC_DIR / "robots.txt").write_text("User-agent: *\nAllow: /\n\n" + "".join(
        f"Sitemap: {PUBLIC_SITE}{city.slug}/sitemap.xml\n" for city in CITIES))
    print(f"Wrote {PUBLIC_DIR.relative_to(ROOT.parent)}: the cities, robots.txt and 404.html")


def by_city(city, events, sources, failed, stale):
    """A city's share of the build: its events and sources, and which of them failed or are from before."""
    names = {source["name"] for source in sources if source["city"] == city.slug}
    return ([item for item in events if item["source"] in names], [source for source in sources if source["name"] in names],
            [name for name in failed if name in names], [(name, fetched) for name, fetched in stale if name in names])


def write_city(city, events, sources, failed, stale, built_at):
    """A city's public site, in its folder (CITY_DIR), with use_city() pointing the PUBLIC_ names at it."""
    CITY_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", CITY_DIR, dirs_exist_ok=True)
    shutil.copytree(ROOT / "pushpin", CITY_DIR, dirs_exist_ok=True)  # Its own icons, over the events page's.
    shutil.copytree(ROOT / "share" / city.slug, CITY_DIR / "share", dirs_exist_ok=True)
    (CITY_DIR / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True))
    for page, render in (("about", lambda: render_about(sources, built_at, events)), ("contact", render_contact)):
        (CITY_DIR / page).mkdir(exist_ok=True)
        (CITY_DIR / page / "index.html").write_text(render())
        if city is BOSTON_CITY:  # Where they were first, as about.html and contact.html, on to where they are.
            (CITY_DIR / f"{page}.html").write_text(render_redirect(f"{PUBLIC_URL}{page}/"))
    for category, (slug, *_) in PUBLIC_PAGES.items():
        (CITY_DIR / slug).mkdir(exist_ok=True)
        (CITY_DIR / slug / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True, category=category))
    (CITY_DIR / PUBLIC_TONIGHT[0]).mkdir(parents=True, exist_ok=True)
    (CITY_DIR / PUBLIC_TONIGHT[0] / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True, tonight=True))
    for ahead, (path, *_) in enumerate(PUBLIC_WEEKENDS):
        (CITY_DIR / path).mkdir(parents=True, exist_ok=True)
        (CITY_DIR / path / "index.html").write_text(render_index(events, sources, failed, stale, built_at, public=True, weekend=ahead))
    (CITY_DIR / "sitemap.xml").write_text(render_sitemap(built_at))
    # Each listing's own page, for the preview a shared link to it shows.
    for path, page in event_pages(sources, events).items():
        (CITY_DIR / path).parent.mkdir(parents=True, exist_ok=True)
        (CITY_DIR / path).write_text(page)
    (CITY_DIR / "calendar").mkdir(exist_ok=True)
    for path, feed in calendar_feeds(sources, events, built_at).items():
        (CITY_DIR / path).write_bytes(feed.encode())  # As written: its lines end \r\n, as the format asks.
    print(f"Wrote {CITY_DIR.relative_to(ROOT.parent)}: index.html, {', '.join(slug + '/' for slug, *_ in PUBLIC_PAGES.values())}, tonight/, weekend/, weekend/next/, "
          f"about/, contact/ and sitemap.xml, with {len(events)} listings")


if __name__ == "__main__":
    main()
