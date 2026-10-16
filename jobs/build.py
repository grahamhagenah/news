#!/usr/bin/env python3
"""Build the job board: web and design jobs in a handful of metro areas, and remote ones, newest first. The
filter is the kind of work; where it's worked is the city at the end of a row, which "Remote" is one of.

Sources are in sources.txt, the titles worth listing in keywords.txt, and what the jobs at the top of
the page are scored on in fit.txt. Run it from the repo's top folder:
`python3 -m jobs.build` writes dist/jobs.
"""

import html
import json
import os
import re
import shutil
import sys
import threading
import unicodedata
import urllib.error
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from shared import site as shared

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.txt"
KEYWORDS_FILE = ROOT / "keywords.txt"
FIT_FILE = ROOT / "fit.txt"
OUT_DIR = ROOT.parent / "dist" / "jobs"
# Whose board this is, so a copy of it points at its own repo and page (as the newsfeed does).
REPO = os.environ.get("GITHUB_REPOSITORY") or "grahamhagenah/news"
REPO_URL = f"https://github.com/{REPO}"
OWNER, _, NAME = REPO.partition("/")
# Where the board is published: GitHub Pages' own address for the repo it's pushed to, unless JOBS_URL names
# a domain of its own.
SITE_URL = (os.environ.get("JOBS_URL") or f"https://{OWNER}.github.io/jobs/").rstrip("/") + "/"
BOARD_URL = SITE_URL + "board.json"

EASTERN = ZoneInfo("America/New_York")
PAGE_SIZE = 30  # Jobs per page of the list.
DAYS_TO_KEEP = 30  # A posting older than this has usually gone.
# Except on a university or higher-ed board, which leaves a posting up until the job is filled — often
# months. These are read as far back as this instead, and still sort by their date like everything else.
DAYS_TO_KEEP_SLOW = 120
SLOW_KINDS = ("madgex", "jibe", "silkroad", "workday", "greenhouse")
# At most this many from one source, so no board can crowd out the rest. It's a guard rather than a trim:
# at 120 it was cutting Inside Higher Ed, which is a board worth reading in full, and the sources here are
# each bounded by how much they're asked for (a Madgex board, the most of them, can return 160).
PER_SOURCE = 200
PREVIEW_CHARS = 600
# A source that fails falls back to its jobs from the last build, if they're no older than this.
FALLBACK_LIMIT = timedelta(days=2)
USER_AGENT = "Mozilla/5.0 (compatible; rss-reader/1.0)"

# The metro areas, in the order the menu names them, and the places that belong to each. A job's own city is
# whichever source listed it; these are for saying where it is when a source covers a region (Built In
# Colorado lists Boulder as well as Denver).
CITIES = {
    "boston": "Boston",
    "newyork": "New York",
    "washingtondc": "Washington, DC",
    "bayarea": "Bay Area",
    "seattle": "Seattle",
    "denver": "Denver",
    "chicago": "Chicago",
    "austin": "Austin",
    "losangeles": "Los Angeles",
    # Where the universities and institutions read here actually are. Each of these held three jobs or more
    # the day it was named, which is the bar: a metro nobody posts in is a filter that never does anything.
    "pittsburgh": "Pittsburgh",
    "baltimore": "Baltimore",
    "stlouis": "St. Louis",
    "columbus": "Columbus",
    "sandiego": "San Diego",
    "rochester": "Rochester",
    "providence": "Providence",
    "westernmass": "Western Mass",
    "atlanta": "Atlanta",
    "madison": "Madison",
    "remote": "Remote",
    # Boards that list jobs anywhere (a university in a college town, a non-profit in one city) put the ones
    # outside every metro here, under their own place.
    "elsewhere": "Elsewhere",
}

# The towns that belong to each metro, for a source whose city is "anywhere": its listing's own location
# decides which metro the job is filed under. Town names only, since a state is far too big to be a metro —
# a job in Amherst isn't a job in Boston — and a name that is a town in two metros (Westwood, Georgetown)
# is left out or given its state.
METROS = {
    "boston": ("boston", "cambridge, ma", "somerville, ma", "brookline", "newton, ma", "waltham", "medford, ma",
               "quincy, ma", "woburn", "needham", "dedham", "chestnut hill", "charlestown", "malden",
               "watertown, ma", "wellesley", "framingham", "braintree", "chelsea, ma",
               "salem, ma", "lynn, ma", "beverly, ma", "burlington, ma", "lexington, ma"),
    "newyork": ("new york", "brooklyn", "queens, ny", "bronx", "manhattan", "staten island", "long island city",
                "jersey city", "hoboken", "newark, nj", "yonkers", "white plains", "new rochelle"),
    # The federal government's own metro, which is most of what USAJOBS carries. "Washington" alone is a
    # state as well as a city, so it's only ever given with what follows it.
    "washingtondc": ("washington, dc", "washington, district of columbia", "arlington, va", "alexandria, va",
                     "bethesda", "silver spring", "rockville", "mclean", "reston", "falls church",
                     "fairfax, va", "college park, md", "suitland", "fort belvoir", "quantico", "langley, va"),
    "bayarea": ("san francisco", "oakland", "berkeley", "san jose", "palo alto", "mountain view", "menlo park",
                "santa clara", "sunnyvale", "redwood city", "san mateo", "fremont", "stanford", "emeryville",
                "cupertino", "hayward", "walnut creek", "santa cruz"),
    "seattle": ("seattle", "bellevue, wa", "redmond", "kirkland", "tacoma", "everett", "renton", "bothell"),
    "denver": ("denver", "boulder", "aurora, co", "lakewood, co", "littleton", "englewood, co", "broomfield",
               "golden, co", "fort collins"),
    "chicago": ("chicago", "evanston", "naperville", "oak park", "skokie", "schaumburg", "elgin, il",
                "des plaines", "hyde park campus"),
    "austin": ("austin", "round rock", "cedar park", "pflugerville", "san marcos, tx"),
    "losangeles": ("los angeles", "pasadena, ca", "santa monica", "long beach", "burbank", "glendale, ca",
                   "culver city", "irvine", "northridge", "malibu", "claremont", "pomona", "torrance"),
    # A university often writes a campus or a building where a company writes a town — "Columbus Campus",
    # "Hyde Park Campus", "Smith College". The ones the boards read here use are listed with their towns.
    "pittsburgh": ("pittsburgh", "carnegie mellon"),
    "baltimore": ("baltimore", "johns hopkins", "towson", "catonsville"),
    "stlouis": ("st. louis", "st louis", "saint louis", "washington university", "clayton, mo",
                "university city, mo"),
    "columbus": ("columbus, oh", "columbus campus"),
    "sandiego": ("san diego", "la jolla"),
    "rochester": ("rochester, ny", "rit main location", "henrietta, ny"),
    "providence": ("providence", "pawtucket", "barus & holley", "south street landing"),
    "westernmass": ("northampton", "amherst, ma", "springfield, ma", "holyoke", "south hadley", "longmeadow",
                    "greenfield, ma", "chicopee", "smith college", "mount holyoke"),
    "atlanta": ("atlanta", "decatur, ga", "marietta"),
    "madison": ("madison, wi",),
}
# How a place writes the states our metros are in, for matching a town given as "Medford, Massachusetts"
# against METROS' "medford, ma". Only a state after a comma is shortened, so the town in "New York, New York"
# is left alone.
STATES = {"massachusetts": "MA", "new york": "NY", "new jersey": "NJ", "california": "CA", "washington": "WA",
          "colorado": "CO", "illinois": "IL", "texas": "TX", "ohio": "OH", "wisconsin": "WI",
          "missouri": "MO", "pennsylvania": "PA", "maryland": "MD", "rhode island": "RI", "georgia": "GA"}
STATE_AFTER_TOWN = re.compile(rf",\s*({'|'.join(STATES)})(?!\w)", re.I)
# Some boards join the two with a hyphen instead: "Somerville-MA".
STATE_AFTER_HYPHEN = re.compile(r"(?<=\w)-([A-Z]{2})(?!\w)")
# A place that says the job is done from wherever you are, rather than naming somewhere.
REMOTE_PLACE = re.compile(r"(?<!\w)(remote|anywhere|work from home|telecommut\w*|virtual)", re.I)
# A place that reads as one, for a row to show in place of a metro's name: "Atlanta, Georgia", "Madison, WI".
PLACE_LIKE = re.compile(r"^[A-Z][\w .'’/-]+,\s*[A-Za-z][\w .]*$")


