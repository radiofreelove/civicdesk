"""Self-hosted font verification and drop-in brand asset validation."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path

from .paths import STATIC

DEFAULT_BRAND = {
    "custom_artwork": False,
    "mode": "mark_and_wordmark",
    "logo": "",
    "logo_dark": "",
    "favicon_svg": "favicon.svg",
    "apple_touch_icon": "apple-touch-icon.png",
    "og_image": "og-card.png",
    "og_image_alt": "CivicDesk faceted desk mark on press ink",
}
VALID_BRAND_MODES = {"mark_and_wordmark", "lockup_only"}

FONT_HASHES = {
    "AtkinsonHyperlegibleNext-Regular.woff2": "5cb240a4d99ab7e6fc3a4d4e6d11d89789fdffe9c86a8384000d662c53283f6f",
    "AtkinsonHyperlegibleNext-Bold.woff2": "0dbe07ed7679407db9d90f747d5ca3bd1b2bb7ccbcbe41c8246cfc6d2c7bc3bc",
    "OFL.txt": "474a034e99845062f661855caaa505880d0e9009deddc2727a92795eab374955",
    "SourceSerif4-Latin.woff2": "8e38e3fbd74dea20aabe0831ffef04c9f30bf6fc5b99b7a2649be0dd8c6d859c",
    "SourceSerif4-LatinExt.woff2": "231f594ad829d45f905a8a6cdce704f659615e00f1a9db2699320e6e315c1d1f",
    "SourceSerif4-OFL.txt": "c21d7293d87b6d7ab1d0229a2f55b77f33a7613a6a4e66f6693d68d7d8d09464",
}
# Source Serif 4 is the default text face, so these files are required. Atkinson stays optional.
REQUIRED_FONTS = ("SourceSerif4-Latin.woff2", "SourceSerif4-LatinExt.woff2", "SourceSerif4-OFL.txt")


def verify_fonts(errors: list[str], fonts_dir: Path | None = None) -> bool:
    """Check self-hosted fonts against FONT_HASHES.

    Source Serif 4 (the default face) is required: a missing file or a hash mismatch is a build
    error. The return value reports whether the optional Atkinson Hyperlegible Next set is present,
    which controls the reader-font toggle in Reading settings.
    """
    fonts_dir = fonts_dir or (STATIC / "fonts")
    atkinson_present = True
    for fname, expected_sha in FONT_HASHES.items():
        fpath = fonts_dir / fname
        if not fpath.is_file():
            if fname in REQUIRED_FONTS:
                errors.append(f"required font file missing: static/fonts/{fname}")
            else:
                atkinson_present = False
            continue
        digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if digest != expected_sha:
            errors.append(f"font checksum mismatch for {fname}: expected {expected_sha}, got {digest}")
    return atkinson_present


def validate_svg_asset(svg_path: Path, label: str, errors: list[str]) -> None:
    try:
        raw_svg = svg_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(f"{label} ({svg_path.name}): SVG file is not valid UTF-8")
        return

    if "<!ENTITY" in raw_svg.upper() or "<!DOCTYPE" in raw_svg.upper():
        errors.append(f"{label} ({svg_path.name}): DOCTYPE and XML ENTITY declarations are forbidden")
        return

    try:
        root = ET.fromstring(raw_svg)
    except ET.ParseError as exc:
        errors.append(f"{label} ({svg_path.name}): invalid SVG XML ({exc})")
        return

    if root.tag.split("}")[-1].lower() != "svg":
        errors.append(f"{label} ({svg_path.name}): root XML element must be <svg>")
        return

    if not root.attrib.get("viewBox", "").strip():
        errors.append(f"{label} ({svg_path.name}): <svg> must define a non-empty viewBox attribute")

    for el in root.iter():
        local_tag = el.tag.split("}")[-1].lower()
        if local_tag in {"style", "script", "foreignobject"}:
            errors.append(f"{label} ({svg_path.name}): <{local_tag}> element forbidden by strict CSP")
        for attr_name, attr_val in el.attrib.items():
            local_attr = attr_name.split("}")[-1].lower()
            if local_attr == "style" or local_attr.startswith("on"):
                errors.append(f"{label} ({svg_path.name}): '{local_attr}' attribute forbidden by strict CSP")
            val_lower = attr_val.strip().lower()
            if (
                "http://" in val_lower
                or "https://" in val_lower
                or val_lower.startswith("//")
                or "javascript:" in val_lower
                or "data:text/html" in val_lower
            ):
                errors.append(
                    f"{label} ({svg_path.name}): external or unsafe reference in '{local_attr}' attribute"
                )


def validate_brand_config(
    site: dict,
    static_dir: Path = STATIC,
    errors: list[str] | None = None,
) -> dict:
    err_list = errors if errors is not None else []
    raw_brand = site.get("brand")
    if raw_brand is None:
        raw_brand = {}
    elif not isinstance(raw_brand, Mapping):
        err_list.append("site.yaml: 'brand' must be a mapping")
        raw_brand = {}

    unknown = sorted(set(raw_brand) - set(DEFAULT_BRAND))
    if unknown:
        err_list.append(f"site.yaml: unknown brand key(s): {unknown}")

    brand = {**DEFAULT_BRAND, **dict(raw_brand)}

    if not isinstance(brand["custom_artwork"], bool):
        err_list.append("site.yaml: brand.custom_artwork must be a boolean")

    if brand["mode"] not in VALID_BRAND_MODES:
        err_list.append(
            f"site.yaml: brand.mode '{brand['mode']}' must be one of {sorted(VALID_BRAND_MODES)}"
        )

    for key in ("logo", "logo_dark", "favicon_svg", "apple_touch_icon", "og_image", "og_image_alt"):
        brand[key] = str(brand.get(key) or "").strip()

    if not brand["favicon_svg"]:
        err_list.append("site.yaml: brand.favicon_svg must not be empty")
    if not brand["og_image"]:
        err_list.append("site.yaml: brand.og_image must not be empty")
    if not brand["og_image_alt"]:
        err_list.append("site.yaml: brand.og_image_alt must not be empty")

    if brand["mode"] == "lockup_only" and not brand["logo"]:
        err_list.append("site.yaml: brand.mode 'lockup_only' requires a non-empty brand.logo path")
    if brand["logo_dark"] and not brand["logo"]:
        err_list.append("site.yaml: brand.logo_dark requires brand.logo to be set")

    for key in ("logo", "logo_dark", "favicon_svg", "apple_touch_icon", "og_image"):
        rel = brand[key]
        if not rel:
            continue
        if rel.startswith("/") or "\\" in rel or ".." in Path(rel).parts:
            err_list.append(f"site.yaml: brand.{key} '{rel}' must be a clean relative path under static/")
            continue
        fpath = static_dir / rel
        if not fpath.is_file() or fpath.stat().st_size == 0:
            err_list.append(f"site.yaml: brand.{key} '{rel}' not found or empty under static/")
            continue
        if rel.lower().endswith(".svg"):
            validate_svg_asset(fpath, f"brand.{key}", err_list)

    site["brand"] = brand
    return brand
