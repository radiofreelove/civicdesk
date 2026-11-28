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

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from civicdesk_lib.brand import (  # noqa: E402
    DEFAULT_BRAND,
    FONT_HASHES,
    REQUIRED_FONTS,
    VALID_BRAND_MODES,
    validate_brand_config,
    validate_svg_asset,
    verify_fonts,
)
from civicdesk_lib.html_utils import (  # noqa: E402
    BADGE_RE,
    CHECKED_OLDEST_TEXT_MARK,
    CHECKED_P_RE,
    CHECKED_TEXT_MARK,
    CHECKED_TIME_MARK,
    CONTENT_OPEN,
    HEAD_BLOCK_CLASSES,
    HEAD_P_CLASSES,
    INLINE_CITE_RE,
    LEDE_P_RE,
    NO_TAB_PAGES,
    PURPOSE_P_RE,
    SOURCE_PANEL_MARK,
    SRC_MARK_RE,
    WHY_P_RE,
    _ContentText,
    _HASH_SKIP_CLASSES,
    _HASH_VOID,
    apply_sources_layer,
    bind_trailing_punctuation,
    content_hash,
    detect_body_badges,
    insert_label_key,
    outline_sections,
    render_label_key,
    strip_inline_citations,
)
from civicdesk_lib.paths import (  # noqa: E402
    ART_NAME_RE,
    DATA,
    DEFAULT_OUT,
    ROOT,
    SITE_ROOT,
    STATIC,
    TEMPLATES,
    built_html_path,
    discover_art_dims,
    webp_dimensions,
)

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
    "human_machine_hybrid": "Read by a person, with software help",
    "not_read": "Not read",
}

STATUS_LABELS = {
    "official": ("badge-official", "Official"),
    "not_official": ("badge-not-official", "Not official"),
    "not_confirmed": ("badge-not-confirmed", "Not confirmed"),
    "our_reading": ("badge-our-reading", "Our reading"),
}

CSP_META = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
)

HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SPEC_KEYS = ("cost", "clock", "paperwork", "catch")
ANALOGY_PLACEMENTS = {"lead", "inline"}
ANALOGY_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ABBREV_RE = re.compile(
    r"\b(?:[A-Za-z]\.){2,}|\b(?:No|Nos|Mr|Ms|Mrs|Dr|St|Inc|Co|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
    r"|Ch|Sec|Subsec|Art|Stat|Fed|Reg|Admin|Dept|Div|Gov|Sen|Rep|Mass|Wash|Ore|Or|Wn|App|Rev|Ann|vs|v|etc|approx|p|pp|para|ed)\."
)
_SENTENCE_BREAK_RE = re.compile(r"[.!?][\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")

LASTMOD_PATH = DATA / "lastmod.json"
CI_ENV_KEYS = ("CI", "GITHUB_ACTIONS", "WORKERS_CI", "WORKERS_CI_COMMIT_SHA", "WORKERS_CI_BRANCH", "CF_PAGES")
VALID_DESK_STATUS = {"active", "upcoming"}


class BuildError(Exception):
    """Fatal error during site build."""


def read_note(s: dict) -> str:
    """Reader-facing line for how closely a source was read."""
    depth = READ_DEPTH_LABELS[s["read_depth"]]
    if s["read_by"] == "not_read":
        return depth
    return f"{depth} by a person, with software help"


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


def is_one_sentence(text: str) -> bool:
    masked = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", "_"), text.strip())
    return bool(re.search(r"[.!?][\"')\]]*$", masked)) and not _SENTENCE_BREAK_RE.search(masked)


