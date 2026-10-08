#!/usr/bin/env python3
"""Write a standalone copy of the newsfeed, for someone to run as their own: its code, its tests, a starter
feeds.txt and a workflow that builds and publishes it, with none of the other pages' code. Run it as
`python3 -m news.template <folder>`.

What it writes is published as the grahamhagenah/newsfeed repo, marked as a template, so "Use this template"
gives a clean repo of their own. The newsfeed takes the repo and address it's published at from the
environment (REPO, SITE_URL in build.py), so a copy needs nothing changed to point at theirs.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
# Copied as they are. Everything the newsfeed needs and nothing else: not events/, housing/, their tests or
# their fixtures.
COPIED = ["news/__init__.py", "news/build.py", "news/sources.py", "news/make_icons.py", "news/static",
          "shared/__init__.py", "shared/site.py", "shared/alerts.py",
          "tests/__init__.py", "tests/test_news.py", "tests/test_shared.py", "tests/fixtures/news"]

FEEDS = """\
# One site per line. A homepage is fine — the build finds its feed.
# Add a name after the URL to use instead of the feed's own title.
# Options at the end of a line: limit=5 (at most 5 posts), days=14 (keep posts for 14 days), and
# only="Full Performance" (only posts whose titles have that in them, in any case).
# Defaults are 15 posts from the last 3 days.
# A podcast's episodes link to Apple Podcasts. YouTube channels work the same way (a channel's page is
# enough) and their videos play on the page.
# Delete these and add your own.

https://kottke.org/
https://waxy.org/               Waxy.org   days=7
https://hnrss.org/frontpage?points=150   Hacker News
https://feeds.bbci.co.uk/news/rss.xml   BBC News   limit=8
https://www.theguardian.com/news/series/the-long-read/rss   The Long Read   days=7
https://www.youtube.com/@veritasium   Veritasium   days=14
"""

WORKFLOW = """\
name: Build and deploy

# Builds the newsfeed and publishes it to this repo's GitHub Pages site. Nothing here needs a key or an
# account anywhere else.
on:
  push:
    branches: [main]
  schedule:
    # GitHub runs schedules when it can and drops a lot of them, most often at :00 and :30, when everyone's
    # cron fires. Odd minutes are quieter. See the README for making it reliable.
    - cron: "7,22,37,52 * * * *"
  workflow_dispatch:

permissions:
  contents: write  # The keepalive commit below.
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: ${{ github.event_name == 'push' }}

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v5
      # The feed readers against saved samples of real feeds, so a change that breaks them isn't published.
      - run: python3 -m unittest
      - run: python3 -m news.build
        env:
          # Where it's published, if you've put it on a domain of your own. Leave it out otherwise.
          SITE_URL: ${{ vars.SITE_URL }}
          # A free YouTube Data API key, for a channel whose feed won't load. Leave it out otherwise.
          YOUTUBE_KEY: ${{ secrets.YOUTUBE_KEY }}
      - uses: actions/upload-pages-artifact@v4
        with:
          path: dist/news
      - id: deployment
        uses: actions/deploy-pages@v4

  # GitHub turns off a repo's schedules after 60 days without a commit, which would freeze the page. If the
  # repo has been quiet for 50 days, an empty commit resets that clock.
  keepalive:
    if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v5
      - run: |
          if [ $(( $(date +%s) - $(git log -1 --format=%ct) )) -gt $(( 50 * 86400 )) ]; then
            git config user.name "github-actions[bot]"
            git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
            git commit --allow-empty -m "Keep the scheduled build running"
            git push
          fi
"""

IGNORE = "dist/\n__pycache__/\n"

README = """\
# Newsfeed

A plain black page listing the latest posts, podcast episodes and videos from the sites you choose, newest
first. No accounts, no tracking, nothing to pay for: GitHub builds it every so often and publishes it.

**[Make your own →](https://news.grahamhagenah.com/make-your-own/)** — the steps in full, with pictures of
what you get.

In short:

1. Press **Use this template** above, and name your repo.
2. In your repo's **Settings → Pages**, set **Source** to **GitHub Actions**.
3. In the **Actions** tab, press the button to enable workflows, then run **Build and deploy**.
4. Your page is at `https://<your username>.github.io/<your repo>/`.

Then add your own sites by editing [`news/feeds.txt`](news/feeds.txt), or from the page's own **Add a site**
form, which opens a pre-filled issue that a bot applies for you.

## Adding a site

One per line. A site's homepage is enough — the build finds its feed:

    https://kottke.org/
    https://www.theguardian.com/news/series/the-long-read/rss   The Long Read   days=7

Anything after the address is the name to show. Then options:

- `limit=5` — at most 5 posts from that site, for one that posts constantly.
- `days=14` — keep its posts for 14 days, for a blog that posts rarely. The default is 3.
- `only="Full Performance"` — only posts whose titles have that in them.

**YouTube channels** work like any site: paste the channel's page. Its videos play on the page itself, in a
small window, and Shorts are left out. **Podcasts** work too: paste the show's feed and its episodes open in
Apple Podcasts.

## How often it updates

Every 15 minutes, in theory. In practice GitHub runs only a fraction of scheduled builds — often fewer than
one in ten — so the page can go a few hours stale. The README of the original explains how to have a free
cron service start the builds instead, which makes it reliable. Until then, **Actions → Build and deploy →
Run workflow** updates it at once.

## Running it on your own computer

    python3 -m news.build     # writes dist/news; open its index.html
    python3 -m unittest       # the feed readers, against saved samples

Nothing but the Python standard library.

---

Built from [grahamhagenah/news](https://github.com/grahamhagenah/news), which also runs
[Pushpin](https://pushpin.city) and a housing tracker. This copy is the newsfeed alone.
"""


def write(folder):
    """The newsfeed as a repo of its own, in folder, replacing whatever was there before."""
    folder = Path(folder)
    if folder.exists():
        shutil.rmtree(folder)
    for path in COPIED:
        source, into = ROOT / path, folder / path
        into.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, into)
        else:
            shutil.copy2(source, into)
    (folder / "news" / "feeds.txt").write_text(FEEDS)
    (folder / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (folder / ".github" / "workflows" / "deploy.yml").write_text(WORKFLOW)
    (folder / ".gitignore").write_text(IGNORE)
    (folder / "README.md").write_text(README)
    return folder


def main():
    folder = write(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / "newsfeed-template")
    # It has to stand on its own: its tests, then a build, run from inside it.
    for what in (["python3", "-m", "unittest"], ["python3", "-m", "news.build"]):
        done = subprocess.run(what, cwd=folder, capture_output=True, text=True)
        if done.returncode:
            sys.exit(f"{' '.join(what)} failed in the copy:\n{done.stdout[-2000:]}{done.stderr[-2000:]}")
    print(f"Wrote the newsfeed on its own to {folder}, and its tests and a build passed there.")


if __name__ == "__main__":
    main()
