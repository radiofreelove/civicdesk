#!/usr/bin/env python3
"""Render CivicDesk's editorial press prints into static/img/art/.

    python3 scripts/make_art.py                 # render every print
    python3 scripts/make_art.py oregon utah     # render only the named prints
    python3 scripts/make_art.py --list          # list print names, sizes, and subjects
    python3 scripts/make_art.py --preview DIR   # also write full-size PNG proofs to DIR

Every picture on the site is drawn here from code, the way a small print shop pulls a
four-plate job: process cyan, process magenta, process yellow, and press ink laid down as
separate plates on newsprint. Each plate is screened (a fine stochastic grain for tints, and a
rotated halftone where a gradient should read as printed), nudged slightly out of register,
and multiplied onto the paper so overlapping inks mix the way real ink does.

Nothing is traced, photographed, or produced by an image model. The prints carry no fees,
dates, counts, or rule numbers, so a policy change never makes a picture wrong.

The output is deterministic (fixed seeds). Dependencies live in requirements-art.txt and are
not needed to build or test the site, which serves only the committed .webp files.
"""
from __future__ import annotations

import argparse
import math
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is only needed when rendering prints from scratch
    np = None  # type: ignore[assignment]
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "static" / "img" / "art"
SERIF_WOFF2 = ROOT / "static" / "fonts" / "SourceSerif4-Latin.woff2"
RESPONSIVE_WIDTH = 480

# Broadsheet press anchors (docs/DESIGN.md). Yellow is the site's focus color.
PAPER = (np.array([243, 242, 242], dtype=np.float32) / 255.0) if np is not None else None
INKS = (
    {
        "y": np.array([237, 187, 0], dtype=np.float32) / 255.0,    # process yellow
        "c": np.array([0, 136, 176], dtype=np.float32) / 255.0,    # process cyan
        "m": np.array([214, 0, 108], dtype=np.float32) / 255.0,    # process magenta
        "k": np.array([32, 30, 29], dtype=np.float32) / 255.0,     # press ink
    }
    if np is not None
    else {}
)
PLATES = ("y", "c", "m", "k")
# Classic process screen angles keep the halftone rosette from turning into moire.
SCREEN_ANGLE = {"c": 15.0, "m": 75.0, "y": 0.0, "k": 45.0}
SS = 2  # supersampling: plates are drawn at SS x the output size, then downsampled

Pts = Sequence[tuple[float, float]]


# --------------------------------------------------------------------------------------
# Array helpers
# --------------------------------------------------------------------------------------

