# Newsfeed and Events

Two plain, black pages built from the same code:

- **Newsfeed**, the latest posts, podcast episodes and videos from a few sites, newest first: https://news.grahamhagenah.com/
- **Events**, concerts, films, and art and talks in Boston, Cambridge and Somerville over the next 30 days, grouped by day: https://events.grahamhagenah.com/

## What's where

- `news/`: the newsfeed. `feeds.txt` lists its sites; `build.py` reads their feeds (and links podcast episodes to Pocket Casts); `sources.py` is the bot behind the "Add a site" page.
- `events/`: the events page. `sources.txt` lists its venues and how to read each; `build.py` has a reader for each kind of source and combines a film showing at several theaters into one row; `find_venue.py` looks up Ticketmaster venue ids.
- `shared/`: what both use. `site.py` has the page around each list (head, header, the styles they share, icons, the script that ages timestamps), fetching with retries, and the fallback and failure bookkeeping; `alerts.py` opens and closes "Source failing" issues.
- `tests/`: each page's readers against saved samples of real sources (`tests/fixtures/news`, `tests/fixtures/events`), the fallback rules, and the shared page and alerts.

Run everything from this top folder, with only the Python standard library:

- `python3 -m news.build` and `python3 -m events.build` write `dist/news` and `dist/events`; open their `index.html`.
- `python3 -m unittest` runs the tests.

## The newsfeed

