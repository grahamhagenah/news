#!/usr/bin/env python3
"""Build the job board: web and design jobs in a handful of metro areas, and remote ones, newest first.

Sources are in sources.txt, the titles worth listing in keywords.txt. Run it from the repo's top folder:
`python3 -m jobs.build` writes dist/jobs.
"""

import html
import json
import os
import re
import shutil
import sys
import threading
import urllib.error
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from shared import site as shared

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.txt"
KEYWORDS_FILE = ROOT / "keywords.txt"
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
PER_SOURCE = 120  # At most this many from one source, so no board crowds out the rest.
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
    "bayarea": "Bay Area",
    "seattle": "Seattle",
    "denver": "Denver",
    "chicago": "Chicago",
    "austin": "Austin",
    "losangeles": "Los Angeles",
    "remote": "Remote",
}


def read_sources():
    """sources.txt's lines: how to read the source, its URL, the city its jobs are in, and the board's name."""
    sources = []
    for line in SOURCES_FILE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            kind, url, city, *name = line.split()
            if city not in CITIES:
                sys.exit(f"sources.txt: {city} isn't one of the cities ({', '.join(CITIES)})")
            sources.append({"kind": kind, "url": url, "city": city, "name": " ".join(name)})
    return sources


def read_keywords():
    """keywords.txt as one pattern: any of its words or phrases, as whole words, in any case."""
    lines = [line.strip() for line in KEYWORDS_FILE.read_text().splitlines()]
    words = [line for line in lines if line and not line.startswith("#")]
    return re.compile("|".join(rf"(?<!\w){re.escape(word)}(?!\w)" for word in words), re.I)


_fetched, _fetching = {}, threading.Lock()


def fetch(url, attempts=3, timeout=20):
    """The page or data at url, as text, downloaded once a build however many times it's read."""
    with _fetching:
        pending, first = _fetched.get(url), url not in _fetched
        if first:
            pending = _fetched[url] = Future()
    if first:
        try:
            pending.set_result(shared.fetch(url, USER_AGENT, attempts, timeout)[1].decode("utf-8", "replace"))
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


def job(source, title, company, link, posted, place="", salary="", arrangement="", level="", about_=()):
    """One listing. posted is when the board says it went up; place is where it is in the source's own words
    ("Boston, MA", "Anywhere in the World"), which is finer than the city it's filed under."""
    return {
        "title": title,
        "company": company or source["name"],
        "link": link,
        "posted": posted,
        "city": source["city"],
        "place": place,
        "salary": salary,
        "arrangement": arrangement,
        "level": level,
        "about": [line for line in about_ if line.casefold() != title.casefold()],
        "source": source["name"],
    }


# Built In writes each job as a card, and the list of them again as schema.org data whose descriptions are
# the tidiest summary of each. Its script tag's type is HTML-escaped, so it isn't found by looking for
# `type="application/ld+json"`.
BUILTIN_PAGES = ("jobs/dev-engineering", "jobs/design-ux")
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
    """A Built In metro site's engineering and design listings: a card for each job, with the company, where
    it is, what it pays and how it's worked (remote, hybrid, in the office)."""
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


def salary_range(low, high):
    """What a job pays, as the board gives it: "$90K–$120K", or nothing when it doesn't say."""
    def short(amount):
        return f"${round(amount / 1000)}K" if amount and amount >= 1000 else (f"${amount}" if amount else "")

    low, high = short(low), short(high)
    return f"{low}–{high}" if low and high and low != high else low or high


READERS = {"builtin": read_builtin, "wwr": read_wwr, "remoteok": read_remoteok, "remotive": read_remotive}


def load(source):
    """A source's jobs, or the error that stopped it."""
    try:
        return source, READERS[source["kind"]](source), None
    except Exception as error:
        return source, [], error


def wanted(listing, keywords):
    """Only jobs whose titles say they're about making things for the web."""
    return bool(keywords.search(listing["title"]))


