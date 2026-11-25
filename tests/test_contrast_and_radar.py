from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = ROOT / "static" / "css" / "site.css"


def load_script_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def extract_tokens(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{3,6})", block)}


def test_horizon_palette_and_aaa_contrast() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")

    def block_after(selector_re: str) -> str:
        m = re.search(selector_re + r"\s*\{(.*?)\n\}", css, flags=re.S)
        assert m, f"Missing CSS block {selector_re}"
        return m.group(1)

    light = extract_tokens(block_after(r':root,\s*html\[data-theme="light"\]'))
    warm = {**light, **extract_tokens(block_after(r'html\[data-theme="warm"\]'))}
    dark = extract_tokens(block_after(r'html\[data-theme="dark"\]'))
    contrast = {**light, **extract_tokens(block_after(r'html\[data-theme="contrast"\]'))}
    auto_dark_match = re.search(r'prefers-color-scheme: dark\)\s*\{\s*html\[data-theme="auto"\]\s*\{(.*?)\}', css, re.S)
    assert auto_dark_match
    auto_dark = extract_tokens(auto_dark_match.group(1))

    # Verify exact Civic Horizon palette anchors
    assert light["horizon-sky"].upper() == "#1D70B8"
    assert light["horizon-paper"].upper() == "#FFFFFF"
    assert light["horizon-rose"].upper() == "#C85A70"
    assert light["horizon-slate"].upper() == "#111827"

    for k, v in dark.items():
        if k.startswith("horizon-"):
            continue
        assert auto_dark.get(k, "").upper() == v.upper(), f"auto-dark mismatch on --{k}"

    text_pairs = [
        ("text", "bg"), ("text", "surface"), ("text", "card-bg"),
        ("heading", "bg"), ("heading", "surface"),
        ("link", "bg"), ("link", "surface"), ("link", "card-bg"),
        ("bar-text", "bar-bg"), ("bar-accent", "bar-bg"),
        ("header-text", "header-bg"), ("header-accent", "header-bg"),
        ("text", "why-bg"), ("link", "why-bg"), ("heading", "why-bg"),
        ("text", "watch-bg"), ("link", "watch-bg"), ("watch-title", "watch-bg"), ("watch-title", "badge-bg"),
        ("text", "changed-bg"), ("link", "changed-bg"), ("heading", "changed-bg"),
        ("text", "cost-bg"), ("link", "cost-bg"), ("heading", "cost-bg"),
        ("btn-text", "btn-bg"), ("exit-text", "exit-bg"), ("focus-text", "focus-bg"),
        ("link", "badge-bg"), ("text", "badge-bg"), ("text", "code-bg"),
    ]
    ui_pairs = [
        ("border", "bg"), ("border", "surface"),
        ("focus-ring", "bg"), ("focus-ring", "surface"),
        ("nav-current", "header-bg"),
        ("mark-sky", "mark-bg"), ("mark-rose", "mark-bg"), ("mark-paper", "mark-bg"),
    ]

    for theme_name, tokens in [("light", light), ("warm", warm), ("dark", dark), ("contrast", contrast)]:
        for fg, bg in text_pairs:
            r = contrast_ratio(tokens[fg], tokens[bg])
            assert r >= 7.0, f"{theme_name}: {fg} ({tokens[fg]}) on {bg} ({tokens[bg]}) = {r:.2f}:1 (< 7.0 AAA)"
        for fg, bg in ui_pairs:
            r = contrast_ratio(tokens[fg], tokens[bg])
            assert r >= 3.0, f"{theme_name}: UI/mark {fg} ({tokens[fg]}) on {bg} ({tokens[bg]}) = {r:.2f}:1 (< 3.0)"


def test_policy_radar_both_desks_offline() -> None:
    radar = load_script_module("policy_radar", "scripts/policy_radar.py")
    verify = load_script_module("verify_sources", "scripts/verify_sources.py")

    assert radar.USER_AGENT == "CivicDesk-Verify/1.0"
    assert verify.USER_AGENT == "CivicDesk-Verify/1.0"

    med_dockets = radar.discover_dockets("medicaid")
    assert {"73546826", "74699899", "74784748", "75165734"} <= set(med_dockets)

    id_dockets = radar.discover_dockets("id-records")
    assert "69623342" in id_dockets

    def fake_fetcher(url: str, **_kwargs) -> bytes:
        if "federalregister.gov" in url:
            return json.dumps({
                "results": [{
                    "document_number": "2026-99999",
                    "publication_date": "2026-11-24",
                    "title": "Medicaid Community Engagement Update",
                    "html_url": "https://www.federalregister.gov/d/2026-99999",
                    "abstract": "Clarifies hold harmless period.",
                }]
            }).encode("utf-8")
        if "ecfr.gov" in url:
            return json.dumps({"content_versions": []}).encode("utf-8")
        if "courtlistener.com" in url:
            return (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
            )
        return b'<?xml version="1.0" encoding="utf-8"?><rss><channel></channel></rss>'

    res_med = radar.run_sweep(
        "medicaid",
        days=7,
        today=dt.date(2026, 11, 25),
        fetcher=fake_fetcher,
        env={},
    )
    assert res_med["new_count"] == 1
    md = radar.render_markdown_report(res_med)
    assert "radar-item:" in md

    # Deduplication skips already-seen marker
    marker_id = res_med["items"][0]["id"]
    res_med_dedup = radar.run_sweep(
        "medicaid",
        days=7,
        today=dt.date(2026, 11, 25),
        seen_markers={marker_id},
        fetcher=fake_fetcher,
        env={},
    )
    assert res_med_dedup["new_count"] == 0

    res_id = radar.run_sweep(
        "id-records",
        days=7,
        today=dt.date(2026, 11, 25),
        fetcher=fake_fetcher,
        env={"LEGISCAN_API_KEY": "supersecret123"},
    )
    # Secret redaction check when fake_fetcher returns XML to LegiScan JSON parser
    legiscan_status = [s["status"] for s in res_id["statuses"] if s["source"] == "LegiScan API"][0]
    assert "supersecret123" not in legiscan_status
