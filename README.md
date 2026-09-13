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

- Venues rarely publish feeds, so the build uses the most dependable thing each offers: AEG's event data (Roadrunner), an event feed (The Sinclair), schema.org event data (the Brattle, House of Blues, Deep Cuts' page on DICE), a calendar feed (Midway Cafe), a WordPress calendar's API (Lizard Lounge, The Rockwell's music), the schedule data behind a theater chain's site (Alamo Drafthouse, Landmark's Kendall Square), Ticketmaster (the Paradise, Brighton Music Hall, the Crystal Ballroom, the Somerville Theatre), or the page itself (the Coolidge, TicketWeb listings at the Middle East).
- Art & talks come from MIT's events calendar (public exhibitions, and talks from its architecture and humanities schools or listed under the arts), the Boston Public Library's events feed (talks, lectures and exhibitions for adults), the MFA's and Harvard Art Museums' calendars (their lectures, talks and special events; their films go under Film), and SoWa's First Fridays. An exhibition is listed on the day it opens, with when it closes.
- Hovering an event's name shows what its source says about it: a film's blurb or credits, a band's bio, the door time and cover, as the newsfeed shows a post's first lines. Sources that say nothing (the MFA, the Middle East, Kendall Square) have no preview, except a cut-off name in full.
- Ticketmaster venues need a free [Ticketmaster Discovery API](https://developer.ticketmaster.com/) key, kept as this repo's `TICKETMASTER_KEY` secret. To add one, find its venue id with Actions → "Find a Ticketmaster venue".

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
