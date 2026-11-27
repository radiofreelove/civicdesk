#!/usr/bin/env python3
"""Generate CivicDesk's faceted desk mark, icons, and 1200x630 Open Graph card.

    python3 scripts/make_mark.py          # write templates/brand-mark.svg, static/favicon.svg, static/apple-touch-icon.png, static/og-card.png
    python3 scripts/make_mark.py --check  # exit 1 if generated files are out of date (or validate custom files when brand.custom_artwork is true)

The mark is the old five-stripe service-desk mark cut as a gem. Its five bands become the
crown (two bands), the girdle (the wide middle band, the service counter), and the pavilion
(two bands). A ridge splits every band into a lit facet and a shaded facet. The ridge is drawn
as a ray of light that bends where it meets the girdle and bends back as it leaves, the way a
prism refracts light, and the light and dark tints trade sides above and below the girdle.
Every facet is a Broadsheet press ink or a tint of one; process yellow lights the girdle.

Facet colors on #201E1D press ink (non-text contrast, all >= 3:1):
  sky    #0088B0 process cyan       4.07:1     sky-2  #6DB8CE cyan tint       7.6:1
  rose   #D6006C process magenta    3.22:1     rose-2 #E36DA8 magenta tint    5.5:1
  paper  #F3F2F2 newsprint         14.86:1     gold   #EDBB00 process yellow  9.3:1
The header copy uses CSS classes, so each reading theme can retint the facets.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

COLORS = {
    "bg": "#201E1D",
    "sky": "#0088B0",
    "sky-2": "#6DB8CE",
    "rose": "#D6006C",
    "rose-2": "#E36DA8",
    "paper": "#F3F2F2",
    "gold": "#EDBB00",
}

GAP = 0.7          # ink showing between facets
CX = 20.0          # vertical ridge
TIP_TOP, TIP_BOTTOM = 4.0, 36.0
SHOULDER = 12.0    # crown and pavilion half-width where they meet the girdle
GIRDLE = (17.6, 22.4, 15.0)  # top, bottom, half-width
BANDS = [(4.0, 10.4), (11.1, 16.9), (23.1, 28.9), (29.6, 36.0)]


def _r(v: float) -> float:
    return round(v, 2)


def _half_width(y: float) -> float:
    """Half-width of the crown (above the girdle) or pavilion (below) at height y."""
    if y <= 20:
        return SHOULDER * (y - TIP_TOP) / (17.0 - TIP_TOP)
    return SHOULDER * (TIP_BOTTOM - y) / (TIP_BOTTOM - 23.0)


# The ridge is a ray of light: it enters at the top tip, bends at the girdle, crosses it on a
# slant, bends back, and leaves through the bottom tip (180-degree rotational symmetry).
RAY = [(20.0, TIP_TOP), (17.0, GIRDLE[0]), (23.0, GIRDLE[1]), (20.0, TIP_BOTTOM)]


def _clip(poly, p0, p1, keep_left: bool, offset: float):
    """Sutherland-Hodgman: keep the part of a convex polygon on one side of the line p0->p1,
    pulled back from the line by `offset` (half the facet gap)."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    sign = 1.0 if keep_left else -1.0

    def side(pt):
        # Positive on the kept side; the line runs downward, so "left" is +x of the cross product.
        return sign * ((pt[0] - x0) * dy - (pt[1] - y0) * dx) / length * -1 - offset

    out = []
    n = len(poly)
    for i in range(n):
        cur, nxt = poly[i], poly[(i + 1) % n]
        sc, sn = side(cur), side(nxt)
        if sc >= 0:
            out.append(cur)
        if (sc >= 0) != (sn >= 0):
            t = sc / (sc - sn)
            out.append((cur[0] + (nxt[0] - cur[0]) * t, cur[1] + (nxt[1] - cur[1]) * t))
    return out


def _band_outline(y0: float, y1: float):
    hw0, hw1 = _half_width(y0), _half_width(y1)
    pts = []
    pts.append((CX - hw0, y0)) if hw0 > 0.01 else None
    pts.append((CX + hw0, y0)) if hw0 > 0.01 else pts.append((CX, y0))
    pts += [(CX + hw1, y1)] + ([(CX - hw1, y1)] if hw1 > 0.01 else [(CX, y1)])
    if hw0 <= 0.01:
        pts = [(CX, y0), (CX + hw1, y1), (CX - hw1, y1)]
    if hw1 <= 0.01:
        pts = [(CX - hw0, y0), (CX + hw0, y0), (CX, y1)]
    return pts