def read_sources():
    """sources.txt's lines: how to read the source, its URL, the city its jobs are in, and the board's name."""
    sources = []
    for line in SOURCES_FILE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            kind, url, city, *name = line.split()
            if city not in CITIES and city != "anywhere":
                sys.exit(f"sources.txt: {city} isn't one of the cities ({', '.join(CITIES)}, anywhere)")
            sources.append({"kind": kind, "url": url, "city": city, "name": " ".join(name)})
    return sources


def read_keywords():
    """keywords.txt as one pattern: any of its words or phrases, as whole words, in any case."""
    lines = [line.strip() for line in KEYWORDS_FILE.read_text().splitlines()]
    words = [line for line in lines if line and not line.startswith("#")]
    return re.compile("|".join(rf"(?<!\w){re.escape(word)}(?!\w)" for word in words), re.I)


def read_fit():
    """fit.txt: what a job is scored on. Its sections are the patterns read in a title, the ones read in the
    posting, the kinds of board that count as the sector this board is for, and the plain numbers. A rule
    line is its points, the word a row shows, a colon, then the pattern — or, for a sector, the board kinds
    that earn it."""
    sections, section = {"title": [], "body": [], "sector": [], "numbers": []}, None
    for line in FIT_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section not in sections:
                sys.exit(f"fit.txt: {section} isn't one of its sections ({', '.join(sections)})")
        elif section is None:
            sys.exit(f"fit.txt: {line} comes before any [section]")
        elif section == "numbers":
            name, _, count = line.rpartition(" ")
            sections["numbers"].append((name.strip(), int(count)))
        else:
            worth, _, rest = line.partition(" ")
            label, _, pattern = rest.partition(":")
            sections[section].append((int(worth), label.strip(), pattern.strip()))
    for_, against = ([rule for rule in sections["title"] if (rule[0] > 0) == want] for want in (True, False))
    sectors = {kind: (worth, label) for worth, label, kinds in sections["sector"] for kind in kinds.split()}
    return (tuple((worth, label, pattern) for worth, label, pattern in for_),
            tuple((-worth, label, pattern) for worth, label, pattern in against),
            tuple((label, pattern) for _, label, pattern in sections["body"]),
            sectors, dict(sections["numbers"]))


_fetched, _fetching = {}, threading.Lock()


def fetch(url, attempts=3, timeout=20, body=None, headers=None):
    """The page or data at url, as text, downloaded once a build however many times it's read. With body
    (JSON), it's asked for by POST, which is how a Workday board answers; headers are the ones a board asks
    for beyond the usual."""
    key = (url, body)
    with _fetching:
        pending, first = _fetched.get(key), key not in _fetched
        if first:
            pending = _fetched[key] = Future()
    if first:
        try:
            pending.set_result(shared.fetch(
                url, USER_AGENT, attempts, timeout,
                body=body.encode() if body else None,
                content_type="application/json" if body else None,
                extra=headers)[1].decode("utf-8", "replace"))
        except Exception as error:
            pending.set_exception(error)
    return pending.result()


def text(markup):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup or ""))).strip()


def about(markup, limit=PREVIEW_CHARS):
    """What a board says about a job, as a few short paragraphs for its preview."""
    parts = re.split(r"\n\s*\n|</p>|<br\s*/?>", markup or "")
    kept, budget = [], limit
    for part in parts:
        line = text(part)
        if len(line.split()) < 4:
            continue
        kept.append(line if len(line) <= budget else line[:budget].rsplit(" ", 1)[0] + "…")
        budget -= len(kept[-1])
        if budget <= 0 or len(kept) == 3:
            break
    return kept


def metro_of(place, arrangement=""):
    """Which metro a place is in: the first of METROS it names, or remote when it names nowhere at all. A
    place in none of them is "elsewhere", which a row shows under the place's own name."""
    # "San José" is San Jose, so the accents come off before anything is matched.
    plain = "".join(mark for mark in unicodedata.normalize("NFD", f"{place} {arrangement}")
                    if not unicodedata.combining(mark))
    said = STATE_AFTER_TOWN.sub(lambda found: ", " + STATES[found.group(1).casefold()], plain)
    said = STATE_AFTER_HYPHEN.sub(r", \1", said)
    for city, towns in METROS.items():
        if any(re.search(rf"(?<!\w){re.escape(town)}(?!\w)", said, re.I) for town in towns):
            return city
    return "remote" if REMOTE_PLACE.search(said) else "elsewhere"


def job(source, title, company, link, posted, place="", salary="", arrangement="", level="", about_=()):
    """One listing. posted is when the board says it went up; place is where it is in the source's own words
    ("Boston, MA", "Anywhere in the World"), which is finer than the city it's filed under."""
    return {
        "title": title,
        "company": company or source["name"],
        "link": link,
        "posted": posted,
        # A board that lists jobs anywhere gives each one its own city, from the place it names.
        "city": source["city"] if source["city"] != "anywhere" else metro_of(place, arrangement),
        "place": place,
        "salary": salary,
        "arrangement": arrangement,
        "level": level,
        "about": [line for line in about_ if line.casefold() != title.casefold()],
        "source": source["name"],
        # How its board is read, which is the nearest thing to what kind of employer it is.
        "board": source["kind"],
    }


# Built In writes each job as a card, and the list of them again as schema.org data whose descriptions are
# the tidiest summary of each. Its script tag's type is HTML-escaped, so it isn't found by looking for
# `type="application/ld+json"`.
# The categories worth reading: the two the board started with, plus the ones the wider keywords are
# for. Marketing, operations, HR and customer success were tried and gave almost nothing the keywords
# wanted, so they aren't fetched every build.
BUILTIN_PAGES = ("jobs/dev-engineering", "jobs/design-ux", "jobs/product", "jobs/data-analytics",
                 "jobs/content")