def collect(sources, keywords, built_at):
    """Every source's jobs that keywords allows, newest first, each company's posting listed once."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(load, sources))
    cutoff = built_at.date() - timedelta(days=DAYS_TO_KEEP)
    gathered, failed, errors = [], [], {}
    for source, found, error in results:
        if error:
            errors[source["name"]] = str(error)
            failed.append(source["name"])
            print(f"✗ {source['name']}: {error}", file=sys.stderr)
            continue
        kept = [listing for listing in found
                if wanted(listing, keywords) and (listing["posted"] is None or listing["posted"] >= cutoff)]
        gathered.append((source, kept[:PER_SOURCE]))
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
    "remote": '<path d="M8 2.75a5.25 5.25 0 0 1 0 10.5a5.25 5.25 0 0 1 0-10.5z"/><path d="M2.75 8h10.5M8 2.75c1.6 1.7 1.6 8.8 0 10.5c-1.6-1.7-1.6-8.8 0-10.5z"/>',
}
ICON_SYMBOLS = shared.icon_symbols(ICON_DRAWINGS)
# What a title is about, for the filter: design work, or building it.
DESIGN = re.compile(r"(?<!\w)(design(er|s)?|ux|ui|user research|researcher|brand|visual|content strategist)(?!\w)", re.I)


def kind_of(listing):
    return "remote" if listing["city"] == "remote" else "design" if DESIGN.search(listing["title"]) else "engineering"


def icon(kind, decorative=False):
    return shared.icon(kind, None if decorative else kind.capitalize())


def render_index(listings, failed, stale, built_at):
    """The board: every job, newest first, with the city menu, the filter, the search and the pager over it."""
    rows = []
    for listing in listings:
        kind = kind_of(listing)
        # What the board knows beyond the title, in the order it's worth reading: where, what it pays, how
        # it's worked, how senior. A phone's row breaks between them.
        facts = [part for part in (listing["place"], listing["salary"],
                                   listing["arrangement"] if listing["arrangement"] != "Remote" or not listing["place"] else "",
                                   listing["level"]) if part]
        when = render_time(listing["posted"]) if listing["posted"] else ""
        detail = f'<span class="facts">{html.escape(" · ".join(facts))}</span>' if facts else ""
        via = f'<span class="via">on {html.escape(listing["source"])}</span>'
        rows.append(
            f'<li class="row" data-city="{" ".join(listing.get("cities") or [listing["city"]])}" data-kind="{kind}" '
            f'data-from="{html.escape(listing["company"])}">{icon(kind)}'
            f'<div class="headline"><a class="title" href="{html.escape(listing["link"])}">{html.escape(listing["title"])}</a> '
            + " ".join(part for part in (when, detail, via) if part)
            + f'{shared.preview(listing["title"], listing["about"])}</div> '
            f'<span class="source"><span>{html.escape(listing["company"])}</span></span></li>'
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
    kinds = [("engineering", "Engineering"), ("design", "Design"), ("remote", "Remote")]
    show_filter = (
        '<nav class="filter" aria-label="Show"><button data-show="all">All</button>'
        + "".join(f'<button data-show="{key}">{icon(key, decorative=True)}{label}</button>'
                  for key, label in kinds if any(kind_of(listing) == key for listing in listings))
        + f'<span class="finders">{menu}{shared.SEARCH}</span></nav>\n'
    )

    body = (
        show_filter
        + f'<ul class="posts" data-page-size="{PAGE_SIZE}">\n' + "\n".join(rows) + "\n</ul>\n"
        '<p class="empty" hidden></p>\n'
        '<nav class="pager"></nav>\n'
        f"<footer>\n{failed_note}"
        f'<p>{len(listings)} jobs from <a href="sources.html">{len(set(listing["source"] for listing in listings))} '
        'boards</a>, newest first.</p>\n'
        "</footer>\n"
        f"<script>{INDEX_JS}</script>"
    )
    return page("Jobs", body, updated=built_at)


def render_sources(sources, listings, built_at):
    """Where the jobs come from, and what each board gave this build."""
    rows = []
    for source in sources:
        count = sum(1 for listing in listings if listing["source"] == source["name"]
                    and listing["city"] == source["city"])
        rows.append(f'<li><a href="{html.escape(source["url"])}">{html.escape(source["name"])}</a>'
                    f'<span class="note">{CITIES[source["city"]]} · {count} jobs</span></li>')
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
  const links = [...document.querySelectorAll(".posts a.title")];

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
      li.classList.remove("unread");
      saveClicked();
    };
    a.addEventListener("click", markRead);
    a.addEventListener("auxclick", markRead);
  }

  // One page of jobs at a time, with the filter (the kind of work), the menu (the city) and the search over
  // them. The city and the search are kept in the address (?city=, ?q=), so a list can be sent to someone;
  // the kind is remembered in this browser.
  const list = document.querySelector(".posts");
  const pageSize = Number(list.dataset.pageSize);
  const items = [...list.children];
  const pager = document.querySelector(".pager");
  const empty = document.querySelector(".empty");
  const filter = document.querySelector(".filter");
  const search = document.querySelector(".search");
  const where = document.querySelector(".filter .pick");
  let show = "all";
  try { show = localStorage.getItem("jobs-show") || "all"; } catch (error) {}
  const plain = text => text.normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
  const searchable = new Map(items.map(li => [li, plain(li.textContent)]));
  const query = () => (search && search.offsetParent ? search.value.trim() : "");
  if (search) search.value = new URLSearchParams(location.search).get("q") || "";
  if (where) {
    where.value = new URLSearchParams(location.search).get("city") || "";
    if (where.selectedIndex < 0) where.value = "";  // A city with nothing on the page now.
  }
  const address = page => {
    const params = new URLSearchParams();
    if (query()) params.set("q", query());
    if (where && where.value) params.set("city", where.value);
    if (page > 1) params.set("page", page);
    return params.toString() ? "?" + params : location.pathname;
  };

  function showJobs() {
    const words = plain(query()).split(/\\s+/).filter(Boolean);
    const city = where ? where.value : "";
    const shown = items.filter(li => (show === "all" || li.dataset.kind === show)
      && (!city || li.dataset.city.split(" ").includes(city))
      && words.every(word => searchable.get(li).includes(word)));
    const pages = Math.max(1, Math.ceil(shown.length / pageSize));
    const page = Math.min(pages, Math.max(1, parseInt(new URLSearchParams(location.search).get("page")) || 1));
    items.forEach(li => { li.hidden = true; });
    shown.forEach((li, i) => { li.hidden = i < (page - 1) * pageSize || i >= page * pageSize; });
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
  /* The kinds' marks: green for building it, violet for designing it, coral for remote. */
  :root { --engineering: #34d399; --design: #a78bfa; --remote: #fb7185; }
  h1 { margin: 3rem 0 .75rem; color: #777; font-size: .75rem; font-weight: 600;
       letter-spacing: .08em; text-transform: uppercase; }
  h1:first-child, header + h1 { margin-top: 0; }
  .filter [data-show="engineering"] .icon { color: var(--engineering); }
  .filter [data-show="design"] .icon { color: var(--design); }
  .filter [data-show="remote"] .icon { color: var(--remote); }
  li { padding: .4rem 0; }
  /* Until the script picks the page, show the first one, so the whole list never flashes up. */
  .posts:not(.paged) li:nth-child(n+PAGE_START) { display: none; }
  .note, time, .facts { color: #666; font-size: .8em; }
  .headline time, .headline .facts { flex: none; }
  .headline > .via { display: none; }
  .facts { margin-left: .6em; white-space: nowrap; }
  /* Each job's mark: in its kind's color while it's unopened, gray once it's been clicked. */
  .row > .icon { color: #555; }
  .unread > .icon { color: var(--engineering); }
  .row[data-kind="design"].unread > .icon { color: var(--design); }
  .row[data-kind="remote"].unread > .icon { color: var(--remote); }
  @media (max-width: 34rem) {
    /* A phone's row reads as one line: the title, when it went up, where it is, and who it's with. */
    .headline > time, .headline > .facts { margin-left: 0; }
    .headline > .via { display: inline; color: #666; font-size: .8em; }
    .facts { white-space: normal; }
    .posts .row > .source { display: none; }
  }
  .sources { columns: 2; column-gap: 2.5rem; }
  .sources li { break-inside: avoid; }
  @media (max-width: 34rem) { .sources { columns: 1; } }
  .help { color: #777; font-size: .85em; }
  .pager { display: flex; justify-content: space-between; margin-top: 2.5rem; color: #666; font-size: .8rem; }
  .pager a, .pager a:visited { color: #999; }
  .empty { color: #777; }
"""


