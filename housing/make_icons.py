"""Draw the icons in static/ as PNGs, with no dependencies: the site's mark on black, and the picture a link to
it shows. Run once after changing the design: python3 -m housing.make_icons

The mark is Tabler Icons' "building-community" (MIT license), drawn here as it is there: lines of an even width
with round ends and corners, on a 24×24 grid.
"""

import math
import re
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent / "static"
SAMPLES = 4  # Supersampling per axis, for smooth edges.

# Tabler Icons, "building-community" (https://tabler.io/icons, MIT). Its own path, kept as it is written there.
MARK = ("M8 9l5 5v7h-5v-4m0 4h-5v-7l5 -5m1 1v-6a1 1 0 0 1 1 -1h10a1 1 0 0 1 1 1v17h-8"
        " M13 7l0 .01 M17 7l0 .01 M17 11l0 .01 M17 15l0 .01")
MARK_GRID = 24  # The grid its path is drawn on.
MARK_WIDTH = 2  # And the width of its lines.

# The five statuses' colors, as the site draws them, for the share card.
DOTS = ["#adadad", "#e0a93b", "#4c9be8", "#55b86a", "#d65a50"]
LAND = (0x24, 0x24, 0x26)  # The map's land, along the top of the card; its water along the foot.
WATER = (0x0b, 0x15, 0x20)
BACK = (0x0a, 0x0a, 0x0b)


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


def inside_rounded(x, y, x0, y0, x1, y1, r):
    dx = max(x0 + r - x, 0, x - (x1 - r))
    dy = max(y0 + r - y, 0, y - (y1 - r))
    return x0 <= x <= x1 and y0 <= y <= y1 and dx * dx + dy * dy <= r * r


