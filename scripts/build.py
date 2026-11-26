#!/usr/bin/env python3
"""Build CivicDesk (civicdesk.net) into public/.

Usage:
    python3 scripts/build.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
SITE_ROOT = ROOT / "site-root"
DEFAULT_OUT = ROOT / "public"

ALLOWED_STATUS = {"official", "not_official", "not_confirmed", "our_reading"}
VALID_VERIFIED_BY = {"human_reviewed_machine_assisted"}
VALID_READ_DEPTH = {"full", "partial", "searched", "not_read"}
VALID_READ_BY = {"human_machine_hybrid", "not_read"}

READ_DEPTH_LABELS = {
    "full": "Read in full",
    "partial": "Read in part",
    "searched": "Searched",
    "not_read": "Linked only (not relied on for facts)",
}
READ_BY_LABELS = {
    "human_machine_hybrid": "Human-machine hybrid",
    "not_read": "Not read",
}
STATUS_LABELS = {
    "official": ("badge-official", "Official"),
    "not_official": ("badge-not-official", "Not official"),
    "not_confirmed": ("badge-not-confirmed", "Not confirmed"),
    "our_reading": ("badge-our-reading", "Our reading"),
}

FONT_HASHES = {
    "AtkinsonHyperlegibleNext-Regular.woff2": "5cb240a4d99ab7e6fc3a4d4e6d11d89789fdffe9c86a8384000d662c53283f6f",
    "AtkinsonHyperlegibleNext-Bold.woff2": "0dbe07ed7679407db9d90f747d5ca3bd1b2bb7ccbcbe41c8246cfc6d2c7bc3bc",
    "OFL.txt": "474a034e99845062f661855caaa505880d0e9009deddc2727a92795eab374955",
}

CSP_META = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
)

BADGE_RE = re.compile(r'<span class="badge badge-(not-official|not-confirmed|our-reading)">')
CHECKED_P_RE = re.compile(r'(<p class="checked">.*?</p>)', re.S)
LEDE_P_RE = re.compile(r'(<p class="lede">.*?</p>)', re.S)
PURPOSE_P_RE = re.compile(r'(<p class="purpose">.*?</p>)', re.S)

_SRC_LINK_RE = r'<a class="src" [^>]+><span class="src-label">Source:</span>(?:&nbsp;|\s+)[^<]+</a>'
_STATUS_BADGE_RE = r'<span class="badge badge-(?:official|not-official|not-confirmed|our-reading)">[^<]+</span>'
_CITE_UNIT_RE = rf'(?:<span class="src-unit">{_SRC_LINK_RE}\s+{_STATUS_BADGE_RE}</span>|{_SRC_LINK_RE}|{_STATUS_BADGE_RE})'
_CITE_RUN_INNER = rf'{_CITE_UNIT_RE}(?:\s+{_CITE_UNIT_RE})*'
_BLOCK_END_TAIL = r'\s*</(?:p|li|td|dd)>|\s*</span>\s*</(?:li|p)>'
PAREN_CITE_RUN_RE = re.compile(
    rf'(?P<pre>[^\s(>])?\s*\(\s*(?P<run>{_CITE_RUN_INNER})\s*\)\.?(?={_BLOCK_END_TAIL})'
)
CITE_RUN_RE = re.compile(rf'({_CITE_RUN_INNER})(?={_BLOCK_END_TAIL})')
HEX_SHA_RE = re.compile(r'^[0-9a-fA-F]{7,40}$')


class BuildError(Exception):
    """Fatal error during site build."""


def resolve_commit_sha(
    env_vars: Mapping[str, str] | None = None,
    repo_root: Path = ROOT,
    run_git: bool = True,
) -> str:
    env = env_vars if env_vars is not None else os.environ
    for key in ("WORKERS_CI_COMMIT_SHA", "GITHUB_SHA", "CF_PAGES_COMMIT_SHA"):
        candidate = str(env.get(key) or "").strip()
        if HEX_SHA_RE.match(candidate):
            return candidate.lower()
    if run_git:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            git_sha = (proc.stdout or "").strip()
            if proc.returncode == 0 and HEX_SHA_RE.match(git_sha):
                return git_sha.lower()
        except (OSError, subprocess.SubprocessError):
            pass
    return "localdev"


def load_yaml(rel_path: str):
    with open(DATA / rel_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def as_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def human_date(value) -> str:
    d = as_date(value)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def status_badge(status: str) -> Markup:
    cls, label = STATUS_LABELS[status]
    return Markup(f'<span class="badge {cls}">{escape(label)}</span>')


def detect_body_badges(html: str) -> list[str]:
    main = html.split("<main", 1)[-1].split("</main>", 1)[0]
    body = main.split('<section class="page-sources"', 1)[0]
    return sorted(set(BADGE_RE.findall(body)))


def render_label_key(sources_labels_url: str) -> str:
    return (
        '<details class="label-key">\n'
        '  <summary>What our 4 source labels mean</summary>\n'
        '  <div class="label-key-body">\n'
        '    <dl class="label-key-list">\n'
        '      <dt><span class="badge badge-official">Official</span></dt>\n'
        '      <dd>Confirmed in a published government statute, administrative rule, court order, or agency form or guidance.</dd>\n'
        '      <dt><span class="badge badge-not-official">Not official</span></dt>\n'
        '      <dd>Reported by an external legal aid, advocacy, research, or bill-tracking source rather than published by the agency.</dd>\n'
        '      <dt><span class="badge badge-not-confirmed">Not confirmed</span></dt>\n'
        '      <dd>Mentioned in draft policy or pending guidance, or a detail we could not confirm in an official source.</dd>\n'
        '      <dt><span class="badge badge-our-reading">Our reading</span></dt>\n'
        '      <dd>CivicDesk\'s plain-language synthesis across multiple primary sources. Never legal advice.</dd>\n'
        '    </dl>\n'
        f'    <p class="label-key-more"><a href="{escape(sources_labels_url)}">See every source and label</a>.</p>\n'
        '  </div>\n'
        '</details>'
    )


def insert_label_key(html: str, sources_labels_url: str) -> str:
    key = render_label_key(sources_labels_url)
    for pattern in (LEDE_P_RE, PURPOSE_P_RE, CHECKED_P_RE):
        if pattern.search(html):
            return pattern.sub(r"\1\n" + key, html, count=1)
    return html


def _strip_paren_cite_run(match: re.Match[str]) -> str:
    pre = match.group("pre") or ""
    run = match.group("run")
    if not pre:
        return run
    if pre in ".!?:;":
        return f"{pre} {run}"
    return f"{pre}. {run}"


def group_citations(html: str) -> str:
    main_idx = html.find("<main")
    if main_idx == -1:
        return html
    main_end = html.find("</main>", main_idx)
    if main_end == -1:
        return html
    sources_idx = html.find('<section class="page-sources"', main_idx, main_end)
    body_end = sources_idx if sources_idx != -1 else main_end
    body = html[main_idx:body_end]
    body = PAREN_CITE_RUN_RE.sub(_strip_paren_cite_run, body)
    body = CITE_RUN_RE.sub(r'<span class="cite-row">\1</span>', body)
    return html[:main_idx] + body + html[body_end:]


def validate_registry(entries: list[dict], expected_prefix: str, expected_desk: str, errors: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    required = {"id", "desk", "title", "publisher", "url", "kind", "jurisdiction", "document_date", "checked", "status"}
    for s in entries:
        sid = str(s.get("id", ""))
        missing = required - set(s)
        if missing:
            errors.append(f"source {sid or '?'}: missing {sorted(missing)}")
            continue
        if not sid.startswith(expected_prefix):
            errors.append(f"source {sid}: must start with '{expected_prefix}'")
        if s["desk"] != expected_desk:
            errors.append(f"source {sid}: desk must be '{expected_desk}', got {s['desk']!r}")
        if sid in out:
            errors.append(f"duplicate source id: {sid}")
        if s["status"] not in ALLOWED_STATUS:
            errors.append(f"source {sid}: status {s['status']!r} not in {sorted(ALLOWED_STATUS)}")
        verified_by = s.get("verified_by", "human_reviewed_machine_assisted")
        if verified_by not in VALID_VERIFIED_BY:
            errors.append(f"source {sid}: invalid verified_by {verified_by!r}")
        s["verified_by"] = verified_by
        read_depth = s.get("read_depth")
        if read_depth not in VALID_READ_DEPTH:
            errors.append(f"source {sid}: invalid read_depth {read_depth!r}")
        read_by = s.get("read_by")
        if read_by not in VALID_READ_BY:
            errors.append(f"source {sid}: invalid read_by {read_by!r}")
        elif read_depth == "not_read" and read_by != "not_read":
            errors.append(f"source {sid}: read_depth='not_read' requires read_by='not_read'")
        elif read_depth != "not_read" and read_by != "human_machine_hybrid":
            errors.append(f"source {sid}: read source requires read_by='human_machine_hybrid'")
        as_date(s["checked"])
        out[sid] = s
    return out


def verify_fonts(errors: list[str]) -> bool:
    fonts_dir = STATIC / "fonts"
    if not fonts_dir.exists():
        return False
    all_present = True
    for fname, expected_sha in FONT_HASHES.items():
        fpath = fonts_dir / fname
        if not fpath.is_file():
            all_present = False
            continue
        digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if digest != expected_sha:
            errors.append(f"font checksum mismatch for {fname}: expected {expected_sha}, got {digest}")
    return all_present and (fonts_dir / "AtkinsonHyperlegibleNext-Regular.woff2").is_file()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory (default: public/)")
    args = parser.parse_args()
    out_dir = args.out if args.out.is_absolute() else (ROOT / args.out)

    errors: list[str] = []
    warnings: list[str] = []

    site = load_yaml("site.yaml")
    pages = load_yaml("pages.yaml")
    medicaid_list = load_yaml("sources/medicaid.yaml")
    id_records_list = load_yaml("sources/id_records.yaml")
    facts = load_yaml("facts.yaml")
    medicaid_languages = load_yaml("medicaid_languages.yaml")

    med_map = validate_registry(medicaid_list, "MED-", "medicaid", errors)
    id_map = validate_registry(id_records_list, "ID-", "id-records", errors)
    overlap = set(med_map) & set(id_map)
    if overlap:
        errors.append(f"overlapping source IDs across registries: {sorted(overlap)}")
    sources = {**med_map, **id_map}

    for fid, f in facts.items():
        for sid in f.get("sources", []):
            if sid not in sources:
                errors.append(f"fact {fid}: unknown source {sid}")

    fonts_available = verify_fonts(errors)

    pages_by_slug: dict[str, dict] = {}
    seen_routes: set[str] = set()
    for p in pages:
        for req in ("slug", "route", "template", "title", "description", "checked"):
            if req not in p:
                errors.append(f"page missing '{req}': {p}")
        slug = p["slug"]
        route = p["route"]
        if slug in pages_by_slug:
            errors.append(f"duplicate page slug: {slug}")
        if route in seen_routes:
            errors.append(f"duplicate page route: {route}")
        if slug != "404" and (not route.startswith("/") or not route.endswith("/")):
            errors.append(f"page {slug}: route '{route}' must start and end with '/'")
        pages_by_slug[slug] = p
        seen_routes.add(route)
        for sid in p.get("sources", []):
            if sid not in sources:
                errors.append(f"page {slug}: unknown declared source '{sid}'")

    if errors:
        print("BUILD VALIDATION FAILED", file=sys.stderr)
        for e in errors:
            print("  error:", e, file=sys.stderr)
        return 1

    env_vars = os.environ
    pages_url = (
        env_vars.get("SITE_URL")
        or site.get("public_url")
        or env_vars.get("CF_PAGES_URL")
        or ""
    )
    base_prefix = env_vars.get("BASE_PATH", "/").rstrip("/") + "/"
    if base_prefix == "//":
        base_prefix = "/"
    repo_url = (env_vars.get("REPO_URL") or site["repo_url"]).rstrip("/")
    issues_url = f"{repo_url}/issues"
    commit = resolve_commit_sha(env_vars, ROOT)
    commit_short = commit[:7] if commit != "localdev" else "localdev"
    commit_url = f"{repo_url}/commit/{commit}" if commit != "localdev" else ""
    build_time = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    def checked_range(items: list[dict]) -> dict[str, dt.date]:
        dates = [as_date(s["checked"]) for s in items if not s.get("docs_only")]
        if not dates:
            d = as_date(site["last_checked"])
            return {"oldest": d, "newest": d}
        return {"oldest": min(dates), "newest": max(dates)}

    state_checked = {
        "med_or": checked_range([s for s in medicaid_list if s["guide"] == "oregon"]),
        "med_wa": checked_range([s for s in medicaid_list if s["guide"] == "washington"]),
        "med_ut": checked_range([s for s in medicaid_list if s["guide"] == "utah"]),
        "id_or": checked_range([s for s in id_records_list if s["jurisdiction"] == "OR"]),
        "id_wa": checked_range([s for s in id_records_list if s["jurisdiction"] == "WA"]),
        "id_ut": checked_range([s for s in id_records_list if s["jurisdiction"] == "UT"]),
        "id_us": checked_range([s for s in id_records_list if s["jurisdiction"] == "US"]),
    }

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["human_date"] = human_date

    state = {"used": [], "facts": [], "page": None}
    asset_hashes: dict[str, str] = {}

    def use(sid: str) -> dict:
        if sid not in sources:
            raise BuildError(f"page {state['page']}: unknown source id '{sid}'")
        if sid not in state["used"]:
            state["used"].append(sid)
        return sources[sid]

    def src(sid: str, label: str | None = None) -> Markup:
        s = use(sid)
        short = label or s.get("short") or s["title"]
        link = (
            f'<a class="src" href="{escape(s["url"])}" rel="noreferrer">'
            f'<span class="src-label">Source:</span>&nbsp;{escape(short)}</a>'
        )
        if s["status"] != "official":
            b = status_badge(s["status"])
            return Markup(f'<span class="src-unit">{link} {b}</span>')
        return Markup(link)

    def fact(fid: str) -> Markup:
        if fid not in facts:
            raise BuildError(f"page {state['page']}: unknown fact id '{fid}'")
        if fid not in state["facts"]:
            state["facts"].append(fid)
        for sid in facts[fid].get("sources", []):
            use(sid)
        value = escape(str(facts[fid]["value"]))
        if facts[fid].get("reading"):
            value += Markup(' <span class="badge badge-our-reading">Our reading</span>')
        return Markup(value)

    def fact_src(fid: str) -> Markup:
        value = fact(fid)
        chips = " ".join(str(src(sid)) for sid in facts[fid].get("sources", []))
        return Markup(f"{value} {chips}")

    def ext(sid: str, text: str | None = None) -> Markup:
        s = use(sid)
        label = text or s.get("link_text") or s["title"]
        kind = "official site" if s.get("official", True) else "outside site"
        return Markup(
            f'<a class="ext" href="{escape(s["url"])}" rel="noreferrer">{escape(label)}'
            f'<span class="vh"> ({kind})</span></a>'
        )

    def root_prefix(_page: dict | None = None) -> str:
        return base_prefix

    def page_url(slug_and_anchor: str) -> str:
        name, sep, anchor = slug_and_anchor.partition("#")
        if name not in pages_by_slug:
            raise BuildError(f"page {state['page']}: link to unknown page '{slug_and_anchor}'")
        route = pages_by_slug[name]["route"]
        url = base_prefix.rstrip("/") + route
        return f"{url}{sep}{anchor}" if sep else url

    def asset(rel: str) -> str:
        fpath = STATIC / rel
        if not fpath.is_file():
            raise BuildError(f"page {state['page']}: missing static asset '{rel}'")
        digest = asset_hashes.get(rel)
        if digest is None:
            digest = hashlib.sha256(fpath.read_bytes()).hexdigest()[:8]
            asset_hashes[rel] = digest
        return f"{base_prefix}static/{rel}?v={digest}"

    def correction_mailto(page: dict) -> str:
        route = str(page.get("route") or "/")
        subject = f"CivicDesk correction: {route}"
        return f"mailto:{site['corrections_email']}?subject={quote(subject, safe='')}"

    def header_tagline(page: dict) -> str:
        desk = page.get("desk", "")
        st = page.get("state", "")
        if desk == "medicaid":
            names = {
                "oregon": "Oregon Health Plan (OHP) 2027 guide",
                "washington": "Washington Apple Health 2027 guide",
                "utah": "Utah Medicaid 2027 guide",
            }
            return names.get(st, "2027 Medicaid work and activity rules")
        if desk == "id-records":
            names = {
                "oregon": "Oregon name, ID, and vital records",
                "washington": "Washington name, ID, and vital records",
                "utah": "Utah name, ID, and vital records",
            }
            return names.get(st, "Legal name, ID, and vital records")
        return site["tagline"]

    def switcher_label(page: dict) -> str:
        st = page.get("state", "")
        if st:
            return f"State: {st.capitalize()}"
        desk = page.get("desk", "")
        if desk == "medicaid":
            return "Medicaid states"
        if desk == "id-records":
            return "ID & records states"
        return "Jump to a guide"

    def switcher_aria(page: dict) -> str:
        return f"{switcher_label(page)}. Switch state or desk guide"

    def switcher_items(page: dict) -> list[dict]:
        desk = page.get("desk", "")
        if desk == "medicaid":
            return [
                {"slug": "medicaid-oregon", "name": "Oregon", "guide": "Oregon Health Plan (OHP) 2027 rules", "sep": False},
                {"slug": "medicaid-washington", "name": "Washington", "guide": "Washington Apple Health 2027 rules", "sep": False},
                {"slug": "medicaid-utah", "name": "Utah", "guide": "Utah Medicaid 2027 rules", "sep": False},
                {"slug": "medicaid", "name": "Medicaid desk", "guide": "All states & federal overview", "sep": True},
                {"slug": "id-records", "name": "ID & Vital Records desk", "guide": "Marriage, divorce, court, and ID updates", "sep": False},
                {"slug": "start", "name": "Start here", "guide": "2-question router & full guide finder", "sep": False},
            ]
        if desk == "id-records":
            return [
                {"slug": "id-records-oregon", "name": "Oregon", "guide": "Oregon court, birth record, and DMV", "sep": False},
                {"slug": "id-records-washington", "name": "Washington", "guide": "Washington court, birth record, and DOL", "sep": False},
                {"slug": "id-records-utah", "name": "Utah", "guide": "Utah court, Vital Records, and DLD", "sep": False},
                {"slug": "id-records-federal", "name": "Federal records", "guide": "Passports, Social Security & immigration", "sep": False},
                {"slug": "id-records-path", "name": "Find your path", "guide": "Personalized step-by-step path finder", "sep": True},
                {"slug": "medicaid", "name": "2027 Medicaid desk", "guide": "OHP, Apple Health, and Utah Medicaid", "sep": False},
                {"slug": "start", "name": "Start here", "guide": "2-question router & full guide finder", "sep": False},
            ]
        return [
            {"slug": "start", "name": "Start here", "guide": "2-question router & full guide finder", "sep": False},
            {"slug": "medicaid", "name": "2027 Medicaid overview", "guide": "Federal & 3-state summary", "sep": True},
            {"slug": "medicaid-oregon", "name": "Oregon Medicaid (OHP)", "guide": "Rules, exemptions & 1-minute check", "sep": False},
            {"slug": "medicaid-washington", "name": "Washington Apple Health", "guide": "Rules, exemptions & 1-minute check", "sep": False},
            {"slug": "medicaid-utah", "name": "Utah Medicaid", "guide": "Rules, exclusions & 1-minute check", "sep": False},
            {"slug": "id-records-path", "name": "ID & records path finder", "guide": "Step-by-step custom checklist", "sep": True},
            {"slug": "id-records-oregon", "name": "Oregon ID & vital records", "guide": "Marriage, divorce, court, OHA & DMV", "sep": False},
            {"slug": "id-records-washington", "name": "Washington ID & vital records", "guide": "Marriage, divorce, court, DOH & DOL", "sep": False},
            {"slug": "id-records-utah", "name": "Utah ID & vital records", "guide": "Marriage, divorce, court, Vital & DLD", "sep": False},
            {"slug": "id-records-federal", "name": "Federal IDs", "guide": "Passports, Social Security & immigration", "sep": False},
        ]

    def nav_items(page: dict) -> list[tuple[str, str]]:
        desk = page.get("desk", "")
        st = page.get("state", "")
        if desk == "medicaid":
            if st in {"oregon", "washington", "utah"}:
                return [
                    ("index", "All desks"),
                    (f"medicaid-{st}", f"{st.capitalize()} home"),
                    (f"medicaid-{st}-check", "1-minute check"),
                    (f"medicaid-{st}-rules", "Rules"),
                    (f"medicaid-{st}-reasons", "Exemptions"),
                    (f"medicaid-{st}-apply", "Apply"),
                    (f"medicaid-{st}-help", "Get help"),
                ]
            return [
                ("index", "All desks"),
                ("medicaid", "Medicaid overview"),
                ("medicaid-oregon", "Oregon"),
                ("medicaid-washington", "Washington"),
                ("medicaid-utah", "Utah"),
                ("medicaid-sources", "Sources"),
            ]
        if desk == "id-records":
            if st in {"oregon", "washington", "utah"}:
                return [
                    ("index", "All desks"),
                    ("id-records", "ID desk"),
                    (f"id-records-{st}", st.capitalize()),
                    (f"id-records-{st}-court", "Court order"),
                    (f"id-records-{st}-birth", "Birth certificate"),
                    (f"id-records-{st}-id", "License & ID"),
                ]
            return [
                ("index", "All desks"),
                ("id-records", "Name & ID home"),
                ("id-records-path", "Find your path"),
                ("id-records-federal", "Federal"),
                ("id-records-after", "After court"),
                ("id-records-help", "Get help"),
            ]
        return [
            ("index", "Home"),
            ("start", "Start here"),
            ("medicaid", "2027 Medicaid"),
            ("id-records", "Name, ID & vital records"),
            ("sources", "Sources"),
        ]

    def footer_items(page: dict) -> list[tuple[str, str]]:
        desk = page.get("desk", "")
        st = page.get("state", "")
        base_links = [
            ("index", "CivicDesk home"),
            ("start", "Start here"),
            ("medicaid", "2027 Medicaid desk"),
            ("id-records", "Name, ID & Vital Records desk"),
        ]
        if desk == "medicaid" and st in {"oregon", "washington", "utah"}:
            state_links = [
                (f"medicaid-{st}-letter", "Got a letter"),
                (f"medicaid-{st}-denied", "Turned down"),
                (f"medicaid-{st}-bills", "Past medical bills"),
                (f"medicaid-{st}-health", "Health conditions"),
                (f"medicaid-{st}-immigration", "Immigration status"),
                (f"medicaid-{st}-words", "Words on letters"),
                (f"medicaid-{st}-log", "Hours log"),
                ("medicaid-sources", "Medicaid sources"),
            ]
        elif desk == "id-records":
            state_links = [
                ("id-records-path", "ID path finder"),
                ("id-records-oregon", "Oregon ID guide"),
                ("id-records-washington", "Washington ID guide"),
                ("id-records-utah", "Utah ID guide"),
                ("id-records-federal", "Passports & Social Security"),
                ("id-records-after", "Record-update checklist"),
                ("id-records-compare", "Compare states"),
                ("id-records-glossary", "Words to know"),
                ("id-records-sources", "ID sources"),
            ]
        else:
            state_links = [
                ("medicaid-oregon", "Oregon Medicaid"),
                ("medicaid-washington", "Washington Medicaid"),
                ("medicaid-utah", "Utah Medicaid"),
                ("id-records-path", "ID path finder"),
            ]
        tail_links = [
            ("sources", "All sources"),
            ("about", "About"),
            ("accessibility", "Accessibility"),
            ("privacy", "Privacy"),
        ]
        return base_links + state_links + tail_links

    env.globals.update(
        site=site,
        pages=pages,
        sources=sources,
        medicaid_sources=medicaid_list,
        id_records_sources=id_records_list,
        facts=facts,
        medicaid_languages=medicaid_languages,
        state_checked=state_checked,
        read_depth_labels=READ_DEPTH_LABELS,
        read_by_labels=READ_BY_LABELS,
        status_badge=status_badge,
        src=src,
        fact=fact,
        fact_src=fact_src,
        ext=ext,
        page_url=page_url,
        asset=asset,
        root_prefix=root_prefix,
        header_tagline=header_tagline,
        switcher_label=switcher_label,
        switcher_aria=switcher_aria,
        switcher_items=switcher_items,
        nav_items=nav_items,
        footer_items=footer_items,
        repo_url=repo_url,
        issues_url=issues_url,
        correction_mailto=correction_mailto,
        public_base_url=(pages_url.rstrip("/") + "/") if pages_url else "",
        fonts_available=fonts_available,
        csp_meta=CSP_META,
        build_commit=commit,
        build_commit_short=commit_short,
        build_commit_url=commit_url,
        build_time=build_time,
        used_sources=lambda: [sources[i] for i in state["used"]],
    )

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    all_public_dates = [as_date(s["checked"]) for s in sources.values() if not s.get("docs_only")]
    med_public_dates = [as_date(s["checked"]) for s in medicaid_list if not s.get("docs_only")]
    id_public_dates = [as_date(s["checked"]) for s in id_records_list if not s.get("docs_only")]

    page_reports: list[dict] = []
    newest_checked_by_slug: dict[str, dt.date] = {}

    for p in pages:
        slug = p["slug"]
        declared = list(p.get("sources", []))
        state["used"] = list(declared)
        state["facts"] = []
        state["page"] = slug

        try:
            tpl = env.get_template(p["template"])
            page_date = as_date(p["checked"])
            html = tpl.render(page={**p, "checked": page_date, "oldest_checked": page_date, "newest_checked": page_date})

            if slug == "sources":
                cited_dates = list(all_public_dates)
            elif slug == "medicaid-sources":
                cited_dates = list(med_public_dates)
            elif slug == "id-records-sources":
                cited_dates = list(id_public_dates)
            else:
                cited_dates = [as_date(sources[sid]["checked"]) for sid in state["used"]]

            if cited_dates:
                oldest_date = min(cited_dates)
                newest_date = max(cited_dates)
            else:
                oldest_date = newest_date = page_date

            if oldest_date != page_date or newest_date != page_date:
                state["used"] = list(declared)
                state["facts"] = []
                html = tpl.render(page={**p, "checked": oldest_date, "oldest_checked": oldest_date, "newest_checked": newest_date})
        except BuildError as exc:
            errors.append(str(exc))
            continue

        newest_checked_by_slug[slug] = newest_date
        body_badges = detect_body_badges(html)
        if slug not in {"sources", "medicaid-sources", "id-records-sources", "about", "404"}:
            html = group_citations(html)
        if body_badges and slug not in {"sources", "medicaid-sources", "id-records-sources", "about", "404"}:
            html = insert_label_key(html, page_url("sources#labels"))

        route = p["route"]
        if slug == "404":
            dest_file = out_dir / "404.html"
        elif route == "/":
            dest_file = out_dir / "index.html"
        else:
            dest_file = out_dir / route.strip("/") / "index.html"
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(html, encoding="utf-8")

        page_reports.append({
            "slug": slug,
            "route": route,
            "desk": p.get("desk"),
            "sources": list(state["used"]),
            "facts": list(state["facts"]),
            "badges": body_badges,
            "checked": oldest_date.isoformat(),
            "oldest_checked": oldest_date.isoformat(),
            "newest_checked": newest_date.isoformat(),
        })

    if errors:
        print("BUILD FAILED", file=sys.stderr)
        for e in errors:
            print("  error:", e, file=sys.stderr)
        return 1

    shutil.copytree(STATIC, out_dir / "static", dirs_exist_ok=True)
    if SITE_ROOT.exists():
        shutil.copytree(SITE_ROOT, out_dir, dirs_exist_ok=True)

    today = dt.date.today()
    max_age = int(site.get("max_source_age_days", 120))
    for sid, s in sources.items():
        age = (today - as_date(s["checked"])).days
        if age > max_age:
            warnings.append(f"source {sid} last checked {age} days ago (limit {max_age})")

    public_sources = [
        {k: v for k, v in s.items() if k != "internal_note"}
        for s in (medicaid_list + id_records_list)
        if not s.get("docs_only")
    ]
    (out_dir / "sources.json").write_text(json.dumps(public_sources, indent=2), encoding="utf-8")
    (out_dir / "facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")

    robots_txt = "User-agent: *\nAllow: /\n"
    if pages_url:
        base = pages_url.rstrip("/") + "/"
        robots_txt += f"\nSitemap: {base}sitemap.xml\n"
        urls = "\n".join(
            f"  <url><loc>{base}{p['route'].lstrip('/')}</loc><lastmod>{newest_checked_by_slug[p['slug']].isoformat()}</lastmod></url>"
            for p in pages
            if p["slug"] != "404"
        )
        (out_dir / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{urls}\n</urlset>\n",
            encoding="utf-8",
        )
    (out_dir / "robots.txt").write_text(robots_txt, encoding="utf-8")

    report = {
        "version": site.get("version"),
        "built_at": build_time,
        "commit": commit,
        "commit_short": commit_short,
        "commit_url": commit_url or None,
        "public_url": pages_url or None,
        "repo_url": repo_url,
        "oldest_checked": min(all_public_dates).isoformat(),
        "latest_checked": max(all_public_dates).isoformat(),
        "provenance_standard": "human_reviewed_machine_assisted",
        "read_by_model": "human_machine_hybrid",
        "medicaid_source_count": len(med_map),
        "id_records_source_count": len(id_map),
        "total_source_count": len(sources),
        "fact_count": len(facts),
        "page_count": len(page_reports),
        "fonts_bundled": fonts_available,
        "warnings": warnings,
        "pages": page_reports,
    }
    (out_dir / "build-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        f"Built {len(page_reports)} pages ({len(med_map)} MED-* sources, {len(id_map)} ID-* sources, {len(facts)} facts) -> {out_dir}"
    )
    for w in warnings:
        print("  warning:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
