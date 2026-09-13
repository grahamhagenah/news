"""The images a link to Boston, Daily shows when it's shared (1200×630): one for the home page, one for each
kind's page. They don't change with the listings, so they're made by hand, not by each build, with Chrome
installed: python3 -m events.share_cards. Run it again after renaming the site or rewording a page; it writes
events/share/*.png, which the build copies to the public site."""

import html
import shutil
import subprocess
import tempfile
from pathlib import Path

from events import build

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT = Path(__file__).parent / "share"
COLORS = {"music": "#a78bfa", "film": "#fbbf24", "art": "#60a5fa"}

# The home card, then each kind's: its file, heading, the line under it, and the icons it shows.
CARDS = [
    ("home", build.PUBLIC_NAME, "Concerts, films, and talks around Boston, Cambridge, and Somerville.", list(COLORS), ""),
    ("music", "Concerts around Boston", "Clubs, bars and halls across Boston, Cambridge, and Somerville.", ["music"], "music/"),
    ("film", "Films around Boston", "Repertory screenings to new releases, at theaters across Boston, Cambridge, and Somerville.", ["film"], "film/"),
    ("talks", "Art & talks around Boston", "Artist talks, lectures and exhibitions around Boston, Cambridge, and Somerville.", ["art"], "talks/"),
    ("weekend", "This weekend in Boston", "Concerts, films, and talks, Friday to Sunday, across Boston, Cambridge, and Somerville.", list(COLORS), "weekend/"),
]


def card(heading, line, kinds, path):
    icons = "".join(
        f'<svg viewBox="0 0 16 16" style="color:{COLORS[kind]}"><g fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round">{build.ICON_DRAWINGS[kind]}</g></svg>'
        for kind in kinds
    )
    size = 92 if len(heading) <= 22 else 80  # A longer heading a little smaller, to keep it on one line.
    site = "" if heading == build.PUBLIC_NAME else f"<span>{html.escape(build.PUBLIC_NAME)}</span> · "
    address = build.PUBLIC_URL.removeprefix("https://").rstrip("/") + ("/" + path.rstrip("/") if path else "")
    return f"""<!doctype html><meta charset="utf-8"><style>
  html, body {{ margin: 0; width: 1200px; height: 630px; background: #000; }}
  body {{ box-sizing: border-box; display: flex; flex-direction: column; padding: 84px 96px 72px;
          color: #fff; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }}
  .icons {{ display: flex; gap: 28px; margin-bottom: 44px; }}
  .icons svg {{ width: 58px; height: 58px; }}
  h1 {{ margin: 0; font-size: {size}px; font-weight: 700; letter-spacing: -.02em; line-height: 1.02; white-space: nowrap; }}
  p {{ margin: 30px 0 0; max-width: 940px; color: #8c8c8c; font-size: 38px; line-height: 1.35; text-wrap: balance; }}
  footer {{ margin-top: auto; color: #5c5c5c; font-size: 28px; }}
  footer span {{ color: #8c8c8c; }}
</style>
<div class="icons">{icons}</div>
<h1>{html.escape(heading)}</h1>
<p>{html.escape(line)}</p>
<footer>{site}{html.escape(address)}</footer>
"""


def main():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for name, heading, line, kinds, path in CARDS:
            page = Path(scratch) / f"{name}.html"
            page.write_text(card(heading, line, kinds, path))
            subprocess.run([CHROME, "--headless=new", "--hide-scrollbars", "--force-device-scale-factor=1",
                            "--window-size=1200,630", f"--screenshot={Path(scratch) / name}.png", page.as_uri()],
                           check=True, capture_output=True)
            shutil.move(Path(scratch) / f"{name}.png", OUT / f"{name}.png")
            print(f"Wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}/{name}.png")


if __name__ == "__main__":
    main()