def _girdle_outline():
    import math

    top, bottom, hw = GIRDLE
    r = (bottom - top) / 2
    cy = (top + bottom) / 2
    pts = []
    for i in range(13):  # right cap, top to bottom
        a = -math.pi / 2 + math.pi * i / 12
        pts.append((CX + hw - r + r * math.cos(a), cy + r * math.sin(a)))
    for i in range(13):  # left cap, bottom to top
        a = math.pi / 2 + math.pi * i / 12
        pts.append((CX - hw + r + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _split(poly, seg):
    g = GAP / 2
    left = _clip(poly, seg[0], seg[1], True, g)
    right = _clip(poly, seg[0], seg[1], False, g)
    tidy = lambda pts: [(_r(x), _r(y)) for x, y in pts]
    return tidy(left), tidy(right)


def facets() -> list[tuple[str, list[tuple[float, float]]]]:
    """(role, polygon) pairs in a 40x40 box, top to bottom, left facet first."""
    roles = [("sky", "sky-2"), ("rose", "rose-2"), ("rose-2", "rose"), ("sky-2", "sky")]
    crown, girdle, pavilion = (RAY[0], RAY[1]), (RAY[1], RAY[2]), (RAY[2], RAY[3])
    out = []
    for i, (y0, y1) in enumerate(BANDS):
        if i == 2:
            gl, gr = _split(_girdle_outline(), girdle)
            out += [("paper", gl), ("gold", gr)]
        left, right = _split(_band_outline(y0, y1), crown if i < 2 else pavilion)
        out += [(roles[i][0], left), (roles[i][1], right)]
    return out


def _pts(poly) -> str:
    return " ".join(f"{x:g},{y:g}" for x, y in poly)


def header_svg() -> str:
    shapes = "".join(f'<polygon points="{_pts(p)}" class="mark-{role}"/>' for role, p in facets())
    return (
        "{# Generated by scripts/make_mark.py. Edit that script, not this file. #}\n"
        '<svg class="brand-mark" aria-hidden="true" focusable="false" viewBox="0 0 40 40" width="40" height="40">'
        '<rect x="0" y="0" width="40" height="40" rx="2" class="mark-bg"/>'
        f"{shapes}</svg>\n"
    )


def icon_svg() -> str:
    shapes = "".join(f'<polygon points="{_pts(p)}" fill="{COLORS[role]}"/>' for role, p in facets())
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
        "<!-- CivicDesk faceted desk mark. Generated by scripts/make_mark.py. -->"
        f'<rect x="0" y="0" width="40" height="40" rx="2" fill="{COLORS["bg"]}"/>'
        f"{shapes}</svg>\n"
    )


# ---- Pure-Python antialiased rasterizer (the build and CI have no imaging library) ----------

def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + r for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw, level=9)) + _png_chunk(b"IEND", b""))


def _span(poly, y: float):
    """Left and right x where a horizontal line crosses a convex polygon (or None)."""
    xs = []
    n = len(poly)
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        if y0 == y1:
            continue
        if min(y0, y1) <= y < max(y0, y1):
            xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    if len(xs) < 2:
        return None
    return min(xs), max(xs)


def _draw_shapes(canvas: list[list[list[float]]], shapes, sub: int = 4) -> None:
    """Composite (rgb, polygon-in-pixels) shapes over a float canvas with 4x vertical and exact
    horizontal coverage. Shapes must not overlap each other."""
    height, width = len(canvas), len(canvas[0])
    for rgb, poly in shapes:
        ys = [p[1] for p in poly]
        y_lo, y_hi = max(0, int(min(ys))), min(height - 1, int(max(ys)) + 1)
        for py in range(y_lo, y_hi + 1):
            cov = {}
            for s in range(sub):
                span = _span(poly, py + (s + 0.5) / sub)
                if span is None:
                    continue
                xl, xr = max(0.0, span[0]), min(float(width), span[1])
                for px in range(int(xl), min(width, int(xr) + 1)):
                    c = min(xr, px + 1) - max(xl, px)
                    if c > 0:
                        cov[px] = cov.get(px, 0.0) + c / sub
            row = canvas[py]
            for px, a in cov.items():
                a = min(1.0, a)
                pix = row[px]
                for k in range(3):
                    pix[k] = pix[k] * (1 - a) + rgb[k] * a