BUILTIN_CARD = re.compile(r'(?=<div[^>]*data-id="job-card")')
BUILTIN_TITLE = re.compile(r'<a href="(/job/[^"]+)"[^>]*data-id="job-card-title"[^>]*>(.*?)</a>', re.S)
# A card links the company twice, around its logo and around its name; the name is the one with words in it.
BUILTIN_COMPANY = re.compile(r'href="/company/[^"]*"[^>]*>(.*?)</a>', re.S)
BUILTIN_LD = re.compile(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', re.S)
# "8 Days Ago", "Yesterday", and a posting put back up: "Reposted 2 Days Ago".
BUILTIN_AGE = re.compile(r"^(?:reposted\s+)?(?:(\d+)\s+(hour|day|minute)s?\s+ago|yesterday|today)$", re.I)
BUILTIN_PLACE = re.compile(r"^[A-Z][\w .'’-]+, [A-Z]{2}$")
BUILTIN_SALARY = re.compile(r"^\$?[\d,.]+K?\s*[-–]\s*\$?[\d,.]+K?\s+(?:Annually|Hourly)$", re.I)
BUILTIN_LEVEL = re.compile(r"^(Entry|Junior|Mid|Senior|Expert|Executive)\s+level$", re.I)
BUILTIN_ARRANGEMENT = ("Remote", "Hybrid", "In-Office")


def builtin_age(said, today):
    """"8 Days Ago", "Yesterday" and the rest as the day it went up."""
    found = BUILTIN_AGE.match(said.strip())
    if not found:
        return None
    if found.group(1) is None:
        return today - timedelta(days=1) if said.strip().casefold().endswith("yesterday") else today
    count, unit = int(found.group(1)), found.group(2).lower()
    return today - timedelta(days=count if unit == "day" else 0)


def builtin_descriptions(page):
    """Each job's description on the page, by its address."""
    descriptions = {}
    for block in BUILTIN_LD.findall(page):
        try:
            data = json.loads(html.unescape(block))
        except json.JSONDecodeError:
            continue
        for part in data.get("@graph", [data]):
            for item in part.get("itemListElement", []) if isinstance(part, dict) else []:
                if item.get("url") and item.get("description"):
                    descriptions[item["url"].split("?")[0]] = item["description"]
    return descriptions


def read_builtin(source):
    """A Built In metro site's listings, from each of BUILTIN_PAGES: a card for each job, with the company,
    where it is, what it pays and how it's worked (remote, hybrid, in the office). A job on two of its
    category pages is read twice and merged later, as a job in two metros is."""
    today = datetime.now(EASTERN).date()
    listings = []
    for path in BUILTIN_PAGES:
        page = fetch(urljoin(source["url"], path))
        descriptions = builtin_descriptions(page)
        for card in BUILTIN_CARD.split(page)[1:]:
            found = BUILTIN_TITLE.search(card)
            if not found:
                continue
            link = urljoin(source["url"], found.group(1))
            company = next((text(name) for name in BUILTIN_COMPANY.findall(card) if text(name)), "")
            # The card as its lines of text, which is steadier than the classes they're wrapped in.
            lines = [line.strip() for line in re.split(r"<[^>]+>", card) if line.strip()]
            lines = [html.unescape(line) for line in lines]
            posted = next((builtin_age(line, today) for line in lines if BUILTIN_AGE.match(line)), None)
            listings.append(job(
                source, text(found.group(2)), company, link, posted,
                place=next((line for line in lines if BUILTIN_PLACE.match(line)), ""),
                salary=next((line for line in lines if BUILTIN_SALARY.match(line)), ""),
                arrangement=next((line for line in lines if line in BUILTIN_ARRANGEMENT), ""),
                level=next((line.split()[0] for line in lines if BUILTIN_LEVEL.match(line)), ""),
                about_=about(descriptions.get(link.split("?")[0], "")),
            ))
    return listings


def read_wwr(source):
    """We Work Remotely's feed for a category. Its titles are "Company: Role", and each item says which part
    of the world the job is open to."""
    import xml.etree.ElementTree as ET

    listings = []
    for item in ET.fromstring(fetch(source["url"])).iter("item"):
        title = text(item.findtext("title"))
        company, _, role = title.partition(": ")
        if not role:
            company, role = "", title
        posted = item.findtext("pubDate")
        listings.append(job(
            source, role, company, (item.findtext("link") or "").strip(),
            parsedate_to_datetime(posted).date() if posted else None,
            place=text(item.findtext("region")), arrangement="Remote",
            about_=about(item.findtext("description") or ""),
        ))
    return listings


def read_remoteok(source):
    """Remote OK's JSON. Its first item is its terms rather than a job; its links want the site's own address,
    which is where a reader is sent (its terms ask for the link back)."""
    listings = []
    for item in json.loads(fetch(source["url"])):
        if not item.get("position"):
            continue
        posted = item.get("date") or ""
        listings.append(job(
            source, text(item["position"]), text(item.get("company", "")),
            item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('slug', '')}",
            datetime.fromisoformat(posted).date() if posted else None,
            place=text(item.get("location", "")) or "Anywhere", arrangement="Remote",
            salary=salary_range(item.get("salary_min"), item.get("salary_max")),
            about_=about(item.get("description", "")),
        ))
    return listings


def read_remotive(source):
    """Remotive's JSON for a category."""
    listings = []
    for item in json.loads(fetch(source["url"])).get("jobs", []):
        posted = (item.get("publication_date") or "").replace(" ", "T")
        listings.append(job(
            source, text(item.get("title", "")), text(item.get("company_name", "")), item.get("url", ""),
            datetime.fromisoformat(posted).date() if posted else None,
            place=text(item.get("candidate_required_location", "")) or "Anywhere", arrangement="Remote",
            salary=text(item.get("salary", "")), about_=about(item.get("description", "")),
        ))
    return listings


# Inside Higher Ed and the Chronicle run the same job board software, whose RSS answers a keyword search with
# the 20 newest postings that match. One query is too narrow a window on a board carrying 20,000 of them, so
# each board is asked several, and keywords.txt still decides what's kept.
#
# These ten were chosen by asking both boards eighteen candidates and keeping the ones that found jobs the
# others didn't: together they hold 89% of everything the eighteen found between them. "web developer" and
# "user experience" were dropped from the set for that reason — near enough all of what they return comes
# back under "web manager", "web content" and "ux designer" anyway.
#
# Two more were dropped for what they found rather than how much: "instructional designer" is e-learning and
# curriculum work, and "accessibility" on its own is a university's disability services — testing centres,
# accommodations, resource centres — rather than the accessibility of what's on its website. The narrower
# "digital accessibility" finds that work, and keywords.txt still keeps a job whose title says accessibility
# however it was found.
MADGEX_QUERIES = ("web manager", "graphic designer", "software engineer", "data analyst", "ux designer",
                  "digital accessibility", "front end developer", "web content")
# Its description is the pay (when it gives one), the employer, the posting itself, then the place, each on
# its own line. The pay line is the one starting with a number: "53,142 - 88600:", "Up to GBP 75,000:".
MADGEX_PAY = re.compile(r"^[^A-Za-z]*\d")


def read_madgex(source):
    """A higher-ed or non-profit board running Madgex: its RSS for each of MADGEX_QUERIES. A title reads
    "Institution: Role", and the description's last line is where the job is."""
    import xml.etree.ElementTree as ET

    listings = []
    for query in MADGEX_QUERIES:
        feed = fetch(f"{urljoin(source['url'], 'jobsrss/')}?countrycode=US&keywords={quote(query)}")
        for item in ET.fromstring(feed).iter("item"):
            title = text(item.findtext("title"))
            company, _, role = title.partition(": ")
            if not role:
                company, role = "", title
            lines = [line.strip() for line in (item.findtext("description") or "").splitlines() if line.strip()]
            pay = lines.pop(0).rstrip(":").strip() if lines and MADGEX_PAY.match(lines[0]) else ""
            # The employer again, which the title has already said.
            if lines and lines[0].rstrip(":").casefold() == company.casefold():
                lines.pop(0)
            posted = item.findtext("pubDate")
            listings.append(job(
                source, role, company,
                # Without the feed's own tracking, which is as long again as the address itself.
                (item.findtext("link") or "").strip().split("?")[0],
                parsedate_to_datetime(posted).date() if posted else None,
                place=lines[-1] if len(lines) > 1 else "", salary=pay,
                about_=about(" ".join(lines[:-1] if len(lines) > 1 else lines)),
            ))
    return listings


# Tech Jobs for Good writes each job as a card, newest first, 30 to a page.
TECHJOBS_PAGES = ("jobs/", "jobs/?page=2")
TECHJOBS_CARD = re.compile(r'(?=<div class="ui raised fluid card job-card")')
TECHJOBS_LINK = re.compile(r'href="(/jobs/\d+/[^"]*)"')
TECHJOBS_TITLE = re.compile(r'class="header job-title"\s*title="([^"]*)"')
TECHJOBS_COMPANY = re.compile(r'class="meta company-name"\s*title="([^"]*)"')
TECHJOBS_PLACE = re.compile(r'<span class="location"\s*title="([^"]*)"')
TECHJOBS_PAY = re.compile(r'<span class="salary"\s*title="([^"]*)"')
TECHJOBS_AGE = re.compile(r'class="date-posted">\s*Posted\s*([^<]*?)\s*</span>')
TECHJOBS_ABOUT = re.compile(r'class="content job-snippet"[^>]*>(.*?)</div>', re.S)


def first(pattern, card):
    found = pattern.search(card)
    return text(found.group(1)) if found else ""


def techjobs_age(said, today):
    """How old a card says its job is: "1 day ago", "3 weeks ago", "today"."""
    found = re.match(r"(?:(\d+)\s+)?(minute|hour|day|week|month)s?\s+ago", said.strip(), re.I)
    if not found:
        return today if "today" in said.casefold() else None
    days = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30}[found.group(2).lower()]
    return today - timedelta(days=int(found.group(1) or 1) * days)


