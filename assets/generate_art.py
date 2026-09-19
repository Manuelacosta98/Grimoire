#!/usr/bin/env python3
"""Generate the Grimoire logo and banner as pixel art.

Pure standard library — no Pillow, no build step. Writes PNG (for the README, where
GitHub renders it reliably) and SVG (crisp at any size, diffable in review).

The art is defined as code rather than hand-placed rects so it can be regenerated at a
different scale without redrawing anything.

    python3 assets/generate_art.py            # write PNG + SVG
    python3 assets/generate_art.py --preview  # draw it in the terminal instead
"""

import argparse
import os
import struct
import sys
import zlib

# ----------------------------------------------------------------------- palette

PALETTE = {
    "void": "#0b0a14",   # page background
    "deep": "#141127",   # panel background
    "edge": "#07060f",   # 1px outlines and shadow
    "dark": "#2a1d50",   # cover, shaded
    "mid": "#3f2c78",    # cover, base
    "lite": "#5c43a8",   # cover, lit face
    "glow": "#8b6ef0",   # arcane highlight
    "gold": "#f0b429",   # clasps and rule lines
    "gold2": "#a8761a",  # gold, shaded
    "page": "#ede0c8",   # parchment
    "page2": "#bfae8e",  # parchment, shaded
    "aws": "#ff9900",    # the "forged on live AWS" accent
    "white": "#fdfcff",
    "star": "#2e2752",
}


def rgba(name):
    if name is None:
        return (0, 0, 0, 0)
    h = PALETTE[name].lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


# ------------------------------------------------------------------------ canvas


