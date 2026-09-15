"""The pages left at boston.grahamhagenah.com, where Pushpin Boston was first published (as Boston, Daily): each
of its old addresses sends visitors and search engines on to the same page at pushpin.city/boston/, keeping
any ?venue= or ?q=. They don't change, so they're made once, into dist/moved, with python3 -m events.moved,
and published from the grahamhagenah/boston-daily repo's gh-pages branch."""

from pathlib import Path

from events import build

OLD_DOMAIN = "boston.grahamhagenah.com"
OUT = build.ROOT.parent / "dist" / "moved"
# Every page the site had there, and a page for any other address, which follows it to the same path.
PAGES = ["", "about.html", "contact.html", build.PUBLIC_TONIGHT[0]] + [path for path, *_ in build.PUBLIC_WEEKENDS] + [
    f"{slug}/" for slug, *_ in build.PUBLIC_PAGES.values()]


def main():
    for path in PAGES:
        page = OUT / (path + "index.html" if path == "" or path.endswith("/") else path)
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(build.render_redirect(build.PUBLIC_URL + path))
    (OUT / "404.html").write_text(build.render_redirect(build.PUBLIC_URL, paths=True))
    (OUT / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    (OUT / "CNAME").write_text(OLD_DOMAIN + "\n")
    (OUT / ".nojekyll").write_text("")
    print(f"Wrote {len(PAGES) + 1} pages to {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}")


if __name__ == "__main__":
    main()