def read_techjobs(source):
    """Tech Jobs for Good's listings: non-profit and public-good work, a card for each job with the company,
    where it is (several places, sometimes, and whether it's hybrid), what it pays and how old it is."""
    today = datetime.now(EASTERN).date()
    listings = []
    for path in TECHJOBS_PAGES:
        page = fetch(urljoin(source["url"], path))
        for card in TECHJOBS_CARD.split(page)[1:]:
            found, link = TECHJOBS_TITLE.search(card), TECHJOBS_LINK.search(card)
            if not (found and link):
                continue
            place = first(TECHJOBS_PLACE, card)
            about_ = TECHJOBS_ABOUT.search(card)
            listings.append(job(
                source, text(found.group(1)), first(TECHJOBS_COMPANY, card),
                urljoin(source["url"], link.group(1).split("?")[0]),
                techjobs_age(first(TECHJOBS_AGE, card), today),
                place=place, salary=first(TECHJOBS_PAY, card),
                arrangement=("Hybrid" if "hybrid" in place.casefold()
                             else "Remote" if REMOTE_PLACE.search(place) else ""),
                about_=about(about_.group(1) if about_ else ""),
            ))
    return listings


# A university's own HR board. Each runs on something different, and none of them is a metro board: a job's
# own location decides which city it's filed under, as it does for the nationwide boards.
UNIVERSITY_QUERIES = ("web", "developer", "designer", "data")


def read_jibe(source):
    """A board on Jibe (Tufts), whose own JSON answers a keyword search with everything a row needs."""
    listings = []
    for query in UNIVERSITY_QUERIES:
        found = json.loads(fetch(f"{source['url'].rstrip('/')}/api/jobs?keywords={quote(query)}&page=1&limit=20"))
        for item in found.get("jobs", []):
            data = item.get("data", {})
            if not data.get("title"):
                continue
            # Its dates are ISO with no colon in the offset, which this Python can't read; the day is enough.
            posted = (data.get("posted_date") or "")[:10]
            tags = " ".join(str(tag) for tag in (data.get("tags1") or []))
            listings.append(job(
                source, text(data["title"]), text(data.get("hiring_organization", "")),
                f"{source['url'].rstrip('/')}/jobs/{data.get('slug', '')}",
                date.fromisoformat(posted) if posted else None,
                place=text(data.get("full_location", "")),
                salary=text(" ".join(str(tag) for tag in (data.get("tags5") or []))),
                arrangement="Hybrid" if "hybrid" in tags.casefold() else
                            "Remote" if REMOTE_PLACE.search(tags) else "",
                about_=about(data.get("responsibilities") or data.get("description") or ""),
            ))
    return listings


# A board on SilkRoad (Boston University): panels of openings, ten to a page and grouped by department, with
# no dates — those jobs are dated by the day this board first saw them (see first_seen). It lists 200-odd
# openings, so it's asked the same keyword searches as the rest rather than read page by page.
SILKROAD_JOB = re.compile(r'<a id="Jobs_PagedJobList_Job-\d+"[^>]*?href="([^"]+)"(.*?)</a>', re.S)
SILKROAD_TITLE = re.compile(r'class="sr-panel__title"[^>]*>(.*?)</div>', re.S)
SILKROAD_PLACE = re.compile(r'class="sr-panel__meta"[^>]*>(.*?)</span>', re.S)


def read_silkroad(source):
    """A board on SilkRoad: a panel for each opening, with where it is and nothing else."""
    listings = []
    for query in UNIVERSITY_QUERIES:
        for link, rest in SILKROAD_JOB.findall(fetch(f"{source['url']}?SearchString={quote(query)}")):
            title = SILKROAD_TITLE.search(rest)
            if not title:
                continue
            place = text(next((m.group(1) for m in [SILKROAD_PLACE.search(rest)] if m), ""))
            # It shouts some of its towns: "BOSTON, MA".
            listings.append(job(
                source, text(title.group(1)), "", urljoin(source["url"], link), None,
                place=place.title() if place.isupper() else place,
            ))
    return listings


def workday_api(url):
    """Where a Workday board's own JSON is, worked out from the board's address: a myworkdayjobs host puts
    the tenant in the host and the site in the path, a myworkdaysite host puts both in the path."""
    parts = urlsplit(url)
    path = [part for part in parts.path.split("/") if part]
    tenant, site = (path[-2], path[-1]) if len(path) > 1 else (parts.netloc.split(".")[0], path[-1])
    return f"{parts.scheme}://{parts.netloc}/wday/cxs/{tenant}/{site}/jobs"


def read_workday(source):
    """A board on Workday (Northeastern, Wellesley), whose JSON answers a keyword search by POST. It gives a
    row everything but a description, so these jobs have no preview."""
    api, listings = workday_api(source["url"]), []
    today = datetime.now(EASTERN).date()
    for query in UNIVERSITY_QUERIES:
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query})
        for posting in json.loads(fetch(api, body=body)).get("jobPostings", []):
            if not posting.get("title"):
                continue
            # "Posted 30+ Days Ago", "Posted Yesterday", "Posted Today".
            said = (posting.get("postedOn") or "").replace("Posted", "").replace("+", "").strip()
            listings.append(job(
                source, text(posting["title"]), "",
                urljoin(source["url"].rstrip("/") + "/", "." + posting.get("externalPath", "")),
                builtin_age(said, today), place=text(posting.get("locationsText", "")),
            ))
    return listings