class Canvas(object):
    """A grid of palette names. None means transparent."""

    def __init__(self, width, height, fill=None):
        self.w = width
        self.h = height
        self.px = [[fill for _ in range(width)] for _ in range(height)]

    def set(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = color

    def get(self, x, y):
        if 0 <= x < self.w and 0 <= y < self.h:
            return self.px[y][x]
        return None

    def rect(self, x, y, w, h, color):
        for row in range(y, y + h):
            for col in range(x, x + w):
                self.set(col, row, color)

    def outline(self, x, y, w, h, color):
        for col in range(x, x + w):
            self.set(col, y, color)
            self.set(col, y + h - 1, color)
        for row in range(y, y + h):
            self.set(x, row, color)
            self.set(x + w - 1, row, color)

    def hline(self, x, y, w, color):
        self.rect(x, y, w, 1, color)

    def vline(self, x, y, h, color):
        self.rect(x, y, 1, h, color)

    def stamp(self, x, y, rows, legend):
        """Paint a small pattern. Characters not in `legend` are left untouched."""
        for dy, row in enumerate(rows):
            for dx, char in enumerate(row):
                if char in legend:
                    self.set(x + dx, y + dy, legend[char])

    def blit(self, other, x, y, scale=1):
        for sy in range(other.h):
            for sx in range(other.w):
                color = other.px[sy][sx]
                if color is None:
                    continue
                for ry in range(scale):
                    for rx in range(scale):
                        self.set(x + sx * scale + rx, y + sy * scale + ry, color)


# -------------------------------------------------------------------------- font

# 5x7 bitmap font. Only the glyphs the artwork needs.
FONT = {
    "A": ".###.|#...#|#...#|#####|#...#|#...#|#...#",
    "B": "####.|#...#|#...#|####.|#...#|#...#|####.",
    "C": ".####|#....|#....|#....|#....|#....|.####",
    "D": "####.|#...#|#...#|#...#|#...#|#...#|####.",
    "E": "#####|#....|#....|####.|#....|#....|#####",
    "F": "#####|#....|#....|####.|#....|#....|#....",
    "G": ".###.|#...#|#....|#.###|#...#|#...#|.###.",
    "H": "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    "I": "#####|..#..|..#..|..#..|..#..|..#..|#####",
    "K": "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#",
    "L": "#....|#....|#....|#....|#....|#....|#####",
    "M": "#...#|##.##|#.#.#|#...#|#...#|#...#|#...#",
    "N": "#...#|##..#|#.#.#|#..##|#...#|#...#|#...#",
    "O": ".###.|#...#|#...#|#...#|#...#|#...#|.###.",
    "P": "####.|#...#|#...#|####.|#....|#....|#....",
    "R": "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    "S": ".####|#....|#....|.###.|....#|....#|####.",
    "T": "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    "U": "#...#|#...#|#...#|#...#|#...#|#...#|.###.",
    "V": "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    "W": "#...#|#...#|#...#|#...#|#.#.#|##.##|#...#",
    "Y": "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..",
    "-": ".....|.....|.....|.###.|.....|.....|.....",
    ".": ".....|.....|.....|.....|.....|.....|..#..",
    ",": ".....|.....|.....|.....|.....|..#..|.#...",
    "'": "..#..|..#..|.....|.....|.....|.....|.....",
    " ": ".....|.....|.....|.....|.....|.....|.....",
}

GLYPH_W, GLYPH_H = 5, 7


def text_width(s, scale=1, tracking=1):
    if not s:
        return 0
    return (len(s) * (GLYPH_W + tracking) - tracking) * scale


def draw_text(canvas, x, y, s, color, scale=1, tracking=1):
    cursor = x
    for char in s.upper():
        glyph = FONT.get(char)
        if glyph is None:
            cursor += (GLYPH_W + tracking) * scale
            continue
        for dy, row in enumerate(glyph.split("|")):
            for dx, cell in enumerate(row):
                if cell != "#":
                    continue
                for ry in range(scale):
                    for rx in range(scale):
                        canvas.set(cursor + dx * scale + rx, y + dy * scale + ry, color)
        cursor += (GLYPH_W + tracking) * scale
    return cursor


# --------------------------------------------------------------------- the book

RUNE = [
    "..#..",
    ".###.",
    "##.##",
    "#.#.#",
    "##.##",
    ".###.",
    "..#..",
]


def draw_book(size=32):
    """The grimoire itself: a closed spellbook, front-on, with a lit sigil."""
    c = Canvas(size, size)

    # A single offset row of shadow, so the book sits on something without
    # looking like it is mounted on a plinth.
    c.rect(5, 28, 21, 1, "edge")
    c.rect(7, 29, 17, 1, "edge")

    # Parchment block, peeking out along the right edge.
    c.rect(19, 6, 9, 21, "page2")
    c.rect(19, 6, 8, 20, "page")
    for y in range(8, 25, 3):
        c.hline(22, y, 5, "page2")

    # Cover: outline first, then the face on top of it.
    c.rect(3, 4, 20, 24, "edge")
    c.rect(4, 5, 18, 22, "mid")

    # Spine down the left: shaded, with a highlight that reads as a fold.
    c.rect(4, 5, 5, 22, "dark")
    c.vline(5, 6, 20, "lite")
    c.vline(8, 5, 22, "edge")

    # Lit top edge, shaded bottom edge, so the cover has a direction of light.
    c.hline(9, 5, 13, "lite")
    c.hline(9, 26, 13, "dark")
    c.vline(21, 6, 20, "dark")

    # Gold rule inset into the cover.
    c.outline(10, 8, 11, 16, "gold2")
    c.outline(11, 9, 9, 14, "gold")
    c.rect(12, 10, 7, 12, "mid")

    # The sigil, glowing, with the AWS accent at its poles.
    c.stamp(13, 12, RUNE, {"#": "glow"})
    c.set(15, 15, "white")
    c.set(15, 13, "aws")
    c.set(15, 17, "aws")

    # Two clasps, emerging from the cover edge and reaching over the page block.
    # They start outboard of the gold rule so the two gold elements stay distinct.
    for y in (11, 19):
        c.rect(21, y, 5, 3, "gold2")
        c.rect(21, y, 5, 2, "gold")
        c.set(25, y, "edge")
        c.set(25, y + 2, "edge")

    return c


# ------------------------------------------------------------------- the pieces


def build_logo(size=32):
    return draw_book(size)


def starfield(c, seed=7):
    """A deterministic scatter of faint dots, so the panel is not flat."""
    state = seed
    for _ in range(90):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        x = state % c.w
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        y = state % c.h
        if c.get(x, y) in ("deep", "void"):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            c.set(x, y, "star" if state % 4 else "glow")


def build_banner():
    w, h = 400, 120
    c = Canvas(w, h, "void")
    c.rect(3, 3, w - 6, h - 6, "deep")
    starfield(c)

    # Double border: a thin outer rule and an inner gold hairline at the corners.
    c.outline(0, 0, w, h, "edge")
    c.outline(2, 2, w - 4, h - 4, "dark")
    for cx, cy in ((2, 2), (w - 7, 2), (2, h - 7), (w - 7, h - 7)):
        c.rect(cx, cy, 5, 1, "gold")
        c.rect(cx, cy, 1, 5, "gold")

    c.blit(build_book_for_banner(), 22, 26, scale=2)

    title_x = 118
    draw_text(c, title_x, 30, "GRIMOIRE", "white", scale=4, tracking=1)
    # Underline the title with a gold rule that fades into the accent.
    rule_w = text_width("GRIMOIRE", scale=4)
    c.hline(title_x, 62, rule_w, "gold2")
    c.hline(title_x, 63, rule_w // 2, "gold")
    c.hline(title_x + rule_w // 2, 63, rule_w - rule_w // 2, "aws")

    draw_text(c, title_x + 2, 74, "SPELLBOOK OF AI SKILLS", "glow", scale=2)
    draw_text(c, title_x + 2, 94, "FREE TO STEAL", "aws", scale=2)
    return c


def build_book_for_banner():
    return draw_book(32)


# ------------------------------------------------------------------- exporters


def write_png(path, canvas, scale=1):
    raw = bytearray()
    for row in canvas.px:
        for _ in range(scale):
            raw.append(0)
            for color in row:
                r, g, b, a = rgba(color)
                for _ in range(scale):
                    raw += bytes((r, g, b, a))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", canvas.w * scale, canvas.h * scale, 8, 6, 0, 0, 0),
    )
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)
    return len(png)


def write_svg(path, canvas, scale=1):
    """Emit one rect per horizontal run of colour.

    The most common colour is painted once as a backdrop and then skipped, which
    matters for the banner: a starfield scattered over a flat background otherwise
    breaks every run into single pixels and quadruples the file.
    """
    counts = {}
    for row in canvas.px:
        for color in row:
            if color is not None:
                counts[color] = counts.get(color, 0) + 1

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" shape-rendering="crispEdges" '
        'role="img" aria-label="Grimoire">'
        % (canvas.w * scale, canvas.h * scale, canvas.w, canvas.h)
    ]

    backdrop = max(counts, key=counts.get) if counts else None
    if backdrop is not None:
        parts.append(
            '<rect width="%d" height="%d" fill="%s"/>'
            % (canvas.w, canvas.h, PALETTE[backdrop])
        )

    for y, row in enumerate(canvas.px):
        x = 0
        while x < canvas.w:
            color = row[x]
            if color is None or color == backdrop:
                x += 1
                continue
            run = 1
            while x + run < canvas.w and row[x + run] == color:
                run += 1
            parts.append(
                '<rect x="%d" y="%d" width="%d" height="1" fill="%s"/>'
                % (x, y, run, PALETTE[color])
            )
            x += run
    parts.append("</svg>")
    svg = "".join(parts)
    with open(path, "w") as handle:
        handle.write(svg)
    return len(svg)


