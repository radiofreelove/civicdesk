"""Repository paths, built-file mapping, and WebP dimension discovery."""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
SITE_ROOT = ROOT / "site-root"
DEFAULT_OUT = ROOT / "public"
ART_NAME_RE = re.compile(r"print-[a-z0-9-]+\.webp")


def built_html_path(out_dir: Path, page: Mapping) -> Path:
    """Resolve the compiled HTML path under out_dir for a data/pages.yaml entry."""
    slug = str(page.get("slug") or "")
    route = str(page.get("route") or "/")
    if slug == "404":
        return out_dir / "404.html"
    if route == "/":
        return out_dir / "index.html"
    return out_dir / route.strip("/") / "index.html"


def webp_dimensions(data: bytes) -> tuple[int, int]:
    """Read pixel (width, height) directly from a WebP file header (VP8X/VP8L/VP8 )."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("Not a valid WebP RIFF container")
    chunk = data[12:16]
    if chunk == b"VP8X":
        return (
            1 + int.from_bytes(data[24:27], "little"),
            1 + int.from_bytes(data[27:30], "little"),
        )
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8 ":
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    raise ValueError(f"Unsupported WebP chunk {chunk!r}")


def discover_art_dims(art_dir: Path) -> dict[str, tuple[int, int]]:
    """Return {filename: (width, height)} for all WebP press prints in art_dir."""
    dims: dict[str, tuple[int, int]] = {}
    if not art_dir.is_dir():
        return dims
    for fpath in sorted(art_dir.glob("*.webp")):
        dims[fpath.name] = webp_dimensions(fpath.read_bytes())
    return dims