def read_greenhouse(source):
    """An employer's own Greenhouse board. Greenhouse publishes every board as JSON for anyone to put on a
    site of their own, which is the whole of this: one address, every opening, with what the posting says."""
    token = urlsplit(source["url"]).path.strip("/").split("/")[-1]
    found = json.loads(fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"))
    listings = []
    for posting in found.get("jobs", []):
        day = (posting.get("first_published") or posting.get("updated_at") or "")[:10]
        listings.append(job(
            source, text(posting.get("title", "")), text(posting.get("company_name", "")),
            posting.get("absolute_url", ""), date.fromisoformat(day) if day else None,
            place=text((posting.get("location") or {}).get("name", "")),
            # Its description is HTML inside JSON, escaped twice over.
            about_=about(html.unescape(posting.get("content") or "")),
        ))
    return listings


# The federal government's own board, which answers by key: free, from developer.usajobs.gov, and asked for
# as the email it was issued to plus the key itself. Without them the source is skipped rather than failed.
USAJOBS_KEY = os.environ.get("USAJOBS_KEY", "").strip()
USAJOBS_EMAIL = os.environ.get("USAJOBS_EMAIL", "").strip()
USAJOBS_QUERIES = ("user experience designer", "web developer", "software engineer", "data analyst")


def read_usajobs(source):
    """USAJOBS, for each of USAJOBS_QUERIES. Everything it carries is a federal job, which is to say every
    one of them counts towards public service loan forgiveness."""
    if not (USAJOBS_KEY and USAJOBS_EMAIL):
        print(f"  {source['name']}: no USAJOBS_KEY and USAJOBS_EMAIL set, so it's left out of this build.")
        return []
    headers = {"Authorization-Key": USAJOBS_KEY, "User-Agent": USAJOBS_EMAIL}
    listings = []
    for query in USAJOBS_QUERIES:
        asked = urlencode({"Keyword": query, "ResultsPerPage": 25,
                           "SortField": "PositionStartDate", "SortDirection": "Desc"})
        found = json.loads(fetch(f"{source['url'].rstrip('/')}/api/search?{asked}", headers=headers))
        for item in found.get("SearchResult", {}).get("SearchResultItems", []):
            said = item.get("MatchedObjectDescriptor", {})
            if not said.get("PositionTitle"):
                continue
            pay = (said.get("PositionRemuneration") or [{}])[0]
            day = (said.get("PublicationStartDate") or "")[:10]
            listings.append(job(
                source, text(said["PositionTitle"]),
                text(said.get("OrganizationName") or said.get("DepartmentName", "")),
                # It gives its own address with the port spelled out: "usajobs.gov:443/job/886695900".
                re.sub(r"(?<=usajobs\.gov):443(?=/)", "", said.get("PositionURI", "")),
                date.fromisoformat(day) if day else None,
                place=text(said.get("PositionLocationDisplay", "")),
                salary=salary_range(number(pay.get("MinimumRange")), number(pay.get("MaximumRange"))),
                about_=about(((said.get("UserArea") or {}).get("Details") or {}).get("JobSummary", "")),
            ))
    return listings


def number(said):
    """A figure a board gives as text ("115793.0"), or nothing when it doesn't give one."""
    try:
        return float(said)
    except (TypeError, ValueError):
        return None


def salary_range(low, high):
    """What a job pays, as the board gives it: "$90K–$120K", or nothing when it doesn't say."""
    def short(amount):
        return f"${round(amount / 1000)}K" if amount and amount >= 1000 else (f"${amount}" if amount else "")

    low, high = short(low), short(high)
    return f"{low}–{high}" if low and high and low != high else low or high


READERS = {"builtin": read_builtin, "wwr": read_wwr, "remoteok": read_remoteok, "remotive": read_remotive,
           "madgex": read_madgex, "techjobs": read_techjobs, "jibe": read_jibe, "silkroad": read_silkroad,
           "workday": read_workday, "greenhouse": read_greenhouse, "usajobs": read_usajobs}


def load(source):
    """A source's jobs, or the error that stopped it."""
    try:
        return source, READERS[source["kind"]](source), None
    except Exception as error:
        return source, [], error


def wanted(listing, keywords):
    """Only jobs whose titles say they're web work, or the work next to it: product, data, writing, testing,
    developer relations, art direction. keywords.txt is the whole of that decision."""
    return bool(keywords.search(listing["title"]))


def collect(sources, keywords, built_at):
    """Every source's jobs that keywords allows, newest first, each company's posting listed once."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(load, sources))
    gathered, failed, errors = [], [], {}
    for source, found, error in results:
        cutoff = built_at.date() - timedelta(
            days=DAYS_TO_KEEP_SLOW if source["kind"] in SLOW_KINDS else DAYS_TO_KEEP)
        if error:
            errors[source["name"]] = str(error)
            failed.append(source)
            print(f"✗ {source['name']}: {error}", file=sys.stderr)
            continue
        kept = [listing for listing in found
                if wanted(listing, keywords) and (listing["posted"] is None or listing["posted"] >= cutoff)]
        # Newest first before the cap, so a board over it loses its oldest postings rather than whichever
        # of its searches happened to run last.
        gathered.append((source, newest_first(kept)[:PER_SOURCE]))
        print(f"✓ {source['name']}: {len(kept[:PER_SOURCE])} jobs of {len(found)} listed")
    return gathered, failed, errors


def without_repeats(listings):
    """One row per job. A board listing it under two categories, or two boards carrying it, is one job; so is
    an employer posting the same opening to every metro Built In has, which is most of what repeats here —
    that row belongs to each of those cities, so choosing one still finds it."""
    jobs, order = {}, []
    for listing in listings:
        key = (listing["company"].casefold(), re.sub(r"\W+", "", listing["title"]).casefold())
        if key not in jobs:
            jobs[key] = dict(listing, cities=[listing["city"]])
            order.append(key)
            continue
        same = jobs[key]
        if listing["city"] not in same["cities"]:
            same["cities"].append(listing["city"])
        # Keep whichever copy says the most: the earliest sighting's date, and any pay the other gave.
        if listing["posted"] and (not same["posted"] or listing["posted"] > same["posted"]):
            same["posted"] = listing["posted"]
        same["salary"] = same["salary"] or listing["salary"]
        same["level"] = same["level"] or listing["level"]
    for key in order:
        same = jobs[key]
        if len(same["cities"]) > 1:
            # Everywhere it's posted, rather than the one place the first copy named.
            same["place"] = f"{len(same['cities'])} cities"
    return [jobs[key] for key in order]


def first_seen(listings, previous, built_at):
    """A day for a job whose board doesn't date its openings (a university's HR board, mostly): the day this
    board first saw it. board.json keeps it, so it holds from build to build, and an undated job sorts with
    the rest rather than falling to the end of the page. The first build after a board is added dates all of
    its jobs that day, which is the best this can do."""
    seen = {}
    for kept in previous.get("sources", {}).values():
        for saved_job in kept.get("jobs", []):
            if saved_job.get("link") and saved_job.get("posted"):
                seen[saved_job["link"]] = saved_job["posted"]
    for listing in listings:
        if listing["posted"] is None:
            was = seen.get(listing["link"])
            listing["posted"] = date.fromisoformat(was) if was else built_at.date()
    return listings


def newest_first(listings):
    return sorted(listings, key=lambda listing: listing["posted"] or date_floor(), reverse=True)


def date_floor():
    return (datetime.min.replace(tzinfo=timezone.utc)).date()


def render_time(day, css_class=""):
    """A day, as a timestamp the shared script turns into how long ago it was."""
    attr = f' class="{css_class}"' if css_class else ""
    # The start of the day, never a time still to come: a job put up this morning would otherwise be dated
    # ahead of the build and read as a minute old.
    moment = datetime(day.year, day.month, day.day, tzinfo=EASTERN)
    return f'<time{attr} datetime="{moment.isoformat()}">{day.strftime("%b %-d")}</time>'


# Each job's mark before its title: a page for an opening, in the kind's color while it's unclicked.
ICON_DRAWINGS = {
    "engineering": '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M6 6.5L4 8l2 1.5M10 6.5L12 8l-2 1.5"/>',
    "design": '<circle cx="8" cy="8" r="5.25"/><circle cx="8" cy="8" r="1.75" fill="currentColor" stroke="none"/>',
    "product": '<path d="M8 2.25l5.25 2.75v6L8 13.75 2.75 11V5z"/><path d="M2.75 5L8 7.75 13.25 5M8 7.75v6"/>',
}
ICON_SYMBOLS = shared.icon_symbols(ICON_DRAWINGS)
# What a title is about, for the filter. Design is the studio's work, drawing and research included; product
# is the work around the thing rather than making it, which is most of what the wider keywords bring in;
# everything else is building it.
DESIGN = re.compile(r"(?<!\w)(design(er|s)?|ux|ui|user research|researcher|branding|visual|content strategist|"
                    r"art director|creative director|creative technologist|illustrator|animator|animation|"
                    r"motion graphics|3d|prototyper)(?!\w)", re.I)
PRODUCT = re.compile(r"(?<!\w)(product (manager|owner|management|lead)|head of product|program manager|"
                     r"data (analyst|scientist)|analytics|business intelligence|technical writer|documentation|"
                     r"copywriter|editorial|content strategy|developer (relations|advocate)|devrel|evangelist|"
                     r"solutions architect|implementation|technical account|seo|digital producer)(?!\w)", re.I)


def short_place(place):
    """A place as a narrow column can show it: the first place it names, without the country or how it's
    worked. "Madison, Wisconsin, United States" and "San Diego, CA (Hybrid)" both come back as the town."""
    first = re.split(r"\s*/\s*", re.sub(r"\s*\([^)]*\)", "", place or "").strip())[0]
    parts = [part.strip() for part in first.split(",") if part.strip()]
    parts = [part for part in parts if part.casefold().replace(".", "") not in ("united states", "usa", "us")]
    return ", ".join(parts[:2])


# What a job has to say for itself, measured against the work this board is kept for. The patterns, the
# weights and the numbers below are all in fit.txt, where they can be read and argued with without touching
# this file; read_fit turns that file into the tables the scoring works from.
FIT_FOR, FIT_AGAINST, FIT_IN_BODY, FIT_SECTORS, FIT_NUMBERS = read_fit()
FIT_BODY_MOST = FIT_NUMBERS["body most"]
FIT_REASONS = FIT_NUMBERS["reasons"]  # At most this many reasons on a row.
FEATURED = FIT_NUMBERS["featured"]  # How many jobs lead the page.
FEATURED_LEAST = FIT_NUMBERS["featured least"]  # And how well they have to fit to be worth leading it.
FEATURED_DAYS = FIT_NUMBERS["featured days"]
FEATURED_DAYS_SLOW = FIT_NUMBERS["featured days slow"]


def fit(listing):
    """How closely a job matches the work this board is for, and the few words saying why. It reads titles,
    which say what a job is, and leans on the posting only for the words that are evidence on their own."""
    points, why = 0, []
    for worth, label, pattern in FIT_FOR:
        if re.search(pattern, listing["title"], re.I):
            points += worth
            why.append(label)
    for worth, label, pattern in FIT_AGAINST:
        if re.search(pattern, listing["title"], re.I):
            points -= worth
            why.append(f"not {label}")
    worth, label = FIT_SECTORS.get(listing.get("board", ""), (0, ""))
    if worth:
        points += worth
        why.append(label)
    said = " ".join(listing["about"] or ())
    mentioned = [label for label, pattern in FIT_IN_BODY if re.search(pattern, said, re.I)]
    points += min(len(mentioned), FIT_BODY_MOST)
    why += mentioned[:FIT_BODY_MOST]
    # A word earned in the title and again in the posting is one reason, not two, and five of them is as
    # much as a row can say before it stops being read.
    return points, list(dict.fromkeys(why))[:FIT_REASONS]


def featured(listings, built_at):
    """The jobs to lead the page with: the best fits among those still fresh, best first. A board whose
    postings sit for months would otherwise keep the same job at the top until it was filled."""
    def still_open(listing):
        days = FEATURED_DAYS_SLOW if listing.get("board") in SLOW_KINDS else FEATURED_DAYS
        return listing["posted"] and listing["posted"] >= built_at.date() - timedelta(days=days)

    worth_it = [(fit(listing), listing) for listing in listings if still_open(listing)]
    worth_it = [(scored, listing) for scored, listing in worth_it if scored[0] >= FEATURED_LEAST]
    worth_it.sort(key=lambda pair: (-pair[0][0], -pair[1]["posted"].toordinal()))
    return worth_it[:FEATURED]


def where_of(listing):
    """Where a job is, as the row's last column says it: its city, how many it's posted in, or — for a job in
    none of the metros — the place it names, which says more than "Elsewhere" does."""
    cities = listing.get("cities") or [listing["city"]]
    if len(cities) > 1:
        return f"{len(cities)} cities"
    if cities[0] == "elsewhere":
        place = short_place(listing["place"])
        if PLACE_LIKE.match(place):
            return place
    return CITIES[cities[0]]


def kind_of(listing):
    """Which filter a job sits under: the kind of work it is, whatever and wherever it's worked. The title
    decides, design before product so a product designer isn't filed as product management."""
    if DESIGN.search(listing["title"]):
        return "design"
    if PRODUCT.search(listing["title"]):
        return "product"
    return "engineering"