ASCII_KEY = {
    None: " ",
    "void": ".",
    "deep": ",",
    "star": "'",
    "edge": "#",
    "dark": "d",
    "mid": "m",
    "lite": "l",
    "glow": "*",
    "gold": "G",
    "gold2": "g",
    "page": "P",
    "page2": "p",
    "aws": "O",
    "white": "@",
}


def preview(canvas, ansi=True):
    """Draw the art in a terminal: ANSI blocks, or ASCII when colour is useless."""
    out = []
    for row in canvas.px:
        line = []
        for color in row:
            if ansi:
                r, g, b, a = rgba(color)
                line.append("\x1b[0m  " if a == 0 else "\x1b[48;2;%d;%d;%dm  " % (r, g, b))
            else:
                line.append(ASCII_KEY.get(color, "?"))
        out.append("".join(line) + ("\x1b[0m" if ansi else ""))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--preview", action="store_true", help="Draw in the terminal.")
    parser.add_argument("--ascii", action="store_true", help="Preview as ASCII, not colour.")
    parser.add_argument("--only", choices=["logo", "banner"], help="Limit to one piece.")
    parser.add_argument("--logo-scale", type=int, default=8)
    parser.add_argument("--banner-scale", type=int, default=3)
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    pieces = {"logo": build_logo(), "banner": build_banner()}
    scales = {"logo": args.logo_scale, "banner": args.banner_scale}
    # The logo also ships as SVG, where it is small and useful for favicons and docs.
    # The banner does not: a starfield over a flat background costs ~130 KB of rects
    # for no benefit the 3x PNG does not already give. Regenerate it here if needed.
    formats = {"logo": ("png", "svg"), "banner": ("png",)}

    if args.preview:
        for name, canvas in pieces.items():
            if args.only and name != args.only:
                continue
            print("\n%s  (%dx%d)" % (name, canvas.w, canvas.h))
            print(preview(canvas, ansi=not args.ascii))
        return 0

    for name, canvas in pieces.items():
        if args.only and name != args.only:
            continue
        written = []
        if "png" in formats[name]:
            size = write_png(os.path.join(here, "%s.png" % name), canvas, scales[name])
            written.append("png %6d B" % size)
        if "svg" in formats[name]:
            size = write_svg(os.path.join(here, "%s.svg" % name), canvas, scales[name])
            written.append("svg %6d B" % size)
        print(
            "%-7s %4dx%-4d @%dx  %s"
            % (name, canvas.w, canvas.h, scales[name], "  ".join(written))
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
