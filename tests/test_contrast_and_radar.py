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
    return {m.group(1): m.group(2) for m in re.finditer(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{3,6})", block)}


def test_broadsheet_palette_and_aaa_contrast() -> None:
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

    # Verify exact Broadsheet press anchors
    assert light["press-cyan"].upper() == "#0088B0"
    assert light["press-paper"].upper() == "#F3F2F2"
    assert light["press-magenta"].upper() == "#D6006C"
    assert light["press-ink"].upper() == "#201E1D"

    for k, v in dark.items():
        if k.startswith("press-"):
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
        ("text", "viz-surface"), ("heading", "viz-surface"), ("link", "viz-surface"),
        ("text", "viz-surface-2"), ("heading", "viz-surface-2"), ("link", "viz-surface-2"),
        ("text", "viz-fill-1"), ("heading", "viz-fill-1"), ("link", "viz-fill-1"),
        ("text", "viz-fill-2"), ("heading", "viz-fill-2"), ("link", "viz-fill-2"),
        ("text", "viz-fill-3"), ("heading", "viz-fill-3"), ("link", "viz-fill-3"),
        ("text", "viz-paper"), ("heading", "viz-paper"), ("link", "viz-paper"),
        ("viz-node-text", "viz-node-bg"), ("viz-node-text", "good-edge"),
    ]
    ui_pairs = [
        ("border", "bg"), ("border", "surface"),
        ("focus-ring", "bg"), ("focus-ring", "surface"),
        ("nav-current", "header-bg"),
        ("mark-sky", "mark-bg"), ("mark-rose", "mark-bg"), ("mark-paper", "mark-bg"),
        ("mark-sky-2", "mark-bg"), ("mark-rose-2", "mark-bg"), ("mark-gold", "mark-bg"),
        ("viz-border", "bg"), ("viz-border", "viz-surface"), ("viz-border", "card-bg"), ("viz-border", "viz-paper"),
        ("viz-border", "viz-fill-1"), ("viz-border", "viz-fill-2"), ("viz-border", "viz-fill-3"),
        ("viz-accent", "bg"), ("viz-accent", "viz-surface"), ("viz-accent", "viz-track"),
        ("viz-accent", "viz-paper"), ("viz-accent", "viz-fill-1"),
        ("viz-accent-2", "bg"), ("viz-accent-2", "viz-surface-2"), ("viz-accent-2", "viz-track"),
        ("viz-accent-2", "viz-paper"), ("viz-accent-2", "viz-fill-2"),
        ("viz-accent-3", "bg"), ("viz-accent-3", "viz-surface"), ("viz-accent-3", "viz-track"),
        ("viz-accent-3", "viz-paper"), ("viz-accent-3", "viz-fill-3"),
    ]

    for theme_name, tokens in [("light", light), ("warm", warm), ("dark", dark), ("contrast", contrast)]:
        for fg, bg in text_pairs:
            r = contrast_ratio(tokens[fg], tokens[bg])
            assert r >= 7.0, f"{theme_name}: {fg} ({tokens[fg]}) on {bg} ({tokens[bg]}) = {r:.2f}:1 (< 7.0 AAA)"
        for fg, bg in ui_pairs:
            r = contrast_ratio(tokens[fg], tokens[bg])
            assert r >= 3.0, f"{theme_name}: UI/mark {fg} ({tokens[fg]}) on {bg} ({tokens[bg]}) = {r:.2f}:1 (< 3.0)"

    # Desk section flags: accents mark UI (>= 3:1), inks color eyebrow text (>= 7:1, AAA).
    auto_contrast_match = re.search(r'prefers-contrast: more\)\s*\{\s*html\[data-theme="auto"\]\s*\{(.*?)\}', css, re.S)
    assert auto_contrast_match
    auto_contrast = {**light, **extract_tokens(auto_contrast_match.group(1))}
    desks = ("site", "med", "id", "pub", "food", "legal")
    for theme_name, tokens in [("light", light), ("warm", warm), ("dark", dark), ("contrast", contrast),
                               ("auto-dark", auto_dark), ("auto-contrast", auto_contrast)]:
        for d in desks:
            for ground in ("bg", "surface", "header-bg", "card-bg"):
                r = contrast_ratio(tokens[f"{d}-accent"], tokens[ground])
                assert r >= 3.0, f"{theme_name}: --{d}-accent on {ground} = {r:.2f}:1 (< 3.0)"
                r = contrast_ratio(tokens[f"{d}-ink"], tokens[ground])
                assert r >= 7.0, f"{theme_name}: --{d}-ink on {ground} = {r:.2f}:1 (< 7.0 AAA)"
            if d != "site":
                for fg in ("text", "heading", "link"):
                    r = contrast_ratio(tokens[fg], tokens[f"{d}-tint"])
                    assert r >= 7.0, f"{theme_name}: {fg} on --{d}-tint = {r:.2f}:1 (< 7.0 AAA)"
                r = contrast_ratio(tokens[f"{d}-accent"], tokens[f"{d}-tint"])
                assert r >= 3.0, f"{theme_name}: --{d}-accent on --{d}-tint = {r:.2f}:1 (< 3.0)"
        # body.desk-id-records swaps the chart accents and fills; the swapped pairs must still pass.
        for fg, bg in (("id-accent", "id-tint"), ("med-accent", "med-tint"), ("id-accent", "viz-track"),
                       ("med-accent", "viz-track"), ("id-accent", "viz-paper"), ("med-accent", "viz-paper"),
                       ("id-accent", "viz-surface"), ("med-accent", "viz-surface-2")):
            r = contrast_ratio(tokens[fg], tokens[bg])
            assert r >= 3.0, f"{theme_name}: ID desk chart pair {fg} on {bg} = {r:.2f}:1 (< 3.0)"
    for k, v in dark.items():
        if k.startswith("press-"):
            continue
        assert auto_dark.get(k, "").upper() == v.upper(), f"auto-dark mismatch on --{k}"

    auto_dark_contrast_match = re.search(
        r'prefers-color-scheme: dark\)\s*and\s*\(prefers-contrast: more\)\s*\{\s*html\[data-theme="auto"\]\s*\{(.*?)\}',
        css,
        re.S,
    )
    assert auto_dark_contrast_match, "Missing dark + prefers-contrast: more token block"
    auto_dark_contrast = {**dark, **extract_tokens(auto_dark_contrast_match.group(1))}
    for fg, bg in text_pairs:
        r = contrast_ratio(auto_dark_contrast[fg], auto_dark_contrast[bg])
        assert r >= 7.0, f"auto-dark-contrast: {fg} on {bg} = {r:.2f}:1 (< 7.0 AAA)"
    for fg, bg in ui_pairs:
        r = contrast_ratio(auto_dark_contrast[fg], auto_dark_contrast[bg])
        assert r >= 3.0, f"auto-dark-contrast: {fg} on {bg} = {r:.2f}:1 (< 3.0)"

    assert len(re.findall(r"@media print\b", css)) == 1, "Expected one consolidated @media print block in site.css"
    print_match = re.search(r"@media print\s*\{(.*)\Z", css, re.S)
    assert print_match, "Missing @media print block"
    print_tokens = extract_tokens(print_match.group(1))
    for k in ("viz-surface", "viz-fill-1", "viz-fill-2", "viz-fill-3", "viz-paper"):
        assert print_tokens[k].upper() == "#FFFFFF"
    assert print_tokens["viz-border"].upper() == "#000000"
    assert print_tokens["viz-accent"].upper() == "#000000"
    assert print_tokens["rule"].upper() == "#000000"


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