def icon(kind, decorative=False):
    return shared.icon(kind, None if decorative else kind.capitalize())


def render_row(listing):
    """A job as a row: its kind's mark, the title, who it's with, how old it is, and where it is. Every row
    carries its fit score, which the list can be ordered by, and shows it with the words behind it in the
    box that opens under the headline."""
    kind = kind_of(listing)
    where = where_of(listing)
    # The place only where it says more than the city at the end of the row does: "USA Only" for remote
    # work, not "Seattle, WA" beside Seattle or "Anywhere" beside Remote. A row that ends in "Elsewhere"
    # ends there because its place named no town — "Texas, United States" — and repeating that by the
    # title says no more than the word does.
    # Shortened, since a board that gives a place in full ("Boulder, Colorado, United States") is saying
    # one thing the row wants and two it doesn't.
    place = short_place(listing["place"]) or listing["place"]
    if (not place or where == CITIES["elsewhere"] or place.endswith("cities") or place == "Anywhere"
            or place.startswith(where.split()[0])):
        place = ""
    # A campus named as a place says nothing beside the employer it belongs to: "Johns Hopkins University"
    # twice in a row, once as who it's with and once as where.
    if place and (place.casefold() in listing["company"].casefold()
                  or listing["company"].casefold() in place.casefold()):
        place = ""
    # What it pays and whether it's hybrid are kept in the data (jobs.json) but left off the row: a board
    # states them inconsistently, and they crowd out the title.
    facts = [part for part in (place, listing["level"]) if part]
    when = render_time(listing["posted"]) if listing["posted"] else ""
    company = f'<span class="company">{html.escape(listing["company"])}</span>'
    detail = f'<span class="facts">{html.escape(" · ".join(facts))}</span>' if facts else ""
    # On a phone the row reads as one line, so where it is comes after the rest instead of a column.
    via = f'<span class="via">in {html.escape(where)}</span>'
    # The score and the words behind it go in the box that opens under the headline, with what the posting
    # says. A row of its own for them was a row's worth of page for something read once.
    points, why = fit(listing)
    note = (f'<p class="why"><span class="score">{points}</span>'
            f'<span class="score-said">fit score</span>'
            + (f'<span class="score-for">{html.escape(" · ".join(why))}</span>' if why else "")
            + '</p>')
    return (
        f'<li class="row" data-city="{" ".join(listing.get("cities") or [listing["city"]])}" data-kind="{kind}" '
        f'data-score="{points}" data-from="{html.escape(listing["company"])}">{icon(kind)}'
        f'<div class="headline"><a class="title" href="{html.escape(listing["link"])}">{html.escape(listing["title"])}</a> '
        + " ".join(part for part in (company, when, detail, via) if part)
        + f'{shared.preview(listing["title"], listing["about"], note=note)}</div> '
        f'<span class="source"><span>{html.escape(where)}</span></span></li>'
    )


def render_index(listings, failed, stale, built_at):
    """The board: the jobs that fit best first, then every job newest first, with the city menu, the filter,
    the search and the pager over them."""
    rows = [render_row(listing) for listing in listings]
    best = featured(listings, built_at)
    lead = ""
    if best:
        lead = (
            '<h1 class="featured-title">Worth a look</h1>\n<ul class="featured">\n'
            + "\n".join(render_row(listing) for _, listing in best)
            + '\n</ul>\n<p class="featured-none" hidden></p>\n'
        )

    failed_note = f"<p>Couldn’t load {html.escape(', '.join(failed))}.</p>\n" if failed else ""
    failed_note += "".join(
        f'<p>Couldn’t reach {html.escape(name)}; its jobs are from {render_time(fetched, "updated")}.</p>\n'
        for name, fetched in stale
    )

    # The cities with jobs on the page, in CITIES' order, as the menu; the filter is the kind of work.
    there = [city for city in CITIES
             if any(city in (listing.get("cities") or [listing["city"]]) for listing in listings)]
    cities = "".join(f'<option value="{city}">{html.escape(CITIES[city])}</option>' for city in there)
    menu = (f'<select class="pick" aria-label="City"><option value="">All cities</option>{cities}</select>')
    # Newest first is what a board is for; best fit first is for reading the whole of it at once, and uses
    # the same score as the jobs at the top of the page.
    order = ('<select class="pick order" aria-label="Order"><option value="">Newest</option>'
             '<option value="fit">Best fit</option></select>')
    kinds = [("engineering", "Engineering"), ("design", "Design"), ("product", "Product")]
    show_filter = (
        '<nav class="filter" aria-label="Show"><button data-show="all">All</button>'
        + "".join(f'<button data-show="{key}">{icon(key, decorative=True)}{label}</button>'
                  for key, label in kinds if any(kind_of(listing) == key for listing in listings))
        + f'<span class="finders">{menu}{order}{shared.SEARCH}</span></nav>\n'
    )

    body = (
        show_filter + lead
        + '<h1 class="all-title">Everything</h1>\n' * bool(lead)
        + f'<ul class="posts" data-page-size="{PAGE_SIZE}">\n' + "\n".join(rows) + "\n</ul>\n"
        '<p class="empty" hidden></p>\n'
        '<nav class="pager"></nav>\n'
        f"<footer>\n{failed_note}"
        f'<p>{len(listings)} jobs from <a href="sources.html">{len(set(listing["source"] for listing in listings))} '
        'boards</a>.</p>\n'
        "</footer>\n"
        f"<script>{INDEX_JS}</script>"
    )
    return page("Jobs", body, updated=built_at)