def validate_page_voice(p: dict, sources: Mapping, facts: Mapping, errors: list[str]) -> None:
    slug = p.get("slug")
    for key in ("answer", "why"):
        if key in p and (not isinstance(p[key], str) or not p[key].strip()):
            errors.append(f"page {slug}: '{key}' must be a non-empty string")

    spec = p.get("spec")
    if spec is not None:
        if not isinstance(spec, dict):
            errors.append(f"page {slug}: 'spec' must be a mapping of {', '.join(SPEC_KEYS)}")
        else:
            for key in spec:
                if key not in SPEC_KEYS:
                    errors.append(f"page {slug}: unknown spec key '{key}' (allowed: {', '.join(SPEC_KEYS)})")
            for key in ("cost", "clock"):
                val = spec.get(key)
                if val is not None and (not isinstance(val, str) or val not in facts):
                    errors.append(f"page {slug}: spec.{key} '{val}' is not a data/facts.yaml key")
            paperwork = spec.get("paperwork")
            if paperwork is not None and (not isinstance(paperwork, str) or paperwork not in sources):
                errors.append(f"page {slug}: spec.paperwork '{paperwork}' is not a source id")
            catch = spec.get("catch")
            if catch is not None:
                if not isinstance(catch, dict) or not isinstance(catch.get("text"), str) or not catch["text"].strip():
                    errors.append(f"page {slug}: spec.catch needs 'text' and 'basis'")
                else:
                    basis = catch.get("basis")
                    if not isinstance(basis, list) or not basis:
                        errors.append(f"page {slug}: spec.catch needs at least one basis source id")
                    else:
                        for sid in basis:
                            if not isinstance(sid, str) or sid not in sources:
                                errors.append(f"page {slug}: spec.catch basis '{sid}' is not a source id")

    analogies = p.get("analogies")
    if analogies is None:
        return
    if not isinstance(analogies, list):
        errors.append(f"page {slug}: 'analogies' must be a list")
        return
    seen: set[str] = set()
    leads = 0
    for a in analogies:
        if not isinstance(a, dict):
            errors.append(f"page {slug}: each analogy must be a mapping")
            continue
        aid = str(a.get("id") or "")
        label = f"page {slug}: analogy '{aid}'"
        if not ANALOGY_ID_RE.fullmatch(aid):
            errors.append(f"page {slug}: analogy id '{aid}' must be lowercase-kebab-case")
        if aid in seen:
            errors.append(f"{label} is duplicated")
        seen.add(aid)
        if a.get("placement") not in ANALOGY_PLACEMENTS:
            errors.append(f"{label}: placement must be one of {sorted(ANALOGY_PLACEMENTS)}")
        leads += a.get("placement") == "lead"
        if not str(a.get("compare") or "").strip():
            errors.append(f"{label}: 'compare' (how the system works) is required")
        boundary = str(a.get("boundary") or "").strip()
        if not boundary:
            errors.append(f"{label}: 'boundary' (where the comparison stops) is required")
        elif not is_one_sentence(boundary):
            errors.append(f"{label}: 'boundary' must be exactly one sentence")
        basis = a.get("basis")
        if not isinstance(basis, list) or not basis:
            errors.append(f"{label}: 'basis' needs at least one MED-* or ID-* source id")
        else:
            for sid in basis:
                if not isinstance(sid, str) or sid not in sources:
                    errors.append(f"{label}: basis '{sid}' is not in data/sources/*.yaml")
        unknown = set(a) - {"id", "placement", "compare", "boundary", "basis"}
        if unknown:
            errors.append(f"{label}: unknown keys {sorted(unknown)}")
    if leads > 1:
        errors.append(f"page {slug}: at most one analogy can have placement 'lead'")


def analogy_for(page: Mapping, aid: str | None = None) -> dict | None:
    """The page's lead analogy, or the inline one named by id (templates/macros.html analogy)."""
    items = page.get("analogies") or []
    if aid is None:
        return next((a for a in items if a.get("placement") == "lead"), None)
    found = next((a for a in items if a.get("id") == aid), None)
    if found is None:
        raise BuildError(f"page {page.get('slug')}: no analogy with id '{aid}' in data/pages.yaml")
    return found


def load_lastmod(path: Path = LASTMOD_PATH) -> dict[str, dict]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("hash") and v.get("date")}


def dump_lastmod(entries: Mapping[str, dict]) -> str:
    rows = [f"  {json.dumps(slug)}: {json.dumps({'date': e['date'], 'hash': e['hash']})}" for slug, e in sorted(entries.items())]
    return "{\n" + ",\n".join(rows) + "\n}\n"


