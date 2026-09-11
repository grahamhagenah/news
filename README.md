# Newsfeed

A plain black-and-white list of the latest posts from a few sites, newest first. Live at https://grahamhagenah.com/news/.

- Add or remove sites in `feeds.txt`. A homepage URL is enough; the build finds the feed. Options like `limit=5` and `days=14` go at the end of a line. Full steps are on https://grahamhagenah.com/news/sources.html.
- Build locally with `python3 build.py`, then open `dist/index.html`. Uses only the Python standard library.
- GitHub Actions rebuilds and deploys to GitHub Pages on every push and about every 15 minutes, and makes an empty commit if the repo goes 50 days without one, so GitHub doesn't switch the schedule off.
