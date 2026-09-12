# Newsfeed

A plain black-and-white list of the latest posts from a few sites, newest first. Live at https://news.grahamhagenah.com/.

- Add or remove sites from the [sources page](https://news.grahamhagenah.com/sources.html), which opens a pre-filled issue that the `sources.yml` workflow applies to `feeds.txt`, or edit `feeds.txt` directly. A homepage URL is enough; the build finds the feed. Options like `limit=5` and `days=14` go at the end of a line.
- Each build also publishes `feeds.json`, every feed's posts as the page used them. When a feed fails, or suddenly returns nothing while it had posts in its window, the next build uses its posts from there instead, if they're under two days old, and says so at the bottom of the page. A feed that's just quiet isn't a failure.
- A feed that fails every build for six hours gets a "Source failing" issue here (so GitHub emails about it), opened by `alerts.py` after each build and closed once it loads again.
- `python3 -m unittest discover -s tests` checks the reader against saved samples of real feeds (`tests/fixtures`: RSS, Atom, Hacker News, a podcast, a newsletter, a homepage to find the feed of, Pocket Casts), and the fallback rules. It runs before every deploy, so a change that breaks the reader isn't published.
- Build locally with `python3 build.py`, then open `dist/index.html`. Uses only the Python standard library.
- Every push to `main` rebuilds and deploys to GitHub Pages.

## Scheduled builds

GitHub's own `schedule` trigger skips most runs, so a [cron-job.org](https://cron-job.org) job named "Newsfeed build" starts the deploy every 15 minutes instead. The schedule in `deploy.yml` stays as a backup.

- **The job:** `POST https://api.github.com/repos/grahamhagenah/news/actions/workflows/deploy.yml/dispatches` with body `{"ref":"main"}` and headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`, and `User-Agent: cron-job.org`. GitHub answers 204. Without the User-Agent header it answers 403.
- **The token:** a fine-grained token on the grahamhagenah account, limited to this repo with **Actions: Read and write** only. It can start and cancel builds but can't read or change code. It expires; renew it at github.com/settings/personal-access-tokens and paste the new one into the job's Authorization header.
- **If the page goes stale:** check cron-job.org's history or failure emails. A 401 means the token expired or was revoked, and a 403 means a missing permission or header.
- **Keep-alive:** GitHub disables a workflow after 60 days without a commit, which would also block the job. So on scheduled and dispatched runs, the `keepalive` job makes an empty commit if the repo has been quiet for 50 days.