def head_commit_date(root: Path = ROOT) -> str | None:
    try:
        proc = subprocess.run(["git", "log", "-1", "--format=%cs"], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", out) else None


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
        if not str(s["url"]).startswith("https://"):
            errors.append(f"source {sid}: url must start with https://, got {s['url']!r}")
        if not str(s["kind"]).strip():
            errors.append(f"source {sid}: kind must be non-empty")
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
        try:
            as_date(s["checked"])
        except ValueError:
            errors.append(f"source {sid}: invalid checked date {s['checked']!r}")
        if s.get("review_by"):
            try:
                as_date(s["review_by"])
            except ValueError:
                errors.append(f"source {sid}: invalid review_by date {s['review_by']!r}")
        out[sid] = s
    return out


def validate_desks(site: dict, page_slugs: set[str], errors: list[str]) -> list[dict]:
    """Check the desk registry in site.yaml. Active desks must point at real pages and registries."""
    desks = site.get("desks") or []
    seen: set[str] = set()
    for d in desks:
        slug = str(d.get("slug", ""))
        label = f"site.yaml desk {slug or '?'}"
        for key in ("slug", "status", "name", "kicker", "blurb"):
            if not d.get(key):
                errors.append(f"{label}: missing '{key}'")
        if slug in seen:
            errors.append(f"{label}: duplicate slug")
        seen.add(slug)
        if d.get("status") not in VALID_DESK_STATUS:
            errors.append(f"{label}: status must be one of {sorted(VALID_DESK_STATUS)}")
        if d.get("status") == "active":
            pages_needed = [d.get("hub"), d.get("sources_page")] + [st.get("page") for st in d.get("states", [])]
            if d.get("tool"):
                pages_needed.append(d["tool"].get("page"))
            if d.get("switcher_hub") and d["switcher_hub"].get("page"):
                pages_needed.append(d["switcher_hub"]["page"])
            for ps in pages_needed:
                if ps and ps not in page_slugs:
                    errors.append(f"{label}: unknown page slug {ps!r}")
            for key in ("registry", "source_prefix", "sources_page", "art", "states", "source_groups"):
                if not d.get(key):
                    errors.append(f"{label}: active desk needs '{key}'")
            reg = str(d.get("registry") or "")
            if reg and not (DATA / reg).is_file():
                errors.append(f"{label}: registry file 'data/{reg}' not found")
    if not any(d.get("status") == "active" for d in desks):
        errors.append("site.yaml: desks must list at least one active desk")
    return desks


def _format_checked_time(oldest: dt.date, newest: dt.date) -> str:
    if oldest == newest:
        return f'<time datetime="{oldest.isoformat()}">{human_date(oldest)}</time>'
    return (
        f'<time datetime="{oldest.isoformat()}">{human_date(oldest)}</time>'
        f' to <time datetime="{newest.isoformat()}">{human_date(newest)}</time>'
    )


def _format_checked_text(oldest: dt.date, newest: dt.date) -> str:
    if oldest == newest:
        return human_date(oldest)
    return f"{human_date(oldest)} to {human_date(newest)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory (default: public/)")
    parser.add_argument("--site-yaml", type=Path, default=None, help="Optional override path to site.yaml")
    parser.add_argument("--static-dir", type=Path, default=STATIC, help="Static assets directory (default: static/)")
    args = parser.parse_args(argv)
    out_dir = args.out if args.out.is_absolute() else (ROOT / args.out)
    static_dir = args.static_dir if args.static_dir.is_absolute() else (ROOT / args.static_dir)

    errors: list[str] = []
    warnings: list[str] = []

    if args.site_yaml is not None:
        site_yaml_path = args.site_yaml if args.site_yaml.is_absolute() else (ROOT / args.site_yaml)
        site = yaml.safe_load(site_yaml_path.read_text(encoding="utf-8"))
    else:
        site = load_yaml("site.yaml")
    pages = load_yaml("pages.yaml")
    facts = load_yaml("facts.yaml")
    medicaid_languages = load_yaml("medicaid_languages.yaml")

    active_desks = [d for d in (site.get("desks") or []) if isinstance(d, dict) and d.get("status") == "active"]
    desks_by_slug = {d["slug"]: d for d in active_desks}

    sources_by_desk: dict[str, list[dict]] = {}
    desk_maps: dict[str, dict[str, dict]] = {}
    sources: dict[str, dict] = {}
    all_sources_list: list[dict] = []
    for d in active_desks:
        dslug = d["slug"]
        reg_rel = str(d.get("registry") or "")
        if not reg_rel or not (DATA / reg_rel).is_file():
            errors.append(f"site.yaml desk {dslug}: missing registry file 'data/{reg_rel}'")
            sources_by_desk[dslug] = []
            desk_maps[dslug] = {}
            continue
        entries = load_yaml(reg_rel) or []
        dmap = validate_registry(entries, str(d.get("source_prefix") or ""), dslug, errors)
        overlap = set(sources) & set(dmap)
        if overlap:
            errors.append(f"overlapping source IDs across registries ({dslug}): {sorted(overlap)}")
        sources_by_desk[dslug] = entries
        desk_maps[dslug] = dmap
        sources.update(dmap)
        all_sources_list.extend(entries)

    for fid, f in facts.items():
        if f.get("review_by"):
            try:
                as_date(f["review_by"])
            except ValueError:
                errors.append(f"fact {fid}: invalid review_by date {f['review_by']!r}")
        for sid in f.get("sources", []):
            if sid not in sources:
                errors.append(f"fact {fid}: unknown source {sid}")

    fonts_available = verify_fonts(errors, static_dir / "fonts")
    validate_brand_config(site, static_dir, errors)
    art_dims = discover_art_dims(static_dir / "img" / "art")

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
        if p.get("desk") and p["desk"] not in desks_by_slug:
            errors.append(f"page {slug}: unknown desk '{p['desk']}' (active: {sorted(desks_by_slug)})")
        if p.get("source_registry") and p["source_registry"] not in ({"all"} | set(desks_by_slug)):
            errors.append(f"page {slug}: invalid source_registry '{p['source_registry']}'")
        tpl_path = TEMPLATES / str(p.get("template") or "")
        if not tpl_path.is_file():
            errors.append(f"page {slug}: template '{p.get('template')}' not found")
        for sname in p.get("scripts") or []:
            if not (static_dir / "js" / f"{sname}.js").is_file():
                errors.append(f"page {slug}: script 'static/js/{sname}.js' not found")
        try:
            as_date(p["checked"])
        except ValueError:
            errors.append(f"page {slug}: invalid checked date {p['checked']!r}")
        pages_by_slug[slug] = p
        seen_routes.add(route)
        for sid in p.get("sources", []):
            if sid not in sources:
                errors.append(f"page {slug}: unknown declared source '{sid}'")
        validate_page_voice(p, sources, facts, errors)
        art = str(p.get("art") or "")
        if not art:
            errors.append(f"page {slug}: missing 'art' (a static/img/art/print-*.webp file)")
        elif not ART_NAME_RE.fullmatch(art) or art not in art_dims:
            errors.append(f"page {slug}: art '{art}' is not an existing static/img/art/print-*.webp file")

    validate_desks(site, set(pages_by_slug), errors)

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
    ci_branch = str(env_vars.get("WORKERS_CI_BRANCH") or "").strip()
    noindex_build = env_vars.get("CIVICDESK_NOINDEX") == "1" or (ci_branch not in ("", "main"))
    commit_url = f"{repo_url}/commit/{commit}" if commit != "localdev" else ""
    build_time = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    def checked_range(items: list[dict]) -> dict[str, dt.date]:
        dates = [as_date(s["checked"]) for s in items if not s.get("docs_only")]
        if not dates:
            d = as_date(site["last_checked"])
            return {"oldest": d, "newest": d}
        return {"oldest": min(dates), "newest": max(dates)}

    state_checked: dict[str, dict[str, dt.date]] = {}
    for d in active_desks:
        d_entries = sources_by_desk[d["slug"]]
        for st_cfg in d.get("states", []):
            code = str(st_cfg.get("code") or "")
            rng = checked_range([s for s in d_entries if s.get("jurisdiction") == code])
            if st_cfg.get("checked_key"):
                state_checked[st_cfg["checked_key"]] = rng
            state_checked[f"{d['slug']}:{st_cfg.get('slug', code.lower())}"] = rng

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

    def mark(sid: str) -> str:
        return f"<!--cd-src:{sid}-->"

    def src(sid: str, label: str | None = None) -> Markup:
        s = use(sid)
        short = label or s.get("short") or s["title"]
        link = (
            f'<a class="src" href="{escape(s["url"])}" rel="noreferrer">'
            f'<span class="src-label">Source:</span>&nbsp;{escape(short)}</a>'
        )
        if s["status"] != "official":
            b = status_badge(s["status"])
            return Markup(f'{mark(sid)}<span class="src-unit">{link} {b}</span>')
        return Markup(mark(sid) + link)

    def basis(*sids: str) -> Markup:
        """Rule citation for interactive tool result blocks across any active desk registry."""
        if not sids:
            raise BuildError(f"page {state['page']}: basis() needs at least one source id")
        labels = []
        for sid in sids:
            s = use(sid)
            labels.append(str(s.get("short") or s["title"]))
        return Markup(
            f' data-basis="{escape(" ".join(sids))}" data-basis-label="{escape("; ".join(labels))}"'
        )

    def fact(fid: str) -> Markup:
        if fid not in facts:
            raise BuildError(f"page {state['page']}: unknown fact id '{fid}'")
        if fid not in state["facts"]:
            state["facts"].append(fid)
        marks = ""
        for sid in facts[fid].get("sources", []):
            use(sid)
            marks += mark(sid)
        value = escape(str(facts[fid]["value"]))
        if facts[fid].get("reading"):
            value += Markup(' <span class="badge badge-our-reading">Our reading</span>')
        return Markup(marks) + Markup(value)

    def fact_src(fid: str) -> Markup:
        value = fact(fid)
        chips = " ".join(str(src(sid)) for sid in facts[fid].get("sources", []))
        return Markup(f"{value} {chips}")

    def ext(sid: str, text: str | None = None) -> Markup:
        s = use(sid)
        label = text or s.get("link_text") or s["title"]
        kind = "official site" if s.get("official", True) else "outside site"
        return Markup(
            f'{mark(sid)}<a class="ext" href="{escape(s["url"])}" rel="noreferrer">{escape(label)}'
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
        fpath = static_dir / rel
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
        href = f"mailto:{site['corrections_email']}?subject={quote(subject, safe='')}"
        body = str(site.get("corrections_mailto_body") or "").strip()
        if body:
            href += f"&body={quote(body, safe='')}"
        return href

    def _state_cfg(desk_cfg: dict | None, state_slug: str) -> dict | None:
        if not desk_cfg or not state_slug:
            return None
        for st_cfg in desk_cfg.get("states", []):
            if st_cfg.get("slug") == state_slug:
                return st_cfg
        return None

    def _fmt_nav_pair(pair: list[str], state_slug: str, st_cfg: dict | None) -> tuple[str, str]:
        vars_map = {
            "state": state_slug,
            "State": str((st_cfg or {}).get("name") or state_slug.capitalize()),
            "reasons_label": str((st_cfg or {}).get("reasons_label") or "Exemptions"),
        }
        return (str(pair[0]).format(**vars_map), str(pair[1]).format(**vars_map))

    def header_tagline(page: dict) -> str:
        desk_cfg = desks_by_slug.get(page.get("desk", ""))
        if not desk_cfg:
            return site["tagline"]
        st_cfg = _state_cfg(desk_cfg, page.get("state", ""))
        if st_cfg and st_cfg.get("tagline"):
            return str(st_cfg["tagline"])
        return str(desk_cfg.get("tagline") or desk_cfg.get("full_title") or site["tagline"])

    def switcher_label(page: dict) -> str:
        st = page.get("state", "")
        if st:
            return f"State: {st.capitalize()}"
        desk_cfg = desks_by_slug.get(page.get("desk", ""))
        if desk_cfg:
            return str(desk_cfg.get("switcher_label") or f"{desk_cfg['name']} states")
        return "Jump to a guide"

    def switcher_aria(page: dict) -> str:
        return f"{switcher_label(page)}. Switch state or desk guide"

    def switcher_items(page: dict) -> list[dict]:
        desk_slug = page.get("desk", "")
        desk_cfg = desks_by_slug.get(desk_slug)
        if not desk_cfg:
            return []
        items: list[dict] = []
        for st_cfg in desk_cfg.get("states", []):
            items.append({
                "slug": st_cfg["page"],
                "name": st_cfg.get("switcher_name") or st_cfg["name"],
                "guide": st_cfg.get("switcher_guide") or st_cfg.get("tagline") or desk_cfg["name"],
                "sep": False,
            })
        hub_entry = desk_cfg.get("switcher_hub")
        if hub_entry:
            items.append({
                "slug": hub_entry.get("page") or desk_cfg["hub"],
                "name": hub_entry["name"],
                "guide": hub_entry["guide"],
                "sep": True,
            })
        for other in active_desks:
            if other["slug"] == desk_slug:
                continue
            cross = other.get("switcher_cross") or {}
            items.append({
                "slug": other["hub"],
                "name": cross.get("name") or other["name"],
                "guide": cross.get("guide") or other["blurb"],
                "sep": not bool(hub_entry),
            })
        items.append({
            "slug": "start#all-guides",
            "name": "Find a guide",
            "guide": "2-question router & full guide finder",
            "sep": False,
        })
        return items

    def nav_items(page: dict) -> list[tuple[str, str]]:
        desk_cfg = desks_by_slug.get(page.get("desk", ""))
        if desk_cfg:
            st_slug = page.get("state", "")
            st_cfg = _state_cfg(desk_cfg, st_slug)
            nav_cfg = desk_cfg.get("nav") or {}
            if st_cfg and st_cfg.get("is_state_nav", True) and nav_cfg.get("state"):
                return [_fmt_nav_pair(pair, st_slug, st_cfg) for pair in nav_cfg["state"]]
            if nav_cfg.get("hub"):
                return [(str(a), str(b)) for a, b in nav_cfg["hub"]]
        return (
            [("index", "Home"), ("start", "Start here")]
            + [(d["hub"], d.get("nav_label") or d["name"]) for d in active_desks]
            + [("sources", "Sources")]
        )

    def dateline_places(page: dict) -> str:
        slug = page.get("slug", "")
        if slug == "index":
            return str(site.get("dateline_places") or "")
        for d in active_desks:
            if slug == d.get("hub"):
                return str(d.get("dateline_places") or "")
        return ""

    def desk_has_quick_exit(page: dict) -> bool:
        desk_cfg = desks_by_slug.get(page.get("desk", ""))
        return bool(desk_cfg and desk_cfg.get("quick_exit"))

    pages_by_route = {p["route"]: p for p in pages}
    desks_by_hub = {d["hub"]: d for d in active_desks if d.get("hub")}

    def breadcrumbs(page: dict) -> list[dict]:
        slug = page.get("slug", "")
        route = str(page.get("route") or "/")
        if slug == "404":
            return []
        parts = [seg for seg in route.strip("/").split("/") if seg]
        if len(parts) < 2:
            return []
        crumbs: list[dict] = [{"label": "Home", "href": page_url("index")}]
        for idx in range(1, len(parts)):
            prefix = "/" + "/".join(parts[:idx]) + "/"
            anc = pages_by_route.get(prefix)
            if not anc:
                continue
            if anc["slug"] in desks_by_hub:
                label = desks_by_hub[anc["slug"]]["name"]
            elif idx == 2 and anc.get("state"):
                label = str(anc["state"]).capitalize()
            else:
                label = str(anc["title"])
            crumbs.append({"label": label, "href": page_url(anc["slug"])})
        if len(parts) == 2 and page.get("state"):
            current_label = str(page["state"]).capitalize()
        else:
            current_label = str(page.get("title") or parts[-1])
        crumbs.append({"label": current_label, "href": None})
        return crumbs

    def footer_items(page: dict) -> list[tuple[str, str]]:
        base_links = [("index", "CivicDesk home"), ("start", "Start here")] + [
            (d["hub"], d.get("footer_label") or f"{d['name']} desk") for d in active_desks
        ]
        desk_cfg = desks_by_slug.get(page.get("desk", ""))
        st_slug = page.get("state", "")
        st_cfg = _state_cfg(desk_cfg, st_slug)
        footer_cfg = (desk_cfg or {}).get("footer") or {}
        if st_cfg and st_cfg.get("is_state_nav", True) and footer_cfg.get("state"):
            mid_links = [_fmt_nav_pair(pair, st_slug, st_cfg) for pair in footer_cfg["state"]]
        elif footer_cfg.get("desk"):
            mid_links = [(str(a), str(b)) for a, b in footer_cfg["desk"]]
        else:
            mid_links = []
            for d in active_desks:
                for sc in d.get("states", []):
                    if sc.get("site_footer_label"):
                        mid_links.append((sc["page"], sc["site_footer_label"]))
                if d.get("tool") and d["tool"].get("footer_label"):
                    mid_links.append((d["tool"]["page"], d["tool"]["footer_label"]))
        tail_links = [
            ("sources", "All sources"),
            ("about", "About"),
            ("accessibility", "Accessibility"),
            ("privacy", "Privacy"),
        ]
        return base_links + mid_links + tail_links

    public_checked = [as_date(s["checked"]) for s in sources.values() if not s.get("docs_only")]
    site_latest_checked = max(public_checked) if public_checked else as_date(site["last_checked"])

    env.globals.update(
        site=site,
        active_desks=active_desks,
        desks_by_slug=desks_by_slug,
        pages=pages,
        sources=sources,
        sources_by_desk=sources_by_desk,
        all_sources_list=all_sources_list,
        medicaid_sources=sources_by_desk.get("medicaid", []),
        id_records_sources=sources_by_desk.get("id-records", []),
        facts=facts,
        medicaid_languages=medicaid_languages,
        state_checked=state_checked,
        art_dims=art_dims,
        read_depth_labels=READ_DEPTH_LABELS,
        read_by_labels=READ_BY_LABELS,
        read_note=read_note,
        status_badge=status_badge,
        src=src,
        basis=basis,
        analogy_for=analogy_for,
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
        dateline_places=dateline_places,
        desk_has_quick_exit=desk_has_quick_exit,
        breadcrumbs=breadcrumbs,
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
        site_latest_checked=site_latest_checked,
        noindex_build=noindex_build,
    )

    bs_macros = env.get_template("broadsheet.html").module

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    all_public_dates = [as_date(s["checked"]) for s in sources.values() if not s.get("docs_only")]
    public_dates_by_desk = {
        dslug: [as_date(s["checked"]) for s in entries if not s.get("docs_only")]
        for dslug, entries in sources_by_desk.items()
    }

    page_reports: list[dict] = []

    in_ci = any(env_vars.get(k) for k in CI_ENV_KEYS)
    lastmod_writable = (
        not in_ci
        and out_dir.resolve() == DEFAULT_OUT.resolve()
        and args.site_yaml is None
        and static_dir.resolve() == STATIC.resolve()
    )
    lastmod_old = load_lastmod()
    lastmod_new: dict[str, dict] = {}
    lastmod_by_slug: dict[str, str] = {}
    lastmod_status: dict[str, str] = {}
    local_today = dt.date.today().isoformat()
    commit_date: str | None = None

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
        except BuildError as exc:
            errors.append(str(exc))
            continue

        reg_scope = p.get("source_registry")
        if reg_scope == "all":
            cited_dates = list(all_public_dates)
        elif reg_scope in public_dates_by_desk:
            cited_dates = list(public_dates_by_desk[reg_scope])
        else:
            cited_dates = [as_date(sources[sid]["checked"]) for sid in state["used"]]

        if cited_dates:
            oldest_date = min(cited_dates)
            newest_date = max(cited_dates)
        else:
            oldest_date = newest_date = page_date

        html = (
            html.replace(CHECKED_TIME_MARK, _format_checked_time(oldest_date, newest_date))
            .replace(CHECKED_TEXT_MARK, _format_checked_text(oldest_date, newest_date))
            .replace(CHECKED_OLDEST_TEXT_MARK, human_date(oldest_date))
        )

        show_tabs = slug not in NO_TAB_PAGES
        html = apply_sources_layer(
            html,
            list(state["used"]),
            render_tabs=lambda n, _p=p: bs_macros.page_tabs(n, "Tool" if _p.get("tool") else "Guide"),
            render_section_link=bs_macros.section_sources,
            render_panel=lambda groups, n, _t=show_tabs: bs_macros.sources_panel(
                [{**g, "sources": [sources[sid] for sid in g["sources"]]} for g in groups], n, tabs=_t
            ),
            tabs=show_tabs,
        )
        body_badges = detect_body_badges(html)
        if body_badges and not reg_scope and slug not in {"about", "404"}:
            html = insert_label_key(html, page_url("sources#labels"))

        route = p["route"]
        dest_file = built_html_path(out_dir, p)
        html = bind_trailing_punctuation(html)

        digest = content_hash(html)
        prior = lastmod_old.get(slug)
        if prior and prior["hash"] == digest:
            lastmod_new[slug] = prior
            lastmod_status[slug] = "current"
        elif lastmod_writable:
            lastmod_new[slug] = {"date": local_today, "hash": digest}
            lastmod_status[slug] = "updated"
        else:
            if prior:
                kept = prior["date"]
            else:
                commit_date = commit_date or head_commit_date() or local_today
                kept = commit_date
            lastmod_new[slug] = {"date": kept, "hash": prior["hash"] if prior else digest}
            lastmod_status[slug] = "stale" if prior else "new"
            note = (
                f"page {slug}: content changed but data/lastmod.json was not updated; keeping lastmod {kept}. "
                "Run python3 scripts/build.py locally and commit data/lastmod.json."
                if prior else
                f"page {slug}: not in data/lastmod.json; using the latest commit date {kept}."
            )
            warnings.append(note)
            if env_vars.get("GITHUB_ACTIONS"):
                print(f"::warning file=data/lastmod.json::{note}")
        lastmod_by_slug[slug] = lastmod_new[slug]["date"]

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
            "lastmod": lastmod_by_slug[slug],
            "lastmod_status": lastmod_status[slug],
        })

    if errors:
        print("BUILD FAILED", file=sys.stderr)
        for e in errors:
            print("  error:", e, file=sys.stderr)
        return 1

    shutil.copytree(
        static_dir,
        out_dir / "static",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".gitkeep", "*.md"),
    )
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
        for s in all_sources_list
        if not s.get("docs_only")
    ]
    (out_dir / "sources.json").write_text(json.dumps(public_sources, indent=2), encoding="utf-8")
    (out_dir / "facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")

    if lastmod_writable:
        text = dump_lastmod(lastmod_new)
        if not LASTMOD_PATH.is_file() or LASTMOD_PATH.read_text(encoding="utf-8") != text:
            LASTMOD_PATH.write_text(text, encoding="utf-8")
            changed = sorted(s for s, v in lastmod_status.items() if v == "updated")
            print(f"  lastmod: updated data/lastmod.json ({len(changed)} page(s) changed content)")

    robots_txt = "User-agent: *\nAllow: /\n"
    if pages_url:
        base = pages_url.rstrip("/") + "/"
        robots_txt += f"\nSitemap: {base}sitemap.xml\n"
        urls = "\n".join(
            f"  <url><loc>{base}{p['route'].lstrip('/')}</loc><lastmod>{lastmod_by_slug[p['slug']]}</lastmod></url>"
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

    desk_source_counts = {d["slug"]: len(desk_maps[d["slug"]]) for d in active_desks}
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
        "desk_source_counts": desk_source_counts,
        "medicaid_source_count": desk_source_counts.get("medicaid", 0),
        "id_records_source_count": desk_source_counts.get("id-records", 0),
        "total_source_count": len(sources),
        "fact_count": len(facts),
        "page_count": len(page_reports),
        "fonts_bundled": fonts_available,
        "lastmod_manifest": "updated locally" if lastmod_writable else "read-only",
        "warnings": warnings,
        "pages": page_reports,
    }
    (out_dir / "build-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    counts_summary = ", ".join(
        f"{len(desk_maps[d['slug']])} {d['source_prefix']}* sources" for d in active_desks
    )
    print(f"Built {len(page_reports)} pages ({counts_summary}, {len(facts)} facts) -> {out_dir}")
    for w in warnings:
        print("  warning:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