def page(title, body, updated=None):
    return shared.page("jobs", title, body, css=CSS.replace("PAGE_START", str(PAGE_SIZE + 1)), head=HEAD,
                       symbols=ICON_SYMBOLS, updated=updated, links=[("Jobs", "./", True)])


def saved(listing):
    return dict(listing, posted=listing["posted"].isoformat() if listing["posted"] else None)


def restored(kept):
    return dict(kept, posted=date.fromisoformat(kept["posted"]) if kept["posted"] else None)


def previous_build():
    return shared.previous_build(BOARD_URL, USER_AGENT)


def gather(gathered, failed, previous, built_at):
    """Each source's jobs as they loaded, or its last good ones when it failed and they're recent enough."""
    kept_sources = previous.get("sources", {})
    listings, still_failed, stale = [], [], []
    for name in failed:
        kept = kept_sources.get(name)
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
    listings = newest_first(without_repeats(listings))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "static", OUT_DIR, dirs_exist_ok=True)
    (OUT_DIR / "index.html").write_text(render_index(listings, failed, stale, built_at))
    (OUT_DIR / "sources.html").write_text(render_sources(sources, listings, built_at))
    (OUT_DIR / "jobs.json").write_text(render_json(listings, built_at))
    # What this build found, for the next one to fall back on when a board won't load.
    record = {
        "built": built_at.isoformat(),
        "sources": {source["name"]: {"fetched": built_at.isoformat(),
                                     "jobs": [saved(listing) for listing in found]}
                    for source, found in gathered},
        "failing": still_failing(errors, previous, built_at),
    }
    (OUT_DIR / "board.json").write_text(json.dumps(record, ensure_ascii=False))
    print(f"Wrote {OUT_DIR.relative_to(ROOT.parent)}: {len(listings)} jobs in "
          f"{len({listing['city'] for listing in listings})} cities")


if __name__ == "__main__":
    main()