def render_sources(sources, listings, built_at):
    """Where the jobs come from, and what each board gave this build."""
    rows = []
    for source in sources:
        # A board that lists jobs anywhere is counted by its name alone, since its jobs are in many cities.
        anywhere = source["city"] == "anywhere"
        count = sum(1 for listing in listings if listing["source"] == source["name"]
                    and (anywhere or listing["city"] == source["city"]))
        where = "Anywhere" if anywhere else CITIES[source["city"]]
        rows.append(f'<li><a href="{html.escape(source["url"])}">{html.escape(source["name"])}</a>'
                    f'<span class="note">{where} · {count} job{"" if count == 1 else "s"}</span></li>')
    body = f"""<h1>Where these come from</h1>
<ul class="sources">
{chr(10).join(rows)}
</ul>
<p class="help">Each board is read every build. Only jobs whose titles match
<a href="{REPO_URL}/blob/main/jobs/keywords.txt">keywords.txt</a> are listed — web, design, UI, UX, developer,
JavaScript and the like — so a board's other openings stay off this page.</p>

<h1>Add a board</h1>
<p class="help">Boards are listed in <a href="{REPO_URL}/blob/main/jobs/sources.txt">sources.txt</a>, one per
line: how to read it, its address, the city its jobs are in, and its name.</p>

<h1>Worth a look</h1>
<p class="help">The jobs leading the page are the ones that score highest against the work this board is
kept for. What earns a point, and what loses one, is in
<a href="{REPO_URL}/blob/main/jobs/fit.txt">fit.txt</a> — hover a score to see why a job got it.</p>

<h1>Export</h1>
<p>The jobs on the page are also <a href="jobs.json">JSON</a>.</p>

<footer><p>Updated {render_time(built_at.date(), "updated")} · <a href="./">Back to the jobs</a></p></footer>"""
    return page("Boards", body)


def render_json(listings, built_at):
    return json.dumps({
        "updated": built_at.isoformat(),
        "jobs": [{"title": listing["title"], "company": listing["company"], "link": listing["link"],
                  "city": listing["city"], "place": listing["place"], "salary": listing["salary"],
                  "arrangement": listing["arrangement"], "level": listing["level"], "source": listing["source"],
                  "posted": listing["posted"].isoformat() if listing["posted"] else None}
                 for listing in listings],
    }, ensure_ascii=False, indent=1)


INDEX_JS = """
  const links = [...document.querySelectorAll(".posts a.title, .featured a.title")];

  // A dot beside the jobs you haven't opened yet. Browsers keep visited links private from pages, so clicks
  // are remembered here instead, in this browser only, for 30 days.
  let clicked = {};
  try { clicked = JSON.parse(localStorage.getItem("jobs-clicked") || "{}"); } catch (error) {}
  const monthAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
  for (const href in clicked) if (clicked[href] < monthAgo) delete clicked[href];
  const saveClicked = () => {
    try { localStorage.setItem("jobs-clicked", JSON.stringify(clicked)); } catch (error) {}
  };
  for (const a of links) {
    const li = a.closest("li");
    li.classList.toggle("unread", !clicked[a.href]);
    const markRead = event => {
      if (event.button > 1) return;  // Right-clicks only open a menu.
      clicked[a.href] = Date.now();
      // A featured job is on the page twice; opening either marks both.
      for (const other of links) if (other.href === a.href) other.closest("li").classList.remove("unread");
      saveClicked();
    };
    a.addEventListener("click", markRead);
    a.addEventListener("auxclick", markRead);
  }

  // One page of jobs at a time, with the filter (the kind of work), the menu (the city) and the search over
  // them. The city and the search are kept in the address (?city=, ?q=), so a list can be sent to someone;
  // the kind is remembered in this browser.
  const list = document.querySelector(".posts");
  // The few jobs that lead the page. They're filtered and searched with the rest, and the heading above
  // them goes when nothing in them is left.
  const lead = document.querySelector(".featured");
  const leadRows = lead ? [...lead.children] : [];
  const leadTitle = document.querySelector(".featured-title");
  const leadNone = document.querySelector(".featured-none");
  const allTitle = document.querySelector(".all-title");
  const pageSize = Number(list.dataset.pageSize);
  const items = [...list.children];
  const pager = document.querySelector(".pager");
  const empty = document.querySelector(".empty");
  const filter = document.querySelector(".filter");
  const search = document.querySelector(".search");
  const where = document.querySelector(".filter .pick");
  const order = document.querySelector(".filter .order");
  let show = "all";
  try { show = localStorage.getItem("jobs-show") || "all"; } catch (error) {}
  // A kind this page hasn't got — one that's been dropped since, or that nothing matches today — would
  // leave a reader who chose it last time looking at an empty page.
  if (show !== "all" && !(filter && filter.querySelector('[data-show="' + show + '"]'))) show = "all";
  const plain = text => text.normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
  const searchable = new Map([...items, ...leadRows].map(li => [li, plain(li.textContent)]));
  const query = () => (search && search.offsetParent ? search.value.trim() : "");
  if (search) search.value = new URLSearchParams(location.search).get("q") || "";
  if (where) {
    where.value = new URLSearchParams(location.search).get("city") || "";
    if (where.selectedIndex < 0) where.value = "";  // A city with nothing on the page now.
  }
  if (order) order.value = new URLSearchParams(location.search).get("sort") === "fit" ? "fit" : "";
  const address = page => {
    const params = new URLSearchParams();
    if (query()) params.set("q", query());
    if (where && where.value) params.set("city", where.value);
    if (order && order.value) params.set("sort", order.value);
    if (page > 1) params.set("page", page);
    return params.toString() ? "?" + params : location.pathname;
  };

  function showJobs() {
    const words = plain(query()).split(/\\s+/).filter(Boolean);
    const city = where ? where.value : "";
    const shown = items.filter(li => (show === "all" || li.dataset.kind === show)
      && (!city || li.dataset.city.split(" ").includes(city))
      && words.every(word => searchable.get(li).includes(word)));
    const leadShown = leadRows.filter(li => (show === "all" || li.dataset.kind === show)
      && (!city || li.dataset.city.split(" ").includes(city))
      && words.every(word => searchable.get(li).includes(word)));
    // Ordering the whole list by fit puts the same jobs at the top in the same order, so the few that lead
    // the page would be saying it twice. The list itself is the answer then.
    const leading = !(order && order.value === "fit") && leadShown.length > 0;
    leadRows.forEach(li => { li.hidden = !leading || !leadShown.includes(li); });
    if (lead) lead.hidden = !leading;
    if (leadTitle) leadTitle.hidden = !leading;
    if (allTitle) allTitle.hidden = !leading;
    if (leadNone) {
      leadNone.textContent = "Nothing here fits especially well right now.";
      leadNone.hidden = leading || shown.length === 0 || (order && order.value === "fit");
    }
    // Best fit reorders the rows themselves, since what the page shows is their order in it. Ties keep the
    // order they came in, which is newest first.
    const ordered = order && order.value === "fit"
      ? [...shown].sort((a, b) => Number(b.dataset.score) - Number(a.dataset.score)) : shown;
    for (const li of ordered) list.appendChild(li);
    const pages = Math.max(1, Math.ceil(ordered.length / pageSize));
    const page = Math.min(pages, Math.max(1, parseInt(new URLSearchParams(location.search).get("page")) || 1));
    items.forEach(li => { li.hidden = true; });
    ordered.forEach((li, i) => { li.hidden = i < (page - 1) * pageSize || i >= page * pageSize; });
    list.classList.add("paged");
    const link = (n, text) => `<a href="${address(n)}">${text}</a>`;
    pager.innerHTML = pages < 2 ? "" :
      (page > 1 ? link(page - 1, "← Newer") : "<span></span>") +
      `<span>Page ${page} of ${pages}</span>` +
      (page < pages ? link(page + 1, "Older →") : "<span></span>");
    const place = city ? where.options[where.selectedIndex].textContent : "";
    empty.textContent = words.length ? `Nothing ${place ? "in " + place : "here"} matches \\u201c${query()}\\u201d.`
      : place ? `No jobs in ${place} right now.` : "No jobs right now.";
    empty.hidden = shown.length > 0;
    if (where) where.classList.toggle("chosen", Boolean(city));
    if (order) order.classList.toggle("chosen", Boolean(order.value));
    if (filter) for (const b of filter.querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.show === show);
  }
  showJobs();

  if (filter) filter.addEventListener("click", event => {
    const b = event.target.closest("button");
    if (!b) return;
    show = b.dataset.show;
    try { localStorage.setItem("jobs-show", show); } catch (error) {}
    history.replaceState(null, "", address(1));  // Back to page one.
    showJobs();
  });
  if (search) search.addEventListener("input", () => {
    history.replaceState(null, "", address(1));
    showJobs();
  });
  if (order) order.addEventListener("change", () => {
    history.replaceState(null, "", address(1));
    showJobs();
  });
  // Choosing a city shows every kind of job in it, which is what a reader means by choosing one.
  if (where) where.addEventListener("change", () => {
    show = "all";
    try { localStorage.setItem("jobs-show", show); } catch (error) {}
    history.replaceState(null, "", address(1));
    showJobs();
  });
"""