def corner_curve(start, corner, end, steps=6):
    """A rounded corner, as the few straight pieces it's drawn in."""
    points = []
    for step in range(1, steps + 1):
        t = step / steps
        points.append((
            (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * corner[0] + t ** 2 * end[0],
            (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * corner[1] + t ** 2 * end[1],
        ))
    return points


def strokes(path):
    """An SVG path as the lines it's drawn in: a list of point lists, in the path's own units. Understands the
    commands Tabler's icons use — M, m, l, h, v and a — and rounds each arc off as a corner."""
    lines, here, at = [], [], (0.0, 0.0)
    # A path writes its commands and numbers run together ("M8 9l5 5v7h-5"), so they're picked out one by one.
    bits = re.findall(r"[A-Za-z]|-?\d*\.?\d+", path)
    step = 0
    while step < len(bits):
        command = bits[step]
        if command in ("M", "m"):
            if here:
                lines.append(here)
            x, y = float(bits[step + 1]), float(bits[step + 2])
            at = (x, y) if command == "M" else (at[0] + x, at[1] + y)
            here, step = [at], step + 3
        elif command in ("l", "L"):
            x, y = float(bits[step + 1]), float(bits[step + 2])
            at = (at[0] + x, at[1] + y) if command == "l" else (x, y)
            here.append(at)
            step += 3
        elif command in ("h", "H", "v", "V"):
            away = float(bits[step + 1])
            at = ((at[0] + away, at[1]) if command == "h" else (away, at[1]) if command == "H"
                  else (at[0], at[1] + away) if command == "v" else (at[0], away))
            here.append(at)
            step += 2
        elif command in ("a", "A"):
            radius = float(bits[step + 1])
            x, y = float(bits[step + 6]), float(bits[step + 7])
            end = (at[0] + x, at[1] + y) if command == "a" else (x, y)
            # Where the two straight lines would have met, had the corner not been rounded: on along the way in.
            before = here[-2] if len(here) > 1 else at
            along = (at[0] - before[0], at[1] - before[1])
            far = math.hypot(*along) or 1
            corner = (at[0] + along[0] / far * radius, at[1] + along[1] / far * radius)
            here += corner_curve(at, corner, end)
            at = end
            step += 8
        else:  # A path can also say where not to draw (Tabler's frame): skip whatever it is.
            step += 1
    if here:
        lines.append(here)
    return lines


def mark_pieces(size, room=0.78):
    """The mark's lines, scaled to a square of this many pixels: the pieces to paint, and how wide."""
    scale = size * room / MARK_GRID
    edge = (size - MARK_GRID * scale) / 2
    pieces = []
    for line in strokes(MARK):
        points = [(edge + x * scale, edge + y * scale) for x, y in line]
        pieces += list(zip(points, points[1:])) or [(points[0], points[0])]
    return pieces, MARK_WIDTH * scale


def near(point, ends):
    """How far a point is from a piece of line."""
    (x0, y0), (x1, y1) = ends
    dx, dy = x1 - x0, y1 - y0
    along = ((point[0] - x0) * dx + (point[1] - y0) * dy) / (dx * dx + dy * dy) if (dx or dy) else 0
    along = min(1, max(0, along))
    return math.hypot(point[0] - (x0 + along * dx), point[1] - (y0 + along * dy))


def painted(size, pieces, width):
    """How much of each pixel the lines cover, 0 to 1: each piece painted where it falls, so only the pixels
    around it are ever looked at."""
    cover = [[0.0] * size for _ in range(size)]
    half = width / 2
    for ends in pieces:
        (x0, y0), (x1, y1) = ends
        for py in range(max(0, int(min(y0, y1) - half - 1)), min(size, int(max(y0, y1) + half + 2))):
            for px in range(max(0, int(min(x0, x1) - half - 1)), min(size, int(max(x0, x1) + half + 2))):
                if cover[py][px] == 1:
                    continue
                hit = sum(near((px + (sx + .5) / SAMPLES, py + (sy + .5) / SAMPLES), ends) <= half
                          for sy in range(SAMPLES) for sx in range(SAMPLES))
                if hit:
                    cover[py][px] = max(cover[py][px], hit / SAMPLES ** 2)
    return cover


def icon(size, corner=0.0, room=0.78):
    """The mark in white on the site's black, in a square with corners rounded by corner (0 to .5 of its side)."""
    pieces, width = mark_pieces(size, room)
    cover = painted(size, pieces, width)
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            ground = 1.0
            if corner:
                ground = sum(inside_rounded((px + (sx + .5) / SAMPLES) / size, (py + (sy + .5) / SAMPLES) / size,
                                            0, 0, 1, 1, corner)
                             for sy in range(SAMPLES) for sx in range(SAMPLES)) / SAMPLES ** 2
            white = round(255 * cover[py][px])
            row += bytes((white, white, white, round(255 * max(ground, cover[py][px]))))
        rows.append(bytes(row))
    return rows


def rgb(text):
    return tuple(int(text[at:at + 2], 16) for at in (1, 3, 5))


def over(under, color, alpha):
    return tuple(round(a + (b - a) * alpha) for a, b in zip(under, color))


def card(width, height):
    """The picture a link to the site shows: the mark, a row of the map's rings under it in the statuses' colors,
    and a band of the map's land and water at its edges. A preview's words are the page's own."""
    mark = round(height * 0.34)
    pieces, line = mark_pieces(mark, room=1)
    left, top = round((width - mark) / 2), round(height * 0.26)
    cover = painted(mark, pieces, line)
    radius, gap = height * 0.035, width * 0.085
    dots = [(width / 2 + (at - (len(DOTS) - 1) / 2) * gap, height * 0.74, rgb(color)) for at, color in enumerate(DOTS)]
    band = height * 0.012
    rows = []
    for py in range(height):
        row = bytearray()
        for px in range(width):
            here = LAND if py < band else WATER if py >= height - band else BACK
            mx, my = px - left, py - top
            if 0 <= mx < mark and 0 <= my < mark and cover[my][mx]:
                here = over(here, (255, 255, 255), cover[my][mx])
            for cx, cy, color in dots:
                ring = inside = 0
                for sy in range(SAMPLES):
                    for sx in range(SAMPLES):
                        away = math.hypot(px + (sx + .5) / SAMPLES - cx, py + (sy + .5) / SAMPLES - cy)
                        if abs(away - radius) <= radius * 0.13:
                            ring += 1
                        elif away < radius:
                            inside += 1
                if inside:
                    here = over(here, color, .22 * inside / SAMPLES ** 2)
                if ring:
                    here = over(here, color, ring / SAMPLES ** 2)
            row += bytes(here + (255,))
        rows.append(bytes(row))
    return rows


# The tab's icon: a rounded square, the mark as big as it'll go, to read at 16–32px. Matches favicon.svg.
# Home-screen icons: square (the OS rounds the corners), the mark kept inside the middle's safe zone.
if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    (OUT / "share.png").write_bytes(png(1200, card(1200, 630)))
    print("wrote share.png")
    for name, size, corner, room in [
        ("favicon-32.png", 32, 0.22, 0.84),
        ("apple-touch-icon.png", 180, 0, 0.62),
        ("icon-192.png", 192, 0, 0.62),
        ("icon-512.png", 512, 0, 0.62),
    ]:
        (OUT / name).write_bytes(png(size, icon(size, corner, room)))
        print("wrote", name)