def _box(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    cs = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    n = a.shape[axis]
    hi = np.take(cs, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(cs, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur approximated by three box passes per axis."""
    a = np.asarray(a, dtype=np.float32)
    if sigma <= 0.3:
        return a
    r = max(1, int(round(math.sqrt(4 * sigma * sigma + 1) / 2)))
    for _ in range(3):
        a = _box(_box(a, r, 0), r, 1)
    return a


def shift(a: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Translate without wrap-around (edges are filled with zero)."""
    out = np.zeros_like(a)
    h, w = a.shape
    xs0, xs1 = max(0, -dx), min(w, w - dx)
    ys0, ys1 = max(0, -dy), min(h, h - dy)
    out[ys0 + dy:ys1 + dy, xs0 + dx:xs1 + dx] = a[ys0:ys1, xs0:xs1]
    return out


def smoothstep(e0: float, e1: float, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


@lru_cache(maxsize=1)
def serif_path() -> str:
    """Decompress the self-hosted Source Serif 4 WOFF2 into a temporary TTF for Pillow."""
    from fontTools.ttLib import TTFont  # requirements-art.txt

    font = TTFont(str(SERIF_WOFF2))
    font.flavor = None
    tmp = Path(tempfile.gettempdir()) / "civicdesk-art-serif.ttf"
    font.save(str(tmp))
    return str(tmp)


def serif(size: float, weight: int = 700) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(serif_path(), int(size))
    f.set_variation_by_axes([weight])
    return f


# --------------------------------------------------------------------------------------
# Canvas: four continuous-tone plates in output-pixel coordinates
# --------------------------------------------------------------------------------------

class Canvas:
    def __init__(self, w: int, h: int, seed: int):
        self.w, self.h, self.seed = w, h, seed
        self.W, self.H = w * SS, h * SS
        self.plates = {k: np.zeros((self.H, self.W), np.float32) for k in PLATES}
        self.am = {k: np.zeros((self.H, self.W), np.float32) for k in PLATES}
        yy, xx = np.mgrid[0:self.H, 0:self.W].astype(np.float32)
        self.X = (xx + 0.5) / SS          # output-pixel x of every sample
        self.Y = (yy + 0.5) / SS          # output-pixel y of every sample
        self.u = self.X / w               # 0..1 across
        self.v = self.Y / h               # 0..1 down

    def rng(self, salt: int) -> np.random.Generator:
        return np.random.default_rng(self.seed * 1_000_003 + salt)

    # ---- ink ------------------------------------------------------------------------------
    def lay(self, k: str, cov, mask=None, halftone: bool = False) -> None:
        """Add ink: union of coverage (a + b - ab), optionally through a mask."""
        cov = np.broadcast_to(np.asarray(cov, np.float32), (self.H, self.W))
        if mask is not None:
            cov = cov * mask
        p = self.plates[k]
        self.plates[k] = p + cov - p * cov
        if halftone:
            m = np.clip(mask, 0, 1) if mask is not None else (cov > 0.002).astype(np.float32)
            self.am[k] = np.maximum(self.am[k], m)

    def set(self, k: str, cov, mask, halftone: bool = False) -> None:
        """Replace ink under a mask (an opaque object painted over the scene)."""
        mask = np.clip(np.asarray(mask, np.float32), 0, 1)
        cov = np.broadcast_to(np.asarray(cov, np.float32), (self.H, self.W))
        self.plates[k] = self.plates[k] * (1 - mask) + cov * mask
        self.am[k] = self.am[k] * (1 - mask) + (mask if halftone else 0)

    def paint(self, mask, y=0.0, c=0.0, m=0.0, k=0.0, halftone: str = "") -> None:
        """Opaque object: set every plate under mask. halftone lists plates to screen coarse."""
        for plate, val in (("y", y), ("c", c), ("m", m), ("k", k)):
            self.set(plate, val, mask, halftone=plate in halftone)

    def knock(self, mask, plates: str = "ycmk") -> None:
        mask = np.clip(mask, 0, 1)
        for k in plates:
            self.plates[k] *= 1 - mask

    # ---- rasterizers (output-pixel coordinates in, float mask out) ------------------------
    def _new(self):
        img = Image.new("L", (self.W, self.H), 0)
        return img, ImageDraw.Draw(img)

    @staticmethod
    def _arr(img) -> np.ndarray:
        return np.asarray(img, np.float32) / 255.0

    def poly(self, pts: Pts) -> np.ndarray:
        img, d = self._new()
        d.polygon([(x * SS, y * SS) for x, y in pts], fill=255)
        return self._arr(img)

    def polys(self, many: Sequence[Pts]) -> np.ndarray:
        img, d = self._new()
        for pts in many:
            d.polygon([(x * SS, y * SS) for x, y in pts], fill=255)
        return self._arr(img)

    def circle(self, cx: float, cy: float, r: float, ry: float | None = None) -> np.ndarray:
        ry = r if ry is None else ry
        img, d = self._new()
        d.ellipse([(cx - r) * SS, (cy - ry) * SS, (cx + r) * SS, (cy + ry) * SS], fill=255)
        return self._arr(img)

    def rect(self, x0: float, y0: float, x1: float, y1: float, radius: float = 0) -> np.ndarray:
        img, d = self._new()
        box = [x0 * SS, y0 * SS, x1 * SS, y1 * SS]
        if radius:
            d.rounded_rectangle(box, radius=radius * SS, fill=255)
        else:
            d.rectangle(box, fill=255)
        return self._arr(img)

    def lines(self, paths: Sequence[Pts], width: float) -> np.ndarray:
        img, d = self._new()
        wpx = max(1, int(round(width * SS)))
        for pts in paths:
            d.line([(x * SS, y * SS) for x, y in pts], fill=255, width=wpx, joint="curve")
        return self._arr(img)

    def text(self, xy: tuple[float, float], s: str, size: float, weight: int = 700, anchor: str = "ls") -> np.ndarray:
        img, d = self._new()
        d.text((xy[0] * SS, xy[1] * SS), s, font=serif(size * SS, weight), fill=255, anchor=anchor)
        return self._arr(img)

    def below(self, profile: np.ndarray) -> np.ndarray:
        """Everything under a ridge profile (profile: output-px y per supersampled column)."""
        return np.clip((self.Y - profile[None, :]) * SS + 0.5, 0, 1).astype(np.float32)

    # ---- procedural fields -----------------------------------------------------------------
    def noise(self, scale: float, octaves: int = 4, salt: int = 0) -> np.ndarray:
        """Smooth value noise in 0..1, features roughly `scale` output pixels wide."""
        rng = self.rng(70_000 + salt)
        acc = np.zeros((self.H, self.W), np.float32)
        amp, total, s = 1.0, 0.0, scale * SS
        for _ in range(octaves):
            gh, gw = max(2, int(self.H / s) + 3), max(2, int(self.W / s) + 3)
            grid = Image.fromarray((rng.random((gh, gw)) * 255).astype(np.uint8), "L")
            acc += amp * np.asarray(grid.resize((self.W, self.H), Image.BICUBIC), np.float32) / 255.0
            total += amp
            amp *= 0.5
            s = max(2.0, s / 2)
        acc /= total
        lo, hi = np.percentile(acc, 1), np.percentile(acc, 99)
        return np.clip((acc - lo) / max(1e-6, hi - lo), 0, 1)

    def ridge(self, base: float, amp: float, salt: int, rough: float = 0.52,
              shape: Callable[[np.ndarray], np.ndarray] | None = None, freq: float = 1.4) -> np.ndarray:
        """1-D ridge line: output-px y per supersampled column, from summed seeded sines."""
        rng = self.rng(10_000 + salt)
        u = (np.arange(self.W, dtype=np.float32) + 0.5) / self.W
        y = np.full(self.W, base, np.float32)
        a = amp
        for _ in range(8):
            y += a * np.sin(2 * math.pi * freq * u + rng.random() * 2 * math.pi).astype(np.float32)
            freq *= 2.05
            a *= rough
        if shape is not None:
            y += shape(u).astype(np.float32)
        return y

    def treeline(self, profile: np.ndarray, size: float, salt: int, spacing: float = 0.55,
                 jitter: float = 0.5) -> np.ndarray:
        """Raise a ridge into a ragged edge of small conifer spires."""
        rng = self.rng(20_000 + salt)
        out = profile.copy()
        xpos = rng.random() * size * SS
        while xpos < self.W:
            cx = int(xpos)
            hgt = size * SS * (1 - jitter / 2 + rng.random() * jitter)
            half = hgt * 0.24
            lo, hi = max(0, int(cx - half)), min(self.W, int(cx + half) + 1)
            xs = np.arange(lo, hi)
            t = np.clip(1 - np.abs(xs - cx) / max(1.0, half), 0, 1)
            out[lo:hi] = np.minimum(out[lo:hi], profile[cx] - hgt * t ** 1.3 / SS)
            xpos += size * SS * spacing * (0.6 + rng.random() * 0.8)
        return out

    def shadow(self, mask: np.ndarray, dx: float, dy: float, soft: float) -> np.ndarray:
        return blur(shift(mask, int(round(dx * SS)), int(round(dy * SS))), soft * SS)

    # ---- output -----------------------------------------------------------------------------
    def render(self, grain: float = 0.24, halftone_px: float = 5.5, misregister: float = 1.0,
               wobble: float = 0.07) -> Image.Image:
        H, W = self.H, self.W
        rng = self.rng(99)
        out = np.ones((H, W, 3), np.float32) * PAPER
        fiber = self.noise(38, octaves=3, salt=900) - 0.5
        tooth = rng.random((H, W)).astype(np.float32) - 0.5
        out *= 1.0 + fiber[..., None] * 0.020 + tooth[..., None] * 0.012

        for i, k in enumerate(PLATES):
            cov = np.clip(self.plates[k], 0, 1)
            if cov.max() <= 0.002:
                continue
            dx = int(round((rng.random() * 2 - 1) * misregister * SS))
            dy = int(round((rng.random() * 2 - 1) * misregister * SS))
            moved = shift(cov, dx, dy)
            gap = shift(np.ones_like(cov), dx, dy) < 0.5
            cov = np.where(gap, cov, moved)
            am = np.clip(shift(self.am[k], dx, dy), 0, 1)

            # Stochastic grain (risograph tooth), rank-mapped so thresholds keep tone.
            g = blur(rng.random((H, W)).astype(np.float32), 0.8)
            order = g.ravel().argsort(kind="stable")
            ranks = np.empty(order.size, np.float32)
            ranks[order] = np.arange(order.size, dtype=np.float32)
            g = (ranks.reshape(H, W) + 0.5) / order.size
            fm = cov + ((cov > g).astype(np.float32) - cov) * grain

            # Rotated Euclidean-dot halftone.
            th = math.radians(SCREEN_ANGLE[k])
            p = halftone_px * SS
            uu = (self.X * SS * math.cos(th) + self.Y * SS * math.sin(th)) * (2 * math.pi / p)
            vv = (-self.X * SS * math.sin(th) + self.Y * SS * math.cos(th)) * (2 * math.pi / p)
            spot = (np.cos(uu) + np.cos(vv)) * 0.5
            amd = np.clip((spot - (1 - 2 * cov)) * (p * 0.16) + 0.5, 0, 1)

            screened = fm * (1 - am) + amd * am
            screened = np.where(cov > 0.985, 1.0, screened)
            dens = 1.0 - wobble + wobble * self.noise(240, octaves=2, salt=50 + i)
            a = screened * dens
            out *= 1.0 - a[..., None] * (1.0 - INKS[k])

        img = Image.fromarray(np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8), "RGB")
        return img.resize((self.w, self.h), Image.LANCZOS)


# --------------------------------------------------------------------------------------
# Reusable motifs
# --------------------------------------------------------------------------------------

def fir(cx: float, base: float, h: float, w: float, rng: np.random.Generator, tiers: int = 9) -> list[tuple[float, float]]:
    """Silhouette of a fir: stacked, slightly drooping branch tiers on a narrow trunk."""
    left, right = [], []
    for i in range(tiers + 1):
        t = i / tiers                      # 0 at base, 1 at tip
        y = base - h * (0.08 + 0.92 * t)
        width = w * (1 - t) ** 0.95 * (0.85 + 0.3 * rng.random())
        droop = h / tiers * 0.38
        left += [(cx - width * 0.62, y - droop * 0.2), (cx - width, y + droop)]
        right += [(cx + width, y + droop * (0.8 + 0.4 * rng.random())), (cx + width * 0.62, y - droop * 0.2)]
    tip = (cx + (rng.random() - 0.5) * w * 0.05, base - h * 1.02)
    trunk_w = max(1.0, w * 0.06)
    pts = [(cx - trunk_w, base), (cx - trunk_w, base - h * 0.06)] + left
    pts += [tip] + right[::-1] + [(cx + trunk_w, base - h * 0.06), (cx + trunk_w, base)]
    return pts


def local(cx: float, cy: float, deg: float):
    """Map points from an object's own frame (origin at its centre) into the canvas."""
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)

    def f(x: float, y: float) -> tuple[float, float]:
        return (cx + x * ca - y * sa, cy + x * sa + y * ca)

    return f


def guilloche(f, w: float, h: float, waves: int, lines: int, amp: float, phase: float = 0.0,
              steps: int = 700) -> list[list[tuple[float, float]]]:
    """Interlaced sine bands across a w x h panel (security-print background)."""
    paths = []
    for j in range(lines):
        off = (j / max(1, lines - 1) - 0.5) * h * 0.9
        ph = phase + j * 2 * math.pi / lines
        pts = []
        for s in range(steps + 1):
            x = -w / 2 + w * s / steps
            y = off + amp * math.sin(2 * math.pi * waves * s / steps + ph) * math.cos(math.pi * s / steps * 1.5 + ph * 0.3)
            pts.append(f(x, y))
        paths.append(pts)
    return paths


def rosette(cx: float, cy: float, R: float, r: float, d: float, turns: int, rot: float = 0.0,
            steps: int = 2400) -> list[tuple[float, float]]:
    """Hypotrochoid: the curve a guilloche lathe cuts for seals and rosettes."""
    pts = []
    ca, sa = math.cos(rot), math.sin(rot)
    for s in range(steps + 1):
        t = 2 * math.pi * turns * s / steps
        x = (R - r) * math.cos(t) + d * math.cos((R - r) / r * t)
        y = (R - r) * math.sin(t) - d * math.sin((R - r) / r * t)
        pts.append((cx + x * ca - y * sa, cy + x * sa + y * ca))
    return pts


def sky(c: Canvas, glow: float = 0.42, wash: float = 0.10, top_cyan: float = 0.0, horizon: float = 0.6) -> None:
    t = smoothstep(0.02, horizon, c.v)
    c.lay("y", glow * t ** 1.5)
    if wash:
        c.lay("m", wash * (1 - t) + wash * 0.3, halftone=True)
    if top_cyan:
        c.lay("c", top_cyan * (1 - t) ** 1.3, halftone=True)


def text_lines(c: Canvas, f, x0: float, y0: float, widths: Sequence[float], gap: float, thick: float) -> np.ndarray:
    """Greeked text: short bars standing in for type, no words."""
    polys = []
    for i, wdt in enumerate(widths):
        y = y0 + i * gap
        polys.append([f(x0, y), f(x0 + wdt, y), f(x0 + wdt, y + thick), f(x0, y + thick)])
    return c.polys(polys)


def ident(x: float, y: float) -> tuple[float, float]:
    return (x, y)


# --------------------------------------------------------------------------------------
# Place prints
# --------------------------------------------------------------------------------------

def draw_oregon(c: Canvas) -> None:
    """A snow volcano over foothill forest and a wide river at first light."""
    w, h = c.w, c.h
    sky(c, glow=0.42, wash=0.08)
    sun = c.circle(0.215 * w, 0.565 * h, 0.055 * w)
    c.lay("y", 0.80, sun)
    c.lay("m", 0.10, sun)

    peak = 0.60

    def cone(u):
        left = np.clip(1 - (peak - u) / 0.25, 0, 1)
        right = np.clip(1 - (u - peak) / 0.34, 0, 1)
        prof = np.where(u < peak, left ** 1.25, right ** 1.35)
        horn = 0.035 * np.exp(-((u - peak) / 0.012) ** 2)
        bump = np.where(u > peak, 0.022 * np.exp(-((u - peak - 0.085) / 0.028) ** 2), 0)
        return -(prof * 0.37 + horn + bump) * h

    vm = c.below(c.ridge(0.615 * h, 0.0025 * h, 1, rough=0.6, shape=cone))
    c.paint(vm, c=0.10)
    ridge_x = peak + (c.v - 0.21) * 0.18
    rib_n = c.noise(14, 3, salt=4)
    east = smoothstep(ridge_x - 0.004, ridge_x + 0.004, c.u + (rib_n - 0.5) * 0.02)
    c.lay("c", 0.42 * east, vm)
    c.lay("m", 0.10 * east, vm)
    ang = np.arctan2(c.u - peak, (c.v - 0.215) + 1e-4)
    ribs = (np.sin(ang * 46 + rib_n * 3.0) > 0.72) * smoothstep(0.24, 0.30, c.v)
    c.knock(ribs * east * vm * 0.55, "c")
    gul = (np.sin(ang * 38 + rib_n * 2.0) > 0.86) * smoothstep(0.26, 0.34, c.v)
    c.lay("c", 0.22 * gul * (1 - east), vm)
    snow_n = c.noise(9, 3, salt=5)
    low = smoothstep(0.455, 0.475, c.v + (snow_n - 0.5) * 0.07)
    c.lay("c", 0.45 * low, vm)
    c.lay("m", 0.30 * low, vm)

    for base, amp, salt, tree, cv, mv in (
        (0.600, 0.012, 11, 7, 0.22, 0.10),
        (0.645, 0.016, 12, 9, 0.46, 0.30),
        (0.700, 0.020, 13, 12, 0.80, 0.18),
    ):
        m = c.below(c.treeline(c.ridge(base * h, amp * h, salt), tree, salt))
        c.paint(m, c=cv, m=mv)

    top, bot = 0.752 * h, 0.872 * h
    river = c.rect(0, top, w, bot)
    d = smoothstep(top, bot, c.Y)
    c.paint(river, y=0.34 * (1 - d) ** 1.5, c=0.04 + 0.30 * d ** 1.5, m=0.03 + 0.10 * d)
    wob = c.noise(18, 2, salt=21)
    phase = (c.Y - top) * 0.75 * (1 + d) + wob * 7
    c.lay("c", 0.35 * (np.sin(phase) > 0.80), river)
    path = np.exp(-((c.u - 0.215) / (0.03 + 0.05 * d)) ** 2) * river
    glint = (np.sin(phase * 1.15) > 0.45) * path
    c.knock(glint, "cm")
    c.lay("y", 0.85 * glint)

    rng = c.rng(31)
    near = c.treeline(c.ridge(0.905 * h, 0.010 * h, 31), 22, 31, spacing=0.42, jitter=1.0)
    nm = c.below(near)
    trees = c.polys([fir(0.885 * w, 0.93 * h, 0.33 * h, 0.040 * w, rng, 11),
                     fir(0.945 * w, 0.95 * h, 0.42 * h, 0.048 * w, rng, 12),
                     fir(0.055 * w, 0.93 * h, 0.24 * h, 0.034 * w, rng, 9)])
    c.paint(np.maximum(nm, trees), c=0.55, m=0.30, k=0.90)


def draw_washington(c: Canvas) -> None:
    """A broad glaciated volcano over a sound, mist between the ridges, a ferry crossing."""
    w, h = c.w, c.h
    t = smoothstep(0.0, 0.62, c.v)
    c.lay("c", 0.30 * (1 - t) ** 1.2, halftone=True)
    c.lay("m", 0.10 * (1 - t) + 0.02, halftone=True)
    c.lay("y", 0.16 * t ** 2)

    def massif(u):
        body = np.clip(1 - np.abs(u - 0.52) / 0.40, 0, 1) ** 1.6 * 0.30
        crest = 0.045 * np.exp(-((u - 0.50) / 0.07) ** 2) + 0.03 * np.exp(-((u - 0.40) / 0.035) ** 2)
        return -(body + crest) * h

    mm = c.below(c.ridge(0.60 * h, 0.002 * h, 2, rough=0.6, shape=massif))
    c.paint(mm, c=0.08)
    n = c.noise(10, 3, salt=6)
    ang = np.arctan2(c.u - 0.50, (c.v - 0.24) + 1e-4)
    glaciers = (np.sin(ang * 30 + n * 4) > 0.55) * smoothstep(0.30, 0.40, c.v)
    shade = smoothstep(0.50, 0.62, c.u + (n - 0.5) * 0.08)
    c.lay("c", 0.36 * shade + 0.18 * glaciers * (1 - shade), mm)
    c.lay("m", 0.08 * shade, mm)
    rock = smoothstep(0.50, 0.54, c.v + (n - 0.5) * 0.06)
    c.lay("c", 0.32 * rock, mm)
    c.lay("m", 0.22 * rock, mm)

    for base, amp, salt, tree, cv, mv in (
        (0.585, 0.014, 21, 8, 0.30, 0.12),
        (0.630, 0.018, 22, 10, 0.55, 0.20),
        (0.675, 0.020, 23, 12, 0.85, 0.30),
    ):
        prof = c.treeline(c.ridge(base * h, amp * h, salt), tree, salt)
        m = c.below(prof)
        c.paint(m, c=cv, m=mv)
        foot = c.Y - (base + 0.045) * h
        mist = smoothstep(0.035 * h, 0.0, foot) * smoothstep(-0.04 * h, 0, foot)
        c.knock(np.clip(mist * 0.85, 0, 1) * m, "cm")

    top = 0.735 * h
    water = c.rect(0, top, w, h)
    d = smoothstep(top, h, c.Y)
    c.paint(water, c=0.18 + 0.42 * d, m=0.05 + 0.12 * d, y=0.06 * (1 - d))
    wob = c.noise(20, 2, salt=31)
    c.knock((np.sin((c.Y - top) * 0.9 * (1 + 2 * d) + wob * 6) > 0.86) * water * 0.8, "cm")

    # Ferry: white hull, a band of green, two decks of windows, a wake.
    fx, fy, fl = 0.60 * w, 0.800 * h, 0.17 * w
    wake = c.poly([(fx - fl * 0.48, fy + 4), (fx - fl * 1.6, fy + 10), (fx - fl * 1.6, fy + 16), (fx - fl * 0.48, fy + 8)])
    c.knock(wake * 0.9, "cm")
    hull = c.poly([(fx - fl / 2, fy - 12), (fx + fl / 2, fy - 12), (fx + fl * 0.46, fy + 6), (fx - fl * 0.46, fy + 6)])
    c.paint(hull, c=0.04)
    band = c.rect(fx - fl * 0.47, fy - 4, fx + fl * 0.47, fy + 1)
    c.paint(band, y=0.85, c=0.85)
    deck1 = c.rect(fx - fl * 0.38, fy - 26, fx + fl * 0.38, fy - 12)
    deck2 = c.rect(fx - fl * 0.24, fy - 37, fx + fl * 0.24, fy - 26)
    c.paint(np.maximum(deck1, deck2), c=0.06)
    wins = c.polys([[(fx + x, fy - 22), (fx + x + 6, fy - 22), (fx + x + 6, fy - 17), (fx + x, fy - 17)]
                    for x in np.arange(-fl * 0.34, fl * 0.34, 11)])
    c.paint(wins * deck1, c=0.65, m=0.25, k=0.35)
    stack = c.rect(fx - 5, fy - 47, fx + 5, fy - 37)
    c.paint(stack, k=0.85, c=0.3)

    rng = c.rng(41)
    point = c.poly([(0, 0.78 * h), (0.22 * w, 0.80 * h), (0.34 * w, 0.86 * h), (0.40 * w, h), (0, h)])
    firs = [fir(x * w, (0.80 + 0.10 * (x / 0.34)) * h, (0.20 + 0.18 * rng.random()) * h, 0.032 * w, rng, 10)
            for x in (0.02, 0.07, 0.115, 0.16, 0.20, 0.245, 0.29)]
    c.paint(np.maximum(point, c.polys(firs)), c=0.50, m=0.25, k=0.92)


def draw_utah(c: Canvas) -> None:
    """Red-rock arch on slickrock, far snowy peaks, a deep blue sky."""
    w, h = c.w, c.h
    t = smoothstep(0.0, 0.66, c.v)
    c.lay("c", 0.62 * (1 - t) ** 1.1 + 0.06, halftone=True)
    c.lay("m", 0.10 * (1 - t) ** 1.5)
    c.lay("y", 0.22 * t ** 2.5)

    far = c.ridge(0.60 * h, 0.03 * h, 3, rough=0.55,
                  shape=lambda u: -0.05 * h * np.exp(-((u - 0.72) / 0.12) ** 2) - 0.03 * h * np.exp(-((u - 0.30) / 0.1) ** 2))
    fm = c.below(far)
    c.paint(fm, c=0.28, m=0.14)
    sn = c.noise(8, 3, salt=7)
    snow = (c.Y < far[None, :] + (0.035 + 0.02 * sn) * h) * fm
    c.knock(snow * 0.85, "cm")

    def mesa(u):
        return np.where((u > 0.05) & (u < 0.34), -0.07 * h, 0) + np.where((u > 0.78) & (u < 0.98), -0.05 * h, 0)

    mes = c.ridge(0.665 * h, 0.004 * h, 4, rough=0.5, shape=mesa)
    mm = c.below(blur(np.repeat(mes[None, :], 5, 0), 2)[2])
    sn2 = c.noise(30, 2, salt=8)
    strata = 0.5 + 0.5 * np.sin(c.Y * 0.55 + sn2 * 3)
    c.paint(mm, y=0.62, m=0.40 + 0.10 * strata, c=0.16)
    flutes = (np.sin(c.X * 0.45 + sn2 * 8) > 0.55) * smoothstep(0.58 * h, 0.66 * h, c.Y)
    c.lay("c", 0.18 * flutes * mm)
    c.lay("m", 0.12 * flutes * mm)

    ground = c.ridge(0.78 * h, 0.012 * h, 5, rough=0.5)
    gm = c.below(ground)
    gn = c.noise(60, 3, salt=9)
    c.paint(gm, y=0.62, m=0.30 + 0.14 * gn, c=0.03)
    cracks = c.lines([[(x * w, 0.84 * h + 18 * math.sin(x * 9)), ((x + 0.07) * w, 0.98 * h)] for x in (0.08, 0.30, 0.52, 0.70, 0.88)], 1.6)
    c.lay("m", 0.6 * cracks, gm)
    c.lay("c", 0.3 * cracks, gm)

    ax, base, span, rise = 0.60 * w, 0.80 * h, 0.36 * w, 0.50 * h
    arng = c.rng(52)
    wob = [arng.random() - 0.5 for _ in range(12)]

    def jag(a: float) -> float:
        return sum(wob[i] * math.sin((i + 2) * a + wob[-i - 1] * 6) for i in range(6)) * 5.0

    outer, inner = [], []
    for s in range(161):
        a = math.pi * s / 160
        # Left leg massive, right leg slimmer, span thinnest near the top.
        leg = (0.20 if a > math.pi / 2 else 0.13) * abs(math.cos(a)) ** 1.5
        thick = 0.085 + leg
        ox = ax - math.cos(a) * span / 2 * (1.0 + 0.10 * (1 - math.sin(a)))
        oy = base - math.sin(a) ** 0.85 * rise
        outer.append((ox + jag(a) * 0.6, oy + jag(a + 1)))
        ix = ax - math.cos(a) * (span / 2 - thick * span)
        iy = base - math.sin(a) ** 0.85 * (rise - thick * span * 0.95)
        inner.append((ix + jag(a + 2) * 0.4, iy + jag(a + 3) * 0.6))
    am = c.poly(outer + inner[::-1])
    lit = smoothstep(ax + 0.02 * w, ax - 0.06 * w, c.X)
    sh_n = c.noise(16, 3, salt=10)
    c.paint(am, y=0.80, m=0.52 + 0.10 * sh_n, c=0.04)
    c.lay("c", 0.42 * (1 - lit) * am)
    c.lay("m", 0.16 * (1 - lit) * am)
    c.lay("m", 0.16 * (np.sin(c.Y * 0.32 + sh_n * 3.5) > 0.55) * am)
    c.lay("y", 0.15 * (np.sin(c.Y * 0.32 + sh_n * 3.5) < -0.7) * am)
    sh = c.poly([(ax - span * 0.60, base + 4), (ax + span * 0.85, base + 34), (ax + span * 0.95, base + 60), (ax - span * 0.45, base + 26)])
    c.lay("c", 0.35 * sh * gm)

    rng = c.rng(51)
    for jx, jy, js in ((0.14, 0.86, 1.0), (0.20, 0.88, 0.7), (0.90, 0.83, 0.8)):
        blobs = [c.circle(jx * w + (rng.random() - 0.5) * 30 * js, jy * h - rng.random() * 26 * js, (8 + rng.random() * 9) * js)
                 for _ in range(9)]
        c.paint(np.clip(sum(blobs), 0, 1), c=0.55, y=0.35, k=0.55)


def draw_front(c: Canvas) -> None:
    """From the coast to canyon country: sea stacks, a volcano, and mesas under one sky."""
    w, h = c.w, c.h
    sky(c, glow=0.48, wash=0.10, horizon=0.55)
    sun = c.circle(0.68 * w, 0.40 * h, 0.05 * w)
    c.lay("y", 0.75, sun)

    def cone(u):
        return -np.clip(1 - np.abs(u - 0.38) / 0.20, 0, 1) ** 1.3 * 0.25 * h

    vm = c.below(c.ridge(0.50 * h, 0.002 * h, 61, shape=cone))
    c.paint(vm, c=0.10)
    east = smoothstep(0.38, 0.39, c.u + (c.noise(12, 3, salt=62) - 0.5) * 0.02)
    c.lay("c", 0.35 * east, vm)
    lowv = smoothstep(0.37, 0.40, c.v + (c.noise(8, 3, salt=63) - 0.5) * 0.05)
    c.lay("c", 0.40 * lowv, vm)
    c.lay("m", 0.25 * lowv, vm)

    for x0, x1, top in ((0.66, 0.78, 0.45), (0.82, 0.98, 0.42)):
        m = c.poly([(x0 * w - 26, 0.53 * h), (x0 * w - 4, top * h + 10), (x0 * w, top * h), (x1 * w, top * h), (x1 * w + 6, top * h + 12), (x1 * w + 30, 0.53 * h)])
        c.paint(m, y=0.58, m=0.40, c=0.10)
        c.lay("c", 0.22 * m * smoothstep(x1 * w - 30, x1 * w + 10, c.X))
        c.lay("m", 0.12 * m * (np.sin(c.Y * 0.8) > 0.5))

    across = smoothstep(0.35, 0.75, c.u)
    cols = np.arange(c.W) / c.W
    r1 = c.ridge(0.53 * h, 0.012 * h, 64)
    r1 = np.where(cols < 0.6, c.treeline(r1, 7, 64), r1)
    c.paint(c.below(r1), c=0.55 * (1 - across) + 0.15 * across, m=0.22 * (1 - across) + 0.20 * across, y=0.40 * across)
    r2 = c.ridge(0.60 * h, 0.016 * h, 65)
    r2 = np.where(cols < 0.55, c.treeline(r2, 10, 65), r2)
    c.paint(c.below(r2), c=0.80 * (1 - across) + 0.12 * across, m=0.30 * (1 - across) + 0.38 * across, y=0.58 * across)

    sea = c.poly([(0, 0.66 * h), (0.36 * w, 0.66 * h), (0.30 * w, 0.74 * h), (0.24 * w, 0.86 * h), (0.20 * w, h), (0, h)])
    d = smoothstep(0.66 * h, h, c.Y)
    c.paint(sea, c=0.20 + 0.40 * d, m=0.06 + 0.10 * d, y=0.10 * (1 - d))
    c.knock((np.sin(c.Y * 0.9 + c.noise(16, 2, salt=66) * 6) > 0.85) * sea * 0.8, "cm")
    stacks = c.polys([
        [(0.07 * w, 0.80 * h), (0.09 * w, 0.66 * h), (0.12 * w, 0.62 * h), (0.15 * w, 0.66 * h), (0.17 * w, 0.80 * h)],
        [(0.22 * w, 0.76 * h), (0.235 * w, 0.70 * h), (0.25 * w, 0.69 * h), (0.265 * w, 0.76 * h)],
    ])
    c.paint(stacks, c=0.55, m=0.35, k=0.55)
    floor = np.clip(c.poly([(0.36 * w, 0.66 * h), (w, 0.62 * h), (w, h), (0.20 * w, h), (0.24 * w, 0.86 * h), (0.30 * w, 0.74 * h)]) - sea, 0, 1)
    fn = c.noise(40, 3, salt=67)
    acr = smoothstep(0.25, 0.9, c.u)
    c.paint(floor, y=0.34 + 0.18 * acr, m=0.10 + 0.16 * acr + 0.06 * fn, c=0.26 * (1 - acr) + 0.06)
    scrub = (c.noise(5, 2, salt=69) > 0.78) * floor
    c.lay("c", 0.35 * scrub)
    c.lay("k", 0.12 * scrub)
    road_pts = [(0.40 * w, h), (0.50 * w, 0.84 * h), (0.64 * w, 0.73 * h), (0.80 * w, 0.665 * h), (0.97 * w, 0.635 * h)]
    road = c.lines([road_pts], 9)
    c.paint(road * floor, k=0.72, c=0.22)
    dash = c.lines([road_pts[:-1]], 1.4)
    c.paint(dash * (np.sin(c.X * 0.30) > 0.25) * floor, y=0.95)

    rng = c.rng(68)
    firs = [fir(x * w, (0.69 + 0.02 * rng.random()) * h, (0.10 + 0.08 * rng.random()) * h, 0.022 * w, rng, 8)
            for x in np.linspace(0.30, 0.45, 6)]
    c.paint(c.polys(firs), c=0.55, m=0.30, k=0.85)


def draw_fog(c: Canvas) -> None:
    """Sea stacks fading into coastal fog; the horizon is gone."""
    w, h = c.w, c.h
    c.lay("c", 0.05 + 0.04 * c.noise(120, 2, salt=71))
    stacks = [(0.68, 0.46, 0.26, 0.05), (0.90, 0.20, 0.08, 0.35), (0.42, 0.16, 0.09, 0.62), (0.22, 0.10, 0.06, 0.85), (0.08, 0.07, 0.04, 0.95)]
    rng = c.rng(72)
    for sx, sh, sw, depth in sorted(stacks, key=lambda s: -s[3]):
        base = (0.70 - 0.05 * depth) * h
        pts = [(sx * w - sw * w / 2, base)]
        for i in range(10):
            f = i / 9
            pts.append((sx * w - sw * w / 2 + f * sw * w, base - sh * h * math.sin(math.pi * f) ** 0.5 * (0.9 + 0.2 * rng.random())))
        pts.append((sx * w + sw * w / 2, base))
        m = c.poly(pts)
        fade = smoothstep(base - sh * h, base, c.Y)
        a = (1 - depth) * 0.9 + 0.1
        c.paint(m, c=0.10 + 0.55 * a * (1 - 0.4 * fade), m=0.05 + 0.30 * a * (1 - 0.4 * fade), k=0.35 * a ** 2)
        c.knock(smoothstep(base - 0.08 * h, base, c.Y) * m * 0.7, "cmk")
    sand = c.rect(0, 0.80 * h, w, h)
    d = smoothstep(0.80 * h, h, c.Y)
    c.paint(sand, y=0.18 + 0.12 * d, m=0.06 + 0.06 * d, c=0.06 + 0.10 * d)
    edge = 0.80 * h + 10 * np.sin(c.X * 0.012) + 6 * c.noise(40, 2, salt=73)
    foam = smoothstep(4, 0, np.abs(c.Y - edge - 10)) * sand
    c.knock(foam * 0.9, "ycmk")
    wet = smoothstep(0, 30, c.Y - edge) * smoothstep(60, 30, c.Y - edge) * sand
    c.lay("c", 0.18 * wet)
    c.lay("k", 0.06 * wet)
    for fy in (0.52, 0.62):
        bank = smoothstep(0.06 * h, 0, np.abs(c.Y - fy * h + 10 * np.sin(c.X * 0.01 + fy * 9)))
        c.knock(bank * 0.75, "cmk")
    gulls = []
    for gx, gy, gs in ((0.30, 0.26, 7), (0.34, 0.22, 5), (0.55, 0.30, 6)):
        x, y = gx * w, gy * h
        gulls.append([(x - gs, y - gs * 0.4), (x - gs * 0.4, y - gs * 0.6), (x, y)])
        gulls.append([(x, y), (x + gs * 0.4, y - gs * 0.6), (x + gs, y - gs * 0.4)])
    c.paint(c.lines(gulls, 1.4), k=0.65)


# --------------------------------------------------------------------------------------
# Topic prints (still lifes and civic places)
# --------------------------------------------------------------------------------------

def table(c: Canvas, y: float = 0.20, m: float = 0.08, grain_scale: float = 1.0) -> None:
    """Warm tabletop with long wood grain."""
    rng = c.rng(81)
    rows = np.cumsum(rng.random(c.H) * 2.0).astype(np.float32)
    streak = np.asarray(Image.fromarray(((np.sin(rows * 0.21 / grain_scale) * 0.5 + 0.5) * 255).astype(np.uint8)[:, None])
                        .resize((1, c.H), Image.BILINEAR), np.float32)[:, 0] / 255.0
    drift = c.noise(160 * grain_scale, 2, salt=81)
    grain = np.clip(streak[:, None] * 0.7 + drift * 0.3, 0, 1)
    c.lay("y", y + 0.05 * grain)
    c.lay("m", m + 0.035 * grain)
    c.lay("c", 0.015 + 0.02 * grain)


def drop(c: Canvas, mask: np.ndarray, dx: float = 7, dy: float = 10, soft: float = 7, a: float = 0.55) -> None:
    sh = c.shadow(mask, dx, dy, soft) * (1 - mask)
    c.lay("c", 0.45 * a * sh)
    c.lay("m", 0.30 * a * sh)
    c.lay("k", 0.12 * a * sh)


def quad(c: Canvas, f, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    return c.poly([f(x0, y0), f(x1, y0), f(x1, y1), f(x0, y1)])


def draw_mail(c: Canvas) -> None:
    """Official mail on a kitchen table: window envelopes and an opened notice."""
    w, h = c.w, c.h
    table(c)
    rng = c.rng(83)

    def envelope(cx, cy, ew, eh, deg, window=True, back=False):
        f = local(cx, cy, deg)
        body = quad(c, f, -ew / 2, -eh / 2, ew / 2, eh / 2)
        drop(c, body)
        c.paint(body, y=0.04, c=0.02)
        if back:
            flap = c.lines([[f(-ew / 2, -eh / 2), f(0, eh * 0.08), f(ew / 2, -eh / 2)],
                            [f(-ew / 2, eh / 2), f(-ew * 0.08, -eh * 0.02)], [f(ew / 2, eh / 2), f(ew * 0.08, -eh * 0.02)]], 1.3)
            c.lay("c", 0.35 * flap)
            c.lay("m", 0.12 * flap)
        if window:
            win = quad(c, f, -ew * 0.40, -eh * 0.05, ew * 0.05, eh * 0.28)
            c.lay("c", 0.16 * win)
            c.lay("m", 0.04 * win)
            c.lay("k", 0.55 * text_lines(c, f, -ew * 0.36, 0, [ew * 0.30, ew * 0.24, ew * 0.27], eh * 0.075, eh * 0.035))
            c.lay("k", 0.45 * text_lines(c, f, -ew * 0.45, -eh * 0.40, [ew * 0.22, ew * 0.16], eh * 0.06, eh * 0.028))
            edge = quad(c, f, ew * 0.30, -eh * 0.42, ew * 0.45, -eh * 0.18) - quad(c, f, ew * 0.31, -eh * 0.40, ew * 0.44, -eh * 0.20)
            c.lay("k", 0.55 * np.clip(edge, 0, 1))
            c.lay("m", 0.55 * c.lines([[f(ew * 0.27, -eh * (0.38 - i * 0.05)), f(ew * 0.47, -eh * (0.38 - i * 0.05))] for i in range(4)], 1.1))

    envelope(0.30 * w, 0.30 * h, 0.44 * w, 0.22 * w, -9, window=False, back=True)
    envelope(0.68 * w, 0.25 * h, 0.42 * w, 0.21 * w, 7)

    f = local(0.52 * w, 0.66 * h, -3)
    sw, sh_ = 0.46 * w, 0.62 * h
    sheet = quad(c, f, -sw / 2, -sh_ / 2, sw / 2, sh_ / 2)
    drop(c, sheet, 9, 13, 9, 0.7)
    c.paint(sheet)
    for i, tone in ((1, 0.06), (2, 0.10)):
        band = quad(c, f, -sw / 2, -sh_ / 2 + i * sh_ / 3 - 10, sw / 2, -sh_ / 2 + i * sh_ / 3)
        c.lay("c", tone * band)
        c.lay("m", tone * 0.4 * band)
    c.lay("c", 0.9 * quad(c, f, -sw * 0.42, -sh_ * 0.44, sw * 0.42, -sh_ * 0.425))
    c.lay("k", 0.75 * text_lines(c, f, -sw * 0.42, -sh_ * 0.40, [sw * 0.26, sw * 0.18], sh_ * 0.035, sh_ * 0.016))
    c.lay("y", 0.40 * quad(c, f, -sw * 0.42, -sh_ * 0.27, sw * 0.42, -sh_ * 0.12))
    c.lay("k", 0.70 * text_lines(c, f, -sw * 0.38, -sh_ * 0.235, [sw * 0.52, sw * 0.40, sw * 0.46], sh_ * 0.035, sh_ * 0.016))
    widths = [sw * (0.70 + 0.14 * rng.random()) for _ in range(9)]
    widths[4] = sw * 0.42
    c.lay("k", 0.45 * text_lines(c, f, -sw * 0.42, -sh_ * 0.06, widths, sh_ * 0.042, sh_ * 0.014))
    g = local(0.86 * w, 0.78 * h, -58)
    pen = c.poly([g(-0.19 * w, -6), g(0.15 * w, -6), g(0.19 * w, 0), g(0.15 * w, 6), g(-0.19 * w, 6)])
    drop(c, pen, 5, 8, 4, 0.7)
    shade = 0.5 + 0.5 * np.sin((c.X * math.cos(math.radians(32)) + c.Y * math.sin(math.radians(32))) * 0.5)
    c.paint(pen, c=0.85, m=0.45, k=0.25 + 0.25 * shade)
    c.paint(quad(c, g, -0.15 * w, -9, -0.05 * w, -6), k=0.15)


def draw_calendar(c: Canvas) -> None:
    """A month sheet with days filled in by hand, and the pencil that did it."""
    w, h = c.w, c.h
    table(c, y=0.16, m=0.05)
    rng = c.rng(91)
    f = local(0.46 * w, 0.54 * h, -4)
    sw, shh = 0.66 * w, 0.84 * h
    sheet = quad(c, f, -sw / 2, -shh / 2, sw / 2, shh / 2)
    drop(c, sheet, 9, 12, 9, 0.65)
    c.paint(sheet)
    c.paint(quad(c, f, -sw / 2, -shh / 2, sw / 2, -shh * 0.30), c=0.85, m=0.12)
    rings = [c.circle(*f(x, -shh / 2), 6) for x in np.linspace(-sw * 0.40, sw * 0.40, 9)]
    c.paint(np.clip(sum(rings), 0, 1), k=0.85)
    gx0, gy0, gx1, gy1 = -sw * 0.45, -shh * 0.24, sw * 0.45, shh * 0.46
    cw, ch = (gx1 - gx0) / 7, (gy1 - gy0) / 5
    grid = [[f(gx0 + i * cw, gy0), f(gx0 + i * cw, gy1)] for i in range(8)]
    grid += [[f(gx0, gy0 + j * ch), f(gx1, gy0 + j * ch)] for j in range(6)]
    c.lay("k", 0.55 * c.lines(grid, 1.3))
    labels = [[f(gx0 + i * cw + 6, gy0 - 16), f(gx0 + i * cw + 6 + cw * 0.35, gy0 - 16),
               f(gx0 + i * cw + 6 + cw * 0.35, gy0 - 11), f(gx0 + i * cw + 6, gy0 - 11)] for i in range(7)]
    c.lay("k", 0.5 * c.polys(labels))
    for j in range(5):
        for i in range(7):
            x, y = gx0 + i * cw, gy0 + j * ch
            c.lay("k", 0.45 * quad(c, f, x + 5, y + 6, x + 5 + cw * 0.18, y + 10))
            r = rng.random()
            if r < 0.42 and not (j == 4 and i > 3):
                n = 1 + int(rng.random() * 4)
                marks = [[f(x + cw * 0.30 + k * 6, y + ch * 0.40), f(x + cw * 0.30 + k * 6 + 2, y + ch * 0.78)] for k in range(n)]
                if n == 4 and rng.random() < 0.6:
                    marks.append([f(x + cw * 0.22, y + ch * 0.72), f(x + cw * 0.30 + 26, y + ch * 0.46)])
                c.lay("k", 0.80 * c.lines(marks, 1.5))
            elif r < 0.58:
                c.lay("c", 0.22 * quad(c, f, x + 3, y + 3, x + cw - 3, y + ch - 3), halftone=True)
    for (i, j) in ((4, 1), (2, 3)):
        cx, cy = f(gx0 + (i + 0.5) * cw, gy0 + (j + 0.5) * ch)
        loop = []
        for s in range(0, 380, 6):
            a = math.radians(s)
            rr = 1 + 0.06 * math.sin(a * 3) + s / 3000
            loop.append((cx + math.cos(a) * cw * 0.55 * rr, cy + math.sin(a) * ch * 0.52 * rr))
        c.lay("m", 0.95 * c.lines([loop], 3.2))
    g = local(0.86 * w, 0.62 * h, -72)
    L = 0.36 * h
    body = quad(c, g, -L / 2, -7, L * 0.34, 7)
    tipw = c.poly([g(L * 0.34, -7), g(L * 0.50, 0), g(L * 0.34, 7)])
    lead = c.poly([g(L * 0.45, -2), g(L * 0.50, 0), g(L * 0.45, 2)])
    ferrule = quad(c, g, -L / 2, -7, -L * 0.42, 7)
    eraser = quad(c, g, -L * 0.58, -6.5, -L / 2, 6.5)
    drop(c, np.clip(body + tipw + ferrule + eraser, 0, 1), 5, 8, 4, 0.7)
    facet = (np.sin((c.X * math.cos(math.radians(18)) + c.Y * math.sin(math.radians(18))) * 0.9) > 0)
    c.paint(body, y=0.95, m=0.10 + 0.12 * facet)
    c.paint(tipw, y=0.30, m=0.12)
    c.paint(lead, k=0.85)
    c.paint(ferrule, k=0.30, c=0.15)
    c.paint(eraser, m=0.55, y=0.20)


def draw_window(c: Canvas) -> None:
    """Morning light through blinds on a plain wall, a plant on the sill."""
    w, h = c.w, c.h
    c.paint(np.ones((c.H, c.W), np.float32), y=0.16, m=0.08, c=0.16)
    # Plant on the sill (drawn first as masks so its shadow can fall inside the light).
    px, py = 0.30 * w, 0.80 * h
    pot = c.poly([(px - 46, py - 72), (px + 46, py - 72), (px + 36, py), (px - 36, py)])
    rim = c.rect(px - 50, py - 82, px + 50, py - 70)
    rng = c.rng(101)
    leaves = []
    for i in range(12):
        a = math.radians(-165 + i * 13 + (rng.random() - 0.5) * 10)
        L = 120 + rng.random() * 120
        bx, by = px + (rng.random() - 0.5) * 20, py - 80
        tx, ty = bx + math.cos(a) * L, by + math.sin(a) * L * 1.1
        mx, my = (bx + tx) / 2, (by + ty) / 2
        nx, ny = -(ty - by) / L * 11, (tx - bx) / L * 11
        leaves.append([(bx, by), (mx + nx, my + ny), (tx, ty), (mx - nx, my - ny)])
    leafm = c.polys(leaves)
    plant = np.clip(leafm + pot + rim, 0, 1)
    # Sunlight through the window: a skewed panel, striped by the slats of the blind.
    panel = blur(c.poly([(0.34 * w, 0.02 * h), (0.94 * w, 0.12 * h), (0.88 * w, 0.80 * h), (0.28 * w, 0.70 * h)]), 2.5 * SS)
    phase = (c.Y - (c.X - 0.3 * w) * 0.17) * 0.33
    slats = smoothstep(0.80, 0.96, 0.5 + 0.5 * np.sin(phase))        # thin shadow lines
    frame = blur(c.poly([(0.60 * w, 0.06 * h), (0.625 * w, 0.065 * h), (0.585 * w, 0.76 * h), (0.56 * w, 0.75 * h)]), 1.5 * SS)
    cast = blur(shift(plant, int(150 * SS), int(-40 * SS)), 3 * SS)
    light = panel * (1 - slats * 0.85) * (1 - frame * 0.9) * (1 - cast * 0.85)
    c.knock(light * 0.85, "cm")
    c.lay("y", 0.34 * light)
    c.lay("m", 0.06 * light)
    # Sill and the shadowed wall below it.
    c.paint(c.rect(0, 0.80 * h, w, 0.85 * h), y=0.10, c=0.04)
    c.paint(c.rect(0, 0.85 * h, w, h), c=0.34, m=0.18, y=0.08)
    c.lay("k", 0.25 * c.rect(0, 0.85 * h, w, 0.86 * h))
    c.paint(np.clip(pot + rim, 0, 1), y=0.70, m=0.55, c=0.05)
    c.lay("c", 0.30 * smoothstep(px - 10, px + 40, c.X) * pot)
    vein = 0.5 + 0.5 * np.sin(c.X * 0.2 + c.Y * 0.1)
    c.paint(leafm, c=0.72, y=0.55, m=0.05, k=0.10 + 0.15 * vein)


def draw_homes(c: Canvas) -> None:
    """An apartment block at dusk: some windows lit, some dark, everyone at home."""
    w, h = c.w, c.h
    t = smoothstep(0, 0.22, c.v)
    c.lay("c", 0.55 + 0.1 * t, halftone=True)
    c.lay("m", 0.30 + 0.25 * t, halftone=True)
    moon = np.clip(c.circle(0.10 * w, 0.08 * h, 18) - c.circle(0.10 * w + 8, 0.08 * h - 4, 16), 0, 1)
    c.knock(moon, "cm")
    c.lay("y", 0.5 * moon)
    facade = c.rect(0.05 * w, 0.14 * h, w, h)
    course = np.floor(c.Y / 5.0)
    brick = (np.sin((c.X + (course % 2) * 9) * math.pi / 18) > 0.93) | (np.mod(c.Y, 5.0) < 0.9)
    c.paint(facade, m=0.62, y=0.42, c=0.30 + 0.12 * brick)
    c.paint(c.rect(0.04 * w, 0.13 * h, w, 0.16 * h), c=0.55, m=0.45, k=0.35)
    rng = c.rng(111)
    cols, rows = 6, 5
    x0, y0, cw, rh = 0.10 * w, 0.20 * h, 0.15 * w, 0.165 * h
    for j in range(rows):
        for i in range(cols):
            wx, wy = x0 + i * cw, y0 + j * rh
            ww, wh = cw * 0.56, rh * 0.62
            win = c.rect(wx, wy, wx + ww, wy + wh)
            if rng.random() < 0.55:
                c.paint(win, y=0.88, m=0.18 + 0.10 * rng.random())
                kind = rng.random()
                if kind < 0.33:
                    c.paint(c.rect(wx, wy, wx + ww * 0.28, wy + wh), y=0.70, m=0.55)
                elif kind < 0.6:
                    pot = c.rect(wx + ww * 0.62, wy + wh * 0.78, wx + ww * 0.86, wy + wh)
                    leaf = c.circle(wx + ww * 0.74, wy + wh * 0.66, ww * 0.17)
                    c.paint(np.clip(pot + leaf, 0, 1), c=0.55, m=0.35, k=0.55)
                elif kind < 0.8:
                    lamp = c.poly([(wx + ww * 0.18, wy + wh * 0.46), (wx + ww * 0.40, wy + wh * 0.46),
                                   (wx + ww * 0.44, wy + wh * 0.62), (wx + ww * 0.14, wy + wh * 0.62)])
                    c.paint(lamp, y=1.0, m=0.45)
                    c.paint(c.rect(wx + ww * 0.285, wy + wh * 0.62, wx + ww * 0.295, wy + wh), k=0.7)
            else:
                c.paint(win, c=0.75, m=0.45, k=0.25 + 0.15 * rng.random())
                refl = c.poly([(wx + ww * 0.55, wy), (wx + ww * 0.75, wy), (wx + ww * 0.30, wy + wh), (wx + ww * 0.10, wy + wh)])
                c.knock(refl * win * 0.35, "cmk")
            c.paint(c.rect(wx + ww / 2 - 1.5, wy, wx + ww / 2 + 1.5, wy + wh) * win, k=0.75, c=0.3)
            c.paint(c.rect(wx - 4, wy + wh, wx + ww + 4, wy + wh + 6), y=0.18, m=0.10, c=0.12)
    fe = []
    for j in range(rows):
        y = y0 + j * rh + rh * 0.68
        fe.append([(0.86 * w, y), (0.99 * w, y)])
        fe.append([(0.86 * w, y - rh * 0.22), (0.99 * w, y - rh * 0.22)])
        fe += [[(x, y), (x, y - rh * 0.22)] for x in np.linspace(0.86 * w, 0.99 * w, 7)]
        fe.append([(0.88 * w, y), (0.96 * w, y + rh)])
    c.paint(c.lines(fe, 2.0), k=0.92, c=0.2)


def draw_courthouse(c: Canvas) -> None:
    """A courthouse portico in raking afternoon light."""
    w, h = c.w, c.h
    t = smoothstep(0, 0.4, c.v)
    c.lay("c", 0.38 * (1 - t) + 0.08, halftone=True)
    stone = dict(y=0.12, m=0.04, c=0.02)
    c.paint(c.poly([(0.04 * w, 0.22 * h), (0.50 * w, 0.02 * h), (0.96 * w, 0.22 * h)]), **stone)
    tymp = c.poly([(0.12 * w, 0.205 * h), (0.50 * w, 0.045 * h), (0.88 * w, 0.205 * h)])
    c.lay("c", 0.22 * tymp)
    c.lay("m", 0.10 * tymp)
    c.paint(c.rect(0.02 * w, 0.22 * h, 0.98 * w, 0.32 * h), **stone)
    c.lay("c", 0.30 * c.rect(0.02 * w, 0.30 * h, 0.98 * w, 0.32 * h))
    c.lay("k", 0.45 * c.rect(0.02 * w, 0.22 * h, 0.98 * w, 0.228 * h))
    dent = c.polys([[(x, 0.285 * h), (x + 6, 0.285 * h), (x + 6, 0.30 * h), (x, 0.30 * h)] for x in np.arange(0.03 * w, 0.97 * w, 12)])
    c.lay("c", 0.35 * dent)
    c.paint(c.rect(0.04 * w, 0.32 * h, 0.96 * w, 0.80 * h), c=0.42, m=0.26, y=0.08)
    c.paint(c.rect(0.43 * w, 0.46 * h, 0.57 * w, 0.80 * h), c=0.70, m=0.45, k=0.45)
    c.lay("y", 0.35 * c.rect(0.435 * w, 0.47 * h, 0.565 * w, 0.52 * h))
    xs = np.linspace(0.11 * w, 0.89 * w, 6)
    cr = 0.034 * w
    for x in xs:
        cast = c.poly([(x + cr, 0.32 * h), (x + cr + 0.09 * w, 0.32 * h), (x + cr + 0.05 * w, 0.80 * h), (x - cr * 0.2, 0.80 * h)])
        c.lay("c", 0.22 * cast)
        c.lay("m", 0.12 * cast)
        c.lay("k", 0.12 * cast)
    for x in xs:
        shaft = c.poly([(x - cr, 0.34 * h), (x + cr, 0.34 * h), (x + cr * 1.08, 0.79 * h), (x - cr * 1.08, 0.79 * h)])
        dx = (c.X - x) / (cr * 1.05)
        shade = smoothstep(-0.55, 0.85, dx)
        flutes = (np.cos(np.arcsin(np.clip(dx, -0.999, 0.999)) * 18) > 0.70) * 0.10
        c.paint(shaft, y=0.10, c=0.05 + 0.45 * shade + flutes * (0.3 + shade), m=0.02 + 0.25 * shade)
        ends = np.clip(c.rect(x - cr * 1.45, 0.32 * h, x + cr * 1.45, 0.345 * h) + c.rect(x - cr * 1.40, 0.785 * h, x + cr * 1.40, 0.81 * h), 0, 1)
        c.paint(ends, **stone)
        c.lay("c", 0.40 * ends * smoothstep(x, x + cr * 1.4, c.X))
    for i in range(6):
        y0 = (0.81 + i * 0.032) * h
        c.paint(c.rect(0, y0, w, y0 + 0.012 * h), y=0.14, m=0.04)
        c.paint(c.rect(0, y0 + 0.012 * h, w, y0 + 0.032 * h), c=0.30, m=0.18, y=0.06)


def draw_certificate(c: Canvas) -> None:
    """The corner of a certificate: lathe-work border, rosette, and a foil seal."""
    w, h = c.w, c.h
    c.paint(np.ones((c.H, c.W), np.float32), c=0.78, m=0.52, k=0.30)
    f = local(0.56 * w, 0.54 * h, -6)
    sw, sh_ = 1.05 * w, 1.0 * h
    sheet = quad(c, f, -sw / 2, -sh_ / 2, sw / 2, sh_ / 2)
    drop(c, sheet, 10, 14, 10, 0.9)
    c.paint(sheet, y=0.10, m=0.03)
    band = 46
    for gx, gy, deg, length, phase in ((0, -sh_ / 2 + band / 2 + 18, -6, sw - 40, 0.0), (-sw / 2 + band / 2 + 18, 0, 84, sh_ - 40, 1.0)):
        g = local(*f(gx, gy), deg)
        paths = guilloche(g, length, band, 26, 7, band * 0.45, phase=phase)
        c.lay("c", 0.85 * c.lines(paths, 1.0))
        c.lay("m", 0.25 * c.lines(paths[::2], 1.0))
    c.lay("c", 0.9 * c.lines([[f(-sw / 2 + 80, -sh_ / 2 + 80), f(sw / 2, -sh_ / 2 + 80)],
                              [f(-sw / 2 + 80, -sh_ / 2 + 80), f(-sw / 2 + 80, sh_ / 2)]], 1.6))
    rx, ry = f(-sw / 2 + 41, -sh_ / 2 + 41)
    c.lay("m", 0.9 * c.lines([rosette(rx, ry, 34, 34 / k, 22, k, rot=0.2 * k) for k in (5, 7)], 0.9))
    cx, cy = f(0.05 * sw, 0.05 * sh_)
    big = [rosette(cx, cy, 170, 170 / 13, 110, 13, rot=0.05), rosette(cx, cy, 120, 120 / 11, 80, 11, rot=0.3)]
    c.lay("c", 0.32 * c.lines(big, 0.9))
    c.lay("k", 0.80 * text_lines(c, f, -sw * 0.30, -sh_ * 0.24, [sw * 0.40], 0, sh_ * 0.030))
    c.lay("k", 0.55 * c.lines([[f(-sw * 0.36, -sh_ * 0.10 + i * sh_ * 0.09), f(sw * 0.10, -sh_ * 0.10 + i * sh_ * 0.09)] for i in range(5)], 1.1))
    c.lay("k", 0.45 * text_lines(c, f, -sw * 0.36, -sh_ * 0.12, [sw * 0.10, sw * 0.14, sw * 0.08, sw * 0.12, sw * 0.09], sh_ * 0.09, sh_ * 0.012))
    sx, sy, sr = 0.80 * w, 0.74 * h, 0.13 * w
    teeth = []
    for s in range(721):
        a = math.radians(s / 2)
        rr = sr * (1 + 0.045 * (1 if (s // 6) % 2 else -1))
        teeth.append((sx + math.cos(a) * rr, sy + math.sin(a) * rr))
    seal = c.poly(teeth)
    for dxr, deg in ((-0.04 * w, 12), (0.035 * w, -10)):
        g = local(sx + dxr, sy + sr * 1.05, deg)
        tail = c.poly([g(-16, -30), g(16, -30), g(16, 70), g(0, 56), g(-16, 70)])
        drop(c, tail, 4, 6, 4, 0.6)
        c.paint(tail, m=0.85, y=0.25, c=0.10)
    drop(c, seal, 4, 6, 4, 0.8)
    shine = 0.5 + 0.5 * np.cos((c.X - sx) * 0.035 - (c.Y - sy) * 0.02)
    c.paint(seal, y=0.85, m=0.22 + 0.22 * (1 - shine), k=0.06 + 0.18 * (1 - shine))
    rings = c.lines([[(sx + math.cos(math.radians(a)) * sr * k, sy + math.sin(math.radians(a)) * sr * k) for a in range(0, 361, 3)] for k in (0.80, 0.74)], 1.6)
    c.lay("m", 0.45 * rings)
    c.lay("k", 0.25 * rings)
    c.lay("m", 0.40 * c.lines([rosette(sx, sy, sr * 0.55, sr * 0.55 / 8, sr * 0.35, 8)], 1.0))


def draw_cards(c: Canvas) -> None:
    """A passport booklet, a plastic ID card, and a paper card, laid out on a desk."""
    w, h = c.w, c.h
    table(c, y=0.18, m=0.10, grain_scale=1.4)
    f = local(0.30 * w, 0.52 * h, -8)
    bw, bh = 0.30 * w, 0.42 * w
    book = quad(c, f, -bw / 2, -bh / 2, bw / 2, bh / 2)
    drop(c, book, 9, 13, 9, 0.75)
    c.paint(book, c=0.92, m=0.70, k=0.40)
    c.lay("k", 0.35 * quad(c, f, -bw / 2, -bh / 2, -bw / 2 + 10, bh / 2))
    gold = dict(y=0.85, m=0.20)
    ex, ey = f(0, -bh * 0.05)
    c.paint(c.lines([rosette(ex, ey, bw * 0.24, bw * 0.24 / 9, bw * 0.14, 9, rot=0.3)], 1.4) * book, **gold)
    ring = c.lines([[(ex + math.cos(math.radians(a)) * bw * 0.30, ey + math.sin(math.radians(a)) * bw * 0.30) for a in range(0, 361, 3)]], 1.6)
    c.paint(ring * book, **gold)
    c.paint(text_lines(c, f, -bw * 0.26, -bh * 0.38, [bw * 0.52], 0, bh * 0.025) * book, **gold)
    c.paint(text_lines(c, f, -bw * 0.18, bh * 0.30, [bw * 0.36], 0, bh * 0.020) * book, **gold)
    c.paint(quad(c, f, -bw * 0.08, bh * 0.38, bw * 0.08, bh * 0.44) * book, **gold)

    g = local(0.69 * w, 0.40 * h, 6)
    cw_, ch_ = 0.44 * w, 0.44 * w / 1.586
    card = np.clip((blur(quad(c, g, -cw_ / 2, -ch_ / 2, cw_ / 2, ch_ / 2), 2 * SS) - 0.5) * 3 + 0.5, 0, 1)
    drop(c, card, 7, 10, 7, 0.7)
    c.paint(card, c=0.08, y=0.05)
    c.lay("c", 0.45 * c.lines(guilloche(g, cw_, ch_, 9, 14, ch_ * 0.10, phase=0.4), 0.8) * card)
    c.lay("m", 0.30 * c.lines(guilloche(g, cw_, ch_, 7, 10, ch_ * 0.12, phase=2.0), 0.8) * card)
    c.paint(quad(c, g, -cw_ / 2, -ch_ / 2, cw_ / 2, -ch_ * 0.30) * card, c=0.85, m=0.10)
    c.knock(text_lines(c, g, -cw_ * 0.44, -ch_ * 0.43, [cw_ * 0.40], 0, ch_ * 0.06) * card, "cmk")
    photo = quad(c, g, -cw_ * 0.44, -ch_ * 0.20, -cw_ * 0.12, ch_ * 0.38)
    pv = smoothstep(-ch_ * 0.2, ch_ * 0.4, c.Y - g(0, 0)[1])
    c.paint(photo, c=0.35 + 0.25 * pv, m=0.20 + 0.15 * pv, k=0.10, halftone="cm")
    c.lay("k", 0.60 * text_lines(c, g, -cw_ * 0.05, -ch_ * 0.16, [cw_ * 0.40, cw_ * 0.30, cw_ * 0.36, cw_ * 0.22, cw_ * 0.32], ch_ * 0.10, ch_ * 0.035) * card)
    holo = c.circle(*g(cw_ * 0.36, ch_ * 0.30), ch_ * 0.10)
    c.lay("y", 0.55 * holo)
    c.lay("c", 0.30 * holo * (np.sin(c.X * 0.6) > 0))
    c.lay("m", 0.30 * holo * (np.sin(c.Y * 0.6) > 0))

    k_ = local(0.70 * w, 0.78 * h, -4)
    pw, ph = 0.40 * w, 0.40 * w / 1.6
    paper = quad(c, k_, -pw / 2, -ph / 2, pw / 2, ph / 2) * (1 - card)
    drop(c, paper, 6, 9, 6, 0.6)
    c.paint(paper, c=0.10, y=0.04)
    c.lay("c", 0.40 * c.lines(guilloche(k_, pw, ph, 5, 9, ph * 0.18, phase=0.9), 0.8) * paper)
    c.lay("k", 0.55 * text_lines(c, k_, -pw * 0.30, ph * 0.05, [pw * 0.60, pw * 0.42], ph * 0.16, ph * 0.05) * paper)
    c.lay("m", 0.55 * c.lines([[k_(-pw * 0.30, ph * 0.40), k_(pw * 0.25, ph * 0.40)]], 1.2) * paper)


def draw_letters(c: Canvas) -> None:
    """Big serif letters pulled in two inks, slightly out of register."""
    w, h = c.w, c.h
    c.lay("y", 0.10 + 0.04 * c.noise(80, 2, salt=141))
    a = c.text((0.07 * w, 0.84 * h), "A", 0.86 * h, 700)
    b = shift(c.text((0.50 * w, 0.84 * h), "a", 0.86 * h, 600), int(-6 * SS), int(3 * SS))
    c.lay("c", 0.95 * a)
    c.lay("m", 0.92 * b)
    starve = c.noise(6, 3, salt=142)
    c.knock(a * (starve > 0.86) * 0.6, "c")
    c.knock(b * (starve < 0.12) * 0.6, "m")
    c.lay("k", 0.85 * c.rect(0.05 * w, 0.875 * h, 0.95 * w, 0.880 * h))
    cols = [[(0.05 * w + i * 0.30 * w, 0.91 * h), (0.05 * w + i * 0.30 * w + 0.27 * w, 0.91 * h)] for i in range(3)]
    cols += [[(0.05 * w + i * 0.30 * w, 0.94 * h), (0.05 * w + i * 0.30 * w + 0.20 * w, 0.94 * h)] for i in range(3)]
    c.lay("k", 0.45 * c.lines(cols, 2.0))


def draw_archive(c: Canvas) -> None:
    """Archive shelving: rows of record boxes and leaning folders."""
    w, h = c.w, c.h
    c.paint(np.ones((c.H, c.W), np.float32), c=0.50, m=0.32, y=0.10)
    rng = c.rng(151)
    sh_h = 0.27 * h
    for sy in (0.06, 0.37, 0.68):
        floor = sy * h + sh_h
        x = 0.03 * w
        while x < 0.97 * w:
            if rng.random() < 0.70:
                bw = 0.12 * w * (0.85 + 0.3 * rng.random())
                bh = sh_h * (0.78 + 0.16 * rng.random())
                box = c.rect(x, floor - bh, x + bw, floor)
                if rng.random() < 0.7:
                    c.paint(box, y=0.42, m=0.18, c=0.05)
                else:
                    c.paint(box, c=0.62, m=0.10, y=0.04)
                c.lay("k", 0.25 * c.rect(x, floor - bh, x + bw, floor - bh + 8))
                c.paint(c.rect(x + bw * 0.18, floor - bh * 0.72, x + bw * 0.82, floor - bh * 0.48), y=0.04)
                c.lay("k", 0.65 * text_lines(c, ident, x + bw * 0.24, floor - bh * 0.68, [bw * 0.40, bw * 0.30], bh * 0.07, bh * 0.025))
                c.paint(c.rect(x + bw * 0.38, floor - bh * 0.30, x + bw * 0.62, floor - bh * 0.22, radius=4), k=0.85, c=0.3)
                c.lay("c", 0.25 * box * smoothstep(x + bw * 0.6, x + bw, c.X))
                x += bw + 4
            else:
                lean = 0.0
                for _ in range(2 + int(rng.random() * 3)):
                    fw = 14 + rng.random() * 8
                    lean = 3 + rng.random() * 7
                    fh = sh_h * (0.70 + 0.2 * rng.random())
                    folder = c.poly([(x, floor), (x + fw, floor), (x + fw + lean, floor - fh), (x + lean, floor - fh)])
                    col = rng.random()
                    if col < 0.33:
                        c.paint(folder, m=0.70, y=0.20)
                    elif col < 0.66:
                        c.paint(folder, y=0.70, m=0.10)
                    else:
                        c.paint(folder, c=0.80, m=0.15)
                    c.paint(c.rect(x + lean + 2, floor - fh - 8, x + lean + fw * 0.8, floor - fh), y=0.05)
                    x += fw + 1
                x += lean + 6
        c.paint(c.rect(0, floor, w, floor + 0.025 * h), c=0.18, m=0.10, y=0.20)
        c.lay("k", 0.35 * c.rect(0, floor + 0.022 * h, w, floor + 0.03 * h))
        c.lay("k", 0.18 * blur(c.rect(0, floor + 0.03 * h, w, floor + 0.05 * h), 4 * SS))
    c.paint(np.clip(c.rect(0, 0, 0.02 * w, h) + c.rect(0.98 * w, 0, w, h), 0, 1), c=0.70, m=0.45, k=0.45)


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Print:
    name: str
    size: tuple[int, int]
    subject: str
    draw: Callable[[Canvas], None]
    seed: int


PRINTS: dict[str, Print] = {p.name: p for p in [
    Print("front", (960, 720), "Coast, volcano, and mesas under one sky, joined by a road", draw_front, 2027),
    Print("oregon", (960, 640), "Snow volcano over foothill forest and a river at first light", draw_oregon, 1859),
    Print("washington", (960, 640), "Glaciated volcano over a sound, mist between ridges, a ferry", draw_washington, 1889),
    Print("utah", (960, 640), "Red-rock arch on slickrock under a deep blue sky", draw_utah, 1896),
    Print("fog", (960, 640), "Sea stacks fading into coastal fog", draw_fog, 404),
    Print("mail", (960, 640), "Window envelopes and an opened notice on a table", draw_mail, 1913),
    Print("calendar", (960, 640), "A month sheet with days tallied by hand, and a pencil", draw_calendar, 1582),
    Print("window", (960, 640), "Morning light through blinds, a plant on the sill", draw_window, 1971),
    Print("homes", (960, 640), "An apartment block at dusk, windows lit and dark", draw_homes, 1937),
    Print("courthouse", (960, 640), "A courthouse portico in raking afternoon light", draw_courthouse, 1787),
    Print("certificate", (960, 640), "Certificate corner: lathe-work border, rosette, foil seal", draw_certificate, 1906),
    Print("cards", (960, 640), "Passport booklet, plastic ID card, and a paper card", draw_cards, 1936),
    Print("letters", (960, 640), "Big serif letters pulled in two inks", draw_letters, 1440),
    Print("archive", (960, 640), "Archive shelves of record boxes and leaning folders", draw_archive, 1934),
]}


def write_responsive_variant(full_path: Path, width: int = RESPONSIVE_WIDTH) -> Path:
    """Write a responsive WebP variant (`print-<name>-480.webp`) beside a full-resolution print."""
    target = full_path.with_name(f"{full_path.stem}-{width}.webp")
    with Image.open(full_path) as im:
        src_w, src_h = im.size
        height = max(1, int(round(src_h * (width / src_w))))
        resized = im.resize((width, height), Image.Resampling.LANCZOS)
        resized.save(target, "WEBP", quality=72, method=6)
    return target


def render(name: str, out_dir: Path = OUT_DIR, preview: Path | None = None) -> Path:
    if np is None:
        raise RuntimeError("numpy is required to render press prints from scratch (pip install -r requirements-art.txt)")
    p = PRINTS[name]
    canvas = Canvas(p.size[0], p.size[1], p.seed)
    p.draw(canvas)
    img = canvas.render()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"print-{name}.webp"
    img.save(target, "WEBP", quality=70, method=6)
    write_responsive_variant(target)
    if preview is not None:
        preview.mkdir(parents=True, exist_ok=True)
        img.save(preview / f"print-{name}.png")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render CivicDesk editorial press prints into static/img/art/.")
    parser.add_argument("names", nargs="*", help="Optional print names to render (default: all prints)")
    parser.add_argument("--list", action="store_true", help="List print names, sizes, and subjects")
    parser.add_argument("--preview", type=Path, default=None, help="Also write full-size PNG proofs to DIR")
    parser.add_argument(
        "--responsive-only",
        action="store_true",
        help=f"Regenerate {RESPONSIVE_WIDTH}w WebP variants from existing full-size prints",
    )
    args = parser.parse_args(argv)

    if args.list:
        for p in PRINTS.values():
            print(f"print-{p.name}.webp  {p.size[0]}x{p.size[1]}  {p.subject}")
        return 0

    names = args.names or list(PRINTS)
    unknown = [n for n in names if n not in PRINTS]
    if unknown:
        print(f"Unknown print(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    if args.responsive_only:
        for n in names:
            full_path = OUT_DIR / f"print-{n}.webp"
            if not full_path.is_file():
                print(f"Missing full-size print: {full_path}", file=sys.stderr)
                return 2
            small = write_responsive_variant(full_path)
            print(f"Wrote {small.relative_to(ROOT)} ({small.stat().st_size // 1024} KB)")
        return 0

    for n in names:
        t = render(n, preview=args.preview)
        print(f"Wrote {t.relative_to(ROOT)} ({t.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
