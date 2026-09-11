# Reader

A plain black-and-white list of the latest posts from a few sites.

- Add or remove sites in `feeds.txt`. A homepage URL is enough; the build finds the feed.
- Build locally with `python3 build.py`, then open `dist/index.html`. Uses only the Python standard library.
- GitHub Actions rebuilds and deploys to GitHub Pages on every push and every 30 minutes.
