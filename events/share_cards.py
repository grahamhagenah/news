"""The images a link to Pushpin shows when it's shared (1200×630): for each city (events/share/<its slug>), one for
its home page and one for each kind's page; and one for pushpin.city itself (events/share/site). They don't change
with the listings, so they're made by hand, not by each build, with Chrome installed: python3 -m events.share_cards.
Run it again after adding a city, renaming one or rewording a page; it writes them all, and
events/pushpin/apple-touch-icon.png, which the build copies to the public site."""

import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from events import build

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT = Path(__file__).parent / "share"
PUSHPIN = Path(__file__).parent / "pushpin"  # The site's own icons: favicon.svg, and the home screen's, made here.
COLORS = {"music": "#a78bfa", "film": "#fbbf24", "art": "#60a5fa"}

# Each city's cards after its home page's: each's file, heading, the line under it, the icons it shows, and its page.
CARDS = {
    "boston": [
        ("music", "Concerts around Boston", "Clubs, bars and halls across Boston, Cambridge, and Somerville.", ["music"], "music/"),
        ("film", "Films around Boston", "Repertory screenings to new releases, at theaters across Boston, Cambridge, and Somerville.", ["film"], "film/"),
        ("talks", "Art & talks around Boston", "Artist talks, lectures and exhibitions around Boston, Cambridge, and Somerville.", ["art"], "talks/"),
        ("tonight", "Tonight in Boston", "Concerts, films, and talks still to come today, across Boston, Cambridge, and Somerville.", list(COLORS), "tonight/"),
        ("weekend", "This weekend in Boston", "Concerts, films, and talks, Friday to Sunday, across Boston, Cambridge, and Somerville.", list(COLORS), "weekend/"),
    ],
    "westernma": [
        ("music", "Concerts in Western Mass", "Concerts across the Pioneer Valley and the Berkshires.", ["music"], "music/"),
        ("film", "Films in Western Mass", "Independent theaters across the Pioneer Valley and the Berkshires.", ["film"], "film/"),
        ("talks", "Art & talks in Western Mass", "Talks, readings, performances and exhibitions across the Pioneer Valley and the Berkshires.", ["art"], "talks/"),
        ("tonight", "Tonight in Western Mass", "Concerts, films, and talks still to come today, across the Pioneer Valley and the Berkshires.", list(COLORS), "tonight/"),
        ("weekend", "This weekend in Western Mass", "Concerts, films, and talks, Friday to Sunday, across the Pioneer Valley and the Berkshires.", list(COLORS), "weekend/"),
    ],
    "bayarea": [
        ("music", "Concerts in the Bay Area", "Clubs and halls across San Francisco and Oakland.", ["music"], "music/"),
        ("film", "Films in the Bay Area", "Repertory screenings to new releases, at theaters in San Francisco and Oakland.", ["film"], "film/"),
        ("talks", "Art & talks in the Bay Area", "Artist talks, exhibitions and museum nights in San Francisco and Oakland.", ["art"], "talks/"),
        ("tonight", "Tonight in the Bay Area", "Concerts, films, and talks still to come today, across San Francisco and Oakland.", list(COLORS), "tonight/"),
        ("weekend", "This weekend in the Bay Area", "Concerts, films, and talks, Friday to Sunday, across San Francisco and Oakland.", list(COLORS), "weekend/"),
    ],
}
# pushpin.city's own: every city's.
SITE_CARD = ("home", "Pushpin", "Concerts, films, and talks, aggregated from select venues, in Boston, Western Mass and the Bay Area.", list(COLORS), None)


def card(heading, line, kinds, path):
    """A card, for the city use_city() last pointed build's names at; with path None, pushpin.city's own."""
    icons = "".join(
        f'<svg viewBox="0 0 16 16" style="color:{COLORS[kind]}"><g fill="none" stroke="currentColor" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round">{build.ICON_DRAWINGS[kind]}</g></svg>'
        for kind in kinds
    )
    # The site's name with its pin before it, as in the header: a home card's heading, the others' last line.
    home = path is None or heading == build.PUBLIC_NAME
    # A longer heading a little smaller, to keep it on one line; the pin takes about three letters' room.
    length = len(heading) + 3 * home
    size = 92 if length <= 22 else 80 if length <= 25 else 70
    title = f"{build.PIN_MARK}{html.escape(heading)}" if home else html.escape(heading)
    site = "" if home else f"<span>{build.PIN_MARK}{html.escape(build.PUBLIC_NAME)}</span> · "
    base = build.PUBLIC_SITE if path is None else build.PUBLIC_URL
    address = base.removeprefix("https://").rstrip("/") + ("/" + path.rstrip("/") if path else "")
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
  .pin {{ width: .9em; height: .9em; margin-right: .2em; vertical-align: -.1em; color: #fff; }}
  footer .pin {{ width: 1em; height: 1em; margin-right: .3em; vertical-align: -.15em; }}
</style>
<div class="icons">{icons}</div>
<h1>{title}</h1>
<p>{html.escape(line)}</p>
<footer>{site}{html.escape(address)}</footer>
"""


def touch_icon():
    """The icon an iPhone puts on its home screen (180×180), which it won't take as SVG: the favicon's pin in
    white, on black to the edges (the phone rounds the corners itself, and fills any transparency with black)."""
    pin = re.search(r"<g .*?</g>", (PUSHPIN / "favicon.svg").read_text()).group().replace("<g ", '<g stroke="#fff" ', 1)
    return (f'<!doctype html><style>html, body {{ margin: 0; width: 180px; height: 180px; background: #000; }}</style>'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="180" height="180" viewBox="0 0 180 180">'
            f'<svg x="30" y="30" width="120" height="120" viewBox="0 0 24 24">{pin}</svg></svg>')


def shoot(html_page, size, destination, scratch):
    page = Path(scratch) / (destination.stem + ".html")
    page.write_text(html_page)
    subprocess.run([CHROME, "--headless=new", "--hide-scrollbars", "--force-device-scale-factor=1",
                    f"--window-size={size[0]},{size[1]}", f"--screenshot={Path(scratch) / destination.name}", page.as_uri()],
                   check=True, capture_output=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(Path(scratch) / destination.name, destination)
    print(f"Wrote {destination.relative_to(Path.cwd()) if destination.is_relative_to(Path.cwd()) else destination}")


def main():
    with tempfile.TemporaryDirectory() as scratch:
        for city in build.CITIES:
            build.use_city(city)
            home = ("home", build.PUBLIC_NAME, build.PUBLIC_TAGLINE, list(COLORS), "")
            for name, heading, line, kinds, path in [home] + CARDS[city.slug]:
                shoot(card(heading, line, kinds, path), (1200, 630), OUT / city.slug / f"{name}.png", scratch)
        build.use_city(build.BOSTON_CITY)
        name, heading, line, kinds, path = SITE_CARD
        shoot(card(heading, line, kinds, path), (1200, 630), OUT / "site" / f"{name}.png", scratch)
        shoot(touch_icon(), (180, 180), PUSHPIN / "apple-touch-icon.png", scratch)


if __name__ == "__main__":
    main()