HEAD = """<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="manifest.webmanifest">"""

CSS = """
  /* The kinds' marks: green for building it, violet for designing it, amber for the work around it. Where
     it's worked isn't one of them — that's the city at the end of the row, "Remote" among the rest. */
  :root { --engineering: #34d399; --design: #a78bfa; --product: #fbbf24; }
  h1 { margin: 3rem 0 .75rem; color: #777; font-size: .75rem; font-weight: 600;
       letter-spacing: .08em; text-transform: uppercase; }
  h1:first-child, header + h1 { margin-top: 0; }
  .filter [data-show="engineering"] .icon { color: var(--engineering); }
  .filter [data-show="design"] .icon { color: var(--design); }
  .filter [data-show="product"] .icon { color: var(--product); }
  li { padding: .4rem 0; }
  /* Until the script picks the page, show the first one, so the whole list never flashes up. */
  .posts:not(.paged) li:nth-child(n+PAGE_START) { display: none; }
  .note, time, .facts { color: #666; font-size: .8em; }
  /* Who the job is with, beside its title: brighter than the rest of the line, which is detail. */
  .company { color: #999; font-size: .85em; }
  .headline time, .headline .facts, .headline .company { flex: none; }
  .headline > .via { display: none; }
  /* The parts of a row are flex items, so the spaces between them in the markup don't show: each keeps its
     own gap from the one before. */
  time, .facts, .company { margin-left: .6em; white-space: nowrap; }
  /* Each job's mark: in its kind's color while it's unopened, gray once it's been clicked. */
  .row > .icon { color: #555; }
  .unread > .icon { color: var(--engineering); }
  .row[data-kind="design"].unread > .icon { color: var(--design); }
  .row[data-kind="product"].unread > .icon { color: var(--product); }
  @media (max-width: 34rem) {
    /* A phone's row reads as one line: the title, when it went up, where it is, and who it's with. */
    .headline > time, .headline > .facts, .headline > .company { margin-left: 0; }
    .headline > .via { display: inline; color: #666; font-size: .8em; }
    .facts { white-space: normal; }
    /* Where it is has already been said in the line, by "in Boston"; the column would say it twice. */
    .posts .row > .source, .featured .row > .source { display: none; }
  }
  /* The few jobs that lead the page: the same rows, set apart by the heading above them. Why a job is here
     takes more words than the small print of an ordinary row, and the parts of a headline share one line —
     so it goes on a line of its own underneath, where it can't squeeze the title out of its own row. */
  .featured { margin-bottom: 1rem; }
  /* Only the up and down of it: a phone's row keeps a left margin for the mark beside its first line. */
  .featured .row { padding-top: .5rem; padding-bottom: .5rem; }
  /* How well a featured job fits, at the top of the box that opens under its headline, above what the
     posting says. The number is the score, and what follows it is what earned it. A phone has no hover and
     so never opens the box: there, the order the jobs are in is what the score has to say. */
  .preview .why { display: flex; align-items: baseline; gap: .5em; margin-bottom: .7em; padding-bottom: .6em;
                  border-bottom: 1px solid #222; color: #999; }
  .preview .score { flex: none; padding: 0 .45em; border: 1px solid #444; border-radius: 999px;
                    color: #ddd; font-size: .9em; }
  .preview .score-said { flex: none; color: #fff; }
  .preview .score-for { color: #888; }
  .preview .score-for::before { content: "for "; }
  /* Where a row is three columns, the mark sits with the title rather than in the middle of a row that may
     now run to three lines. On a phone the mark is placed against the first line already. */
  @media (min-width: 34.01rem) {
    .featured .row > .icon { align-self: start; margin-top: .36em; }
  }
  .featured-none { color: #666; font-size: .9rem; }
  .all-title { margin-top: 2.25rem; }
  .sources { columns: 2; column-gap: 2.5rem; }
  .sources li { break-inside: avoid; }
  @media (max-width: 34rem) { .sources { columns: 1; } }
  .help { color: #777; font-size: .85em; }
  .pager { display: flex; justify-content: space-between; margin-top: 2.5rem; color: #666; font-size: .8rem; }
  .pager a, .pager a:visited { color: #999; }
  .empty { color: #777; }
"""


def page(title, body, updated=None):
    # The header names the other pages beside this one, as theirs name it.
    return shared.page("jobs", title, body, css=CSS.replace("PAGE_START", str(PAGE_SIZE + 1)), head=HEAD,
                       symbols=ICON_SYMBOLS, updated=updated)


def saved(listing):
    return dict(listing, posted=listing["posted"].isoformat() if listing["posted"] else None)


def restored(kept):
    return dict(kept, posted=date.fromisoformat(kept["posted"]) if kept["posted"] else None)


def previous_build():
    return shared.previous_build(BOARD_URL, USER_AGENT)


def gather(gathered, failed, previous, built_at):
    """Each source's jobs as they loaded, or its last good ones when it failed and they're recent enough.
    A source is remembered by its address rather than its name, since two feeds of one board share a name
    and would otherwise keep one record between them."""
    kept_sources = previous.get("sources", {})
    listings, still_failed, stale = [], [], []
    for source in failed:
        name, kept = source["name"], kept_sources.get(source["url"])
        if not kept or built_at - datetime.fromisoformat(kept["fetched"]) > FALLBACK_LIMIT:
            still_failed.append(name)
            continue
        fetched = datetime.fromisoformat(kept["fetched"])
        stale.append((name, fetched.date()))
        print(f"  {name}: showing its jobs from {kept['fetched']} instead", file=sys.stderr)
        listings += [restored(listing) for listing in kept["jobs"]]
    for source, found in gathered:
        listings += found
    return listings, still_failed, stale


still_failing = shared.still_failing


def due(previous, built_at):
    """Whether it's time for a new build. The workflow runs every 15 minutes, for the newsfeed; boards list
    the same openings all day, so on those runs the board rebuilds only every few hours (JOBS_EVERY_HOURS).
    A push sets it to 0, to build at once."""
    every = float(os.environ.get("JOBS_EVERY_HOURS") or 0)
    if not every or not previous.get("built"):
        return True
    # A few minutes' grace, so a build that ran slightly early doesn't push the next one back a whole cycle.
    return built_at - datetime.fromisoformat(previous["built"]) >= timedelta(hours=every) - timedelta(minutes=10)


def main():
    sources = read_sources()
    keywords = read_keywords()
    built_at = datetime.now(timezone.utc)
    previous = previous_build()
    if not due(previous, built_at):
        print(f"Built at {previous['built']}; not due yet, so not rebuilding.")
        return
    gathered, failed, errors = collect(sources, keywords, built_at)
    listings, failed, stale = gather(gathered, failed, previous, built_at)
    if not listings:
        sys.exit("No jobs — not writing the page.")
    listings = newest_first(without_repeats(first_seen(listings, previous, built_at)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    (OUT_DIR / "index.html").write_text(render_index(listings, failed, stale, built_at))
    (OUT_DIR / "sources.html").write_text(render_sources(sources, listings, built_at))
    (OUT_DIR / "jobs.json").write_text(render_json(listings, built_at))
    # What this build found, for the next one to fall back on when a board won't load.
    record = {
        "built": built_at.isoformat(),
        "sources": {source["url"]: {"name": source["name"], "fetched": built_at.isoformat(),
                                    "jobs": [saved(listing) for listing in found]}
                    for source, found in gathered},
        "failing": still_failing(errors, previous, built_at),
    }
    (OUT_DIR / "board.json").write_text(json.dumps(record, ensure_ascii=False))
    print(f"Wrote {OUT_DIR.relative_to(ROOT.parent)}: {len(listings)} jobs in "
          f"{len({listing['city'] for listing in listings})} cities")


if __name__ == "__main__":
    main()