- Add or remove sites from the [sources page](https://news.grahamhagenah.com/sources.html), which opens a pre-filled issue that the `sources.yml` workflow applies to `news/feeds.txt`, or edit that file directly. A homepage URL is enough; the build finds the feed. Options like `limit=5` and `days=14` go at the end of a line, and `pocketcasts=<id>` ties a podcast to its Pocket Casts show when its feed address isn't the one Pocket Casts knows.
- YouTube channels work like any site (a channel's page is enough). Their videos, and any post linking straight to a YouTube video, play on the page in YouTube's privacy-enhanced embedded player, in a plain window that closes when the video ends, before YouTube can suggest another. Shorts are left out. Hold a modifier key while clicking to open YouTube instead.

## The events page

- Venues rarely publish feeds, so the build uses the most dependable thing each offers: AEG's event data (Roadrunner), an AXS venue site's full listing and its shows' own pages for start times (The Sinclair), a restaurant site's schedule of shows and their own pages for set times (The Mad Monkfish's jazz), schema.org event data (the Brattle, House of Blues, Deep Cuts' page on DICE), a calendar feed (Midway Cafe), a WordPress calendar's API (Lizard Lounge, The Rockwell's music), the schedule data behind a theater chain's site (Alamo Drafthouse, Landmark's Kendall Square), Ticketmaster (the Paradise, Brighton Music Hall, the Crystal Ballroom, the Somerville Theatre), a Veezi-hosted theater's own data (West Newton Cinema), or the page itself (the Coolidge, the Harvard Film Archive, TicketWeb listings at the Middle East).
- Art & talks come from MIT's events calendar (public exhibitions, and talks from its architecture and humanities schools or listed under the arts), the Boston Public Library's events feed (talks, lectures and exhibitions for adults), the MFA's and Harvard Art Museums' calendars (their lectures, talks and special events; their films go under Film), SoWa's First Fridays, the ICA's talks, dance and First Fridays (its concerts go under Music, its films under Film), the French Library's talks and screenings (its screenings go under Film), and ArtsEmerson's performances. An exhibition is listed on the day it opens, with when it closes.
- The search beside the filter keeps the events with every word typed in their name, place or description, like a venue, a director or "35mm" (press / to jump to it, Esc to clear it). The newsfeed has the same search, over headlines, sources and previews. It's hidden on phones.
- Clicking a showtime adds that showing to a calendar: an iPhone or iPad gets Calendar's own sheet, Android Google Calendar's page, and anything else a `.ics` file. The page makes the event itself, with the venue's address from `VENUE_ADDRESSES` (or the event's own), so nothing is fetched for it.
- Hovering an event's name shows what its source says about it: a film's blurb or credits, a band's bio, the door time and cover, as the newsfeed shows a post's first lines. Sources that say nothing (the MFA, the Middle East, Kendall Square) have no preview, except a cut-off name in full.
- Ticketmaster venues need a free [Ticketmaster Discovery API](https://developer.ticketmaster.com/) key, kept as this repo's `TICKETMASTER_KEY` secret. To add one, find its venue id with Actions → "Find a Ticketmaster venue".

## Boston, Daily, the public version

The events build also writes the same listings for anyone, as **Boston, Daily** (`dist/public`), published from the [grahamhagenah/boston-daily](https://github.com/grahamhagenah/boston-daily) repo's `gh-pages` branch at https://boston.grahamhagenah.com/ (a CNAME record for `boston` points at `grahamhagenah.github.io`). It has its own name in place of the Newsfeed/Events switcher, an About page (what it is, and every venue) and a Contact page, a footer listing every page (Browse: all events and each kind; When: tonight and the weekends; About and Contact) with the current one marked (`public_footer`), shorter previews of the venues' own descriptions, no "Add a source" link, and lets search engines list it.

- For search engines: a descriptive title and description, a one-line tagline, a canonical address, sharing tags, every listing as schema.org event data (with each venue's street address, from `VENUE_ADDRESSES` in `events/build.py` unless the source gives the event's own, so add a new venue's there), a `sitemap.xml` and a `robots.txt` pointing to it.
- Each kind has its own page, `/music/`, `/film/` and `/talks/`, with only its events and its own title, description and tagline (`PUBLIC_PAGES` in `events/build.py`). The filter's choices link to them; on the home page, which has every event, the filter shows a kind in place and puts its page's address in the address bar. The pages link to each other from the site's root (`/about.html`), since a relative link would go astray after that.
- `/tonight/` lists what's still to come today, on one page (`PUBLIC_TONIGHT`). It holds tomorrow's listings too and shows only the day it is where it's read, so it rolls over at midnight rather than waiting up to three hours for the next build.
- `/weekend/` lists Friday to Sunday: the weekend it is, or from Monday to Thursday the one coming (`weekend_days`), and `/weekend/next/` the weekend after; each shows the whole weekend on one page and links to the other where the pager would be (`PUBLIC_WEEKENDS`). Their filter shows a kind in place. Every public page's footer links to this weekend's.
- Shared links show a card (1200×630) in the site's style: the home page's, and one for each kind's page, in `events/share/`. They don't change with the listings, so they're made by hand with Chrome: `python3 -m events.share_cards`, again after renaming the site or rewording a page.
- A source ending in `public=no` in `events/sources.txt` stays off it, for one that isn't ours to republish.
- The Contact page's form sends through [FormSubmit](https://formsubmit.co) to the address in `PUBLIC_CONTACT`; its first message asks that address to confirm, and FormSubmit then offers a random alias to use there instead of the address.
- The deploy pushes it with a deploy key that can write to that repo only, kept as the `PUBLIC_DEPLOY_KEY` secret. To move it to another domain: point the domain at GitHub Pages, set it in that repo's Pages settings, change the `CNAME` the deploy writes, and change `PUBLIC_URL`.

## When a source fails

- Each build publishes what its page was built from, `feeds.json` and `listings.json`. When a source fails, or suddenly returns nothing while it had items coming up, the next build uses its items from there instead, if they're under two days old, and says so at the bottom of the page. A feed that's just quiet isn't a failure.
- A source that fails every build for six hours gets a "Source failing: <name> (<page>)" issue here, so GitHub emails about it. It closes itself once the source loads again.
- When a site changes and its reader is fixed, save a fresh sample of it in `tests/fixtures` too.

## Publishing

`deploy.yml` runs the tests, then builds and publishes both pages:

- The newsfeed is this repo's GitHub Pages site.
- A repo can publish only one Pages site, so the events page is pushed to the `gh-pages` branch of [grahamhagenah/events](https://github.com/grahamhagenah/events), whose Pages site is events.grahamhagenah.com. The push uses a deploy key that can write to that repo only, kept as the `EVENTS_DEPLOY_KEY` secret here (its public half is under that repo's Settings → Deploy keys). Deploy keys don't expire.
- The newsfeed rebuilds every run; the events page every three hours, since listings change slowly and the theaters' sites are small. A push rebuilds both at once.

## Scheduled builds

GitHub's own `schedule` trigger skips most runs, so a [cron-job.org](https://cron-job.org) job named "Newsfeed build" starts the deploy every 15 minutes instead, for both pages. The schedule in `deploy.yml` stays as a backup.

- **The job:** `POST https://api.github.com/repos/grahamhagenah/news/actions/workflows/deploy.yml/dispatches` with body `{"ref":"main"}` and headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`, and `User-Agent: cron-job.org`. GitHub answers 204. Without the User-Agent header it answers 403.
- **The token:** a fine-grained token on the grahamhagenah account, limited to this repo with **Actions: Read and write** only. It can start and cancel builds but can't read or change code. It expires; renew it at github.com/settings/personal-access-tokens and paste the new one into the job's Authorization header.
- **If the pages go stale:** check cron-job.org's history or failure emails. A 401 means the token expired or was revoked, and a 403 means a missing permission or header.
- **Keep-alive:** GitHub disables a workflow after 60 days without a commit, which would also block the job. So on scheduled and dispatched runs, the `keepalive` job makes an empty commit if the repo has been quiet for 50 days.