def _rounded_rect(x0: float, y0: float, x1: float, y1: float, r: float, steps: int = 6):
    import math

    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _mark_shapes(ox: float, oy: float, scale: float, tile: bool = True):
    shapes = []
    if tile:
        shapes.append((_rgb(COLORS["bg"]), _rounded_rect(ox, oy, ox + 40 * scale, oy + 40 * scale, 2 * scale)))
    for role, poly in facets():
        shapes.append((_rgb(COLORS[role]), [(ox + x * scale, oy + y * scale) for x, y in poly]))
    return shapes


def _flatten(canvas) -> list[bytes]:
    return [bytes(int(max(0, min(255, round(v)))) for pix in row for v in pix) for row in canvas]


def og_card_png(width: int = 1200, height: int = 630) -> bytes:
    """Zero-text 1200x630 social card: the faceted mark on press ink over a CMY press rule."""
    bg = _rgb(COLORS["bg"])
    canvas = [[list(bg) for _ in range(width)] for _ in range(height)]
    panel = (45, 43, 43)
    scale = 8.0
    ox, oy = (width - 40 * scale) / 2, (height - 40 * scale) / 2 - 10
    shapes = [(panel, _rounded_rect(ox, oy, ox + 320, oy + 320, 16))]
    shapes += _mark_shapes(ox, oy, scale, tile=False)
    _draw_shapes(canvas, shapes)
    for y0, y1, role in ((height - 22, height - 16, "gold"), (height - 16, height - 8, "rose"), (height - 8, height, "sky")):
        rgb = list(_rgb(COLORS[role]))
        for y in range(y0, y1):
            canvas[y] = [list(rgb) for _ in range(width)]
    return _png(width, height, _flatten(canvas))


def apple_touch_png(size: int = 180) -> bytes:
    """Zero-text 180x180 opaque touch icon: the faceted mark filling a press-ink square."""
    bg = _rgb(COLORS["bg"])
    canvas = [[list(bg) for _ in range(size)] for _ in range(size)]
    scale = size / 40.0
    _draw_shapes(canvas, _mark_shapes(0, 0, scale, tile=False))
    return _png(size, size, _flatten(canvas))


def _load_brand_config(root: Path) -> dict:
    site_path = root / "data" / "site.yaml"
    if not site_path.is_file():
        return {}
    data = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    brand = data.get("brand")
    return dict(brand) if isinstance(brand, dict) else {}


def run_cli(argv: list[str] | None = None, root: Path = ROOT) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    brand = _load_brand_config(root)
    custom_artwork = brand.get("custom_artwork") is True

    if custom_artwork and "--force-default" not in args:
        if "--check" in args:
            issues: list[str] = []
            checked = 0
            for key in ("logo", "logo_dark", "favicon_svg", "apple_touch_icon", "og_image"):
                rel = str(brand.get(key) or "").strip()
                if not rel:
                    continue
                checked += 1
                if rel.startswith("/") or "\\" in rel or ".." in Path(rel).parts:
                    issues.append(f"Invalid relative path for brand.{key}: {rel}")
                    continue
                target = root / "static" / rel
                if not target.is_file() or target.stat().st_size == 0:
                    issues.append(f"Missing or empty custom brand asset for brand.{key}: static/{rel}")
            if brand.get("mode") == "lockup_only" and not str(brand.get("logo") or "").strip():
                issues.append("brand.mode 'lockup_only' requires brand.logo to be set")
            if checked == 0:
                issues.append("brand.custom_artwork is true, but no brand asset paths are configured")
            for msg in issues:
                print(msg, file=sys.stderr)
            return 1 if issues else 0

        print(
            "Refusing to overwrite custom artwork while brand.custom_artwork is true in data/site.yaml "
            "(pass --force-default to regenerate default mark).",
            file=sys.stderr,
        )
        return 1

    text_targets = {
        root / "templates" / "brand-mark.svg": header_svg(),
        root / "static" / "favicon.svg": icon_svg(),
    }
    binary_targets = {
        root / "static" / "apple-touch-icon.png": apple_touch_png(),
        root / "static" / "og-card.png": og_card_png(),
    }

    stale: list[Path] = []
    for p, text in text_targets.items():
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            stale.append(p)
    for p, data in binary_targets.items():
        if not p.exists() or p.read_bytes() != data:
            stale.append(p)

    if "--check" in args:
        for p in stale:
            print(f"Out of date: {p.relative_to(root)}. Run python3 scripts/make_mark.py", file=sys.stderr)
        return 1 if stale else 0

    for p, text in text_targets.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"Wrote {p.relative_to(root)} ({len(text)} bytes)")
    for p, data in binary_targets.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        print(f"Wrote {p.relative_to(root)} ({len(data)} bytes)")
    return 0


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    sys.exit(main())

