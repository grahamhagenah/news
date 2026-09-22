"""Draw the icons in static/ (a white house on black, with its door) as PNGs, with no dependencies. Run once after
changing the design: python3 -m housing.make_icons"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent / "static"
SAMPLES = 4  # Supersampling per axis, for smooth edges.


def inside_rounded(x, y, x0, y0, x1, y1, r):
    dx = max(x0 + r - x, 0, x - (x1 - r))
    dy = max(y0 + r - y, 0, y - (y1 - r))
    return x0 <= x <= x1 and y0 <= y <= y1 and dx * dx + dy * dy <= r * r


def png(width, rows):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, len(rows), 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def inside_polygon(x, y, points):
    """Whether (x, y) is inside the polygon, by counting the edges a ray to the right crosses."""
    inside = False
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def house(scale):
    """The house, scaled about the middle: its outline (roof and walls) and its door, in 0..1 units."""
    def at(x, y):
        return 0.5 + (x - 0.5) * scale, 0.5 + (y - 0.5) * scale
    outline = [at(0.5, 0.18), at(0.84, 0.48), at(0.74, 0.48), at(0.74, 0.82), at(0.26, 0.82), at(0.26, 0.48),
               at(0.16, 0.48)]
    door = (*at(0.43, 0.58), *at(0.57, 0.82))
    return outline, door


def draw(size, corner, scale):
    outline, (dx0, dy0, dx1, dy1) = house(scale)
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            bg = fg = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) / size
                    y = (py + (sy + 0.5) / SAMPLES) / size
                    if inside_rounded(x, y, 0, 0, 1, 1, corner):
                        bg += 1
                        if inside_polygon(x, y, outline) and not (dx0 <= x <= dx1 and dy0 <= y <= dy1):
                            fg += 1
            n = SAMPLES * SAMPLES
            v = round(255 * (fg / bg if bg else 0))
            row += bytes((v, v, v, round(255 * bg / n)))
        rows.append(bytes(row))
    return rows


# The five statuses' colors, as the site draws them.
DOTS = ["#adadad", "#e0a93b", "#4c9be8", "#55b86a", "#d65a50"]
LAND = (0x24, 0x24, 0x26)  # The map's land, along the top of the card; its water along the foot.
WATER = (0x0b, 0x15, 0x20)
BACK = (0x0a, 0x0a, 0x0b)


def rgb(text):
    return tuple(int(text[at:at + 2], 16) for at in (1, 3, 5))


def over(under, color, alpha):
    return tuple(round(a + (b - a) * alpha) for a, b in zip(under, color))


def card(width, height):
    """The picture a link to the site shows: the house on the site's black, a row of the map's rings under it in
    the statuses' colors, and a band of the map's land and water at its edges. The words of a preview come from
    the page's own title and description."""
    house_size = height * 0.34
    middle = (width / 2, height * 0.42)
    outline, (dx0, dy0, dx1, dy1) = house(1)
    radius, gap = height * 0.035, width * 0.085
    dots = [(width / 2 + (at - (len(DOTS) - 1) / 2) * gap, height * 0.74, rgb(color)) for at, color in enumerate(DOTS)]
    band = height * 0.012
    rows = []
    for py in range(height):
        line = bytearray()
        for px in range(width):
            here = LAND if py < band else WATER if py >= height - band else BACK
            covered = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x, y = px + (sx + 0.5) / SAMPLES, py + (sy + 0.5) / SAMPLES
                    hx, hy = (x - middle[0]) / house_size + 0.5, (y - middle[1]) / house_size + 0.5
                    if inside_polygon(hx, hy, outline) and not (dx0 <= hx <= dx1 and dy0 <= hy <= dy1):
                        covered += 1
            if covered:
                here = over(here, (255, 255, 255), covered / SAMPLES ** 2)
            for cx, cy, color in dots:
                ring = inside = 0
                for sy in range(SAMPLES):
                    for sx in range(SAMPLES):
                        x, y = px + (sx + 0.5) / SAMPLES, py + (sy + 0.5) / SAMPLES
                        away = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                        if abs(away - radius) <= radius * 0.13:
                            ring += 1
                        elif away < radius:
                            inside += 1
                if inside:
                    here = over(here, color, .22 * inside / SAMPLES ** 2)
                if ring:
                    here = over(here, color, ring / SAMPLES ** 2)
            line += bytes(here + (255,))
        rows.append(bytes(line))
    return rows


# The tab's icon: a rounded square, the house as big as it'll go, to read at 16–32px. Matches favicon.svg.
# Home-screen icons: square (the OS rounds the corners), the house kept inside the middle's safe zone.
if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    (OUT / "share.png").write_bytes(png(1200, card(1200, 630)))
    print("wrote share.png")
    for name, size, corner, scale in [
        ("favicon-32.png", 32, 0.22, 1.0),
        ("apple-touch-icon.png", 180, 0, 0.72),
        ("icon-192.png", 192, 0, 0.72),
        ("icon-512.png", 512, 0, 0.72),
    ]:
        (OUT / name).write_bytes(png(size, draw(size, corner, scale)))
        print("wrote", name)
