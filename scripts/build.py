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
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
SITE_ROOT = ROOT / "site-root"
DEFAULT_OUT = ROOT / "public"
ART_NAME_RE = re.compile(r"print-[a-z0-9-]+\.webp")

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


def read_note(s: dict) -> str:
    """Reader-facing line for how closely a source was read, e.g. "Read in full by a person, with software help"."""
    depth = READ_DEPTH_LABELS[s["read_depth"]]
    if s["read_by"] == "not_read":
        return depth
    return f"{depth} by a person, with software help"
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
    "SourceSerif4-Latin.woff2": "8e38e3fbd74dea20aabe0831ffef04c9f30bf6fc5b99b7a2649be0dd8c6d859c",
    "SourceSerif4-LatinExt.woff2": "231f594ad829d45f905a8a6cdce704f659615e00f1a9db2699320e6e315c1d1f",
    "SourceSerif4-OFL.txt": "c21d7293d87b6d7ab1d0229a2f55b77f33a7613a6a4e66f6693d68d7d8d09464",
}
# Source Serif 4 is the default text face, so these files are required. Atkinson stays optional.
REQUIRED_FONTS = ("SourceSerif4-Latin.woff2", "SourceSerif4-LatinExt.woff2", "SourceSerif4-OFL.txt")

CSP_META = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
)

BADGE_RE = re.compile(r'<span class="badge badge-(not-official|not-confirmed|our-reading)">')
CHECKED_P_RE = re.compile(r'(<p class="checked">.*?</p>)', re.S)
LEDE_P_RE = re.compile(r'(<p class="lede">.*?</p>)', re.S)
WHY_P_RE = re.compile(r'(<p class="why-it-matters">.*?</p>)', re.S)
PURPOSE_P_RE = re.compile(r'(<p class="purpose">.*?</p>)', re.S)

_SRC_LINK_RE = r'<a class="src" [^>]+><span class="src-label">Source:</span>(?:&nbsp;|\s+)[^<]+</a>'
_STATUS_BADGE_RE = r'<span class="badge badge-(?:official|not-official|not-confirmed|our-reading)">[^<]+</span>'
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
    for pattern in (WHY_P_RE, LEDE_P_RE, PURPOSE_P_RE, CHECKED_P_RE):
        if pattern.search(html):
            return pattern.sub(r"\1\n" + key, html, count=1)
    return html


# --- Broadsheet sources layer ------------------------------------------------------------------
# src(), fact() and ext() leave an HTML comment marker where a source is used. After rendering,
# apply_sources_layer() uses the markers to group sources by page section, strips the inline
# "Source:" links from the reading flow, adds one "Sources for this section (n)" link per section,
# and fills the single grouped Sources panel. Honesty badges that qualify a claim (Our reading,
# Not confirmed) stay inline; a source's own Not official badge moves to the panel with it.
SRC_MARK_RE = re.compile(r"<!--cd-src:([A-Za-z0-9_.-]+)-->")
SOURCE_PANEL_MARK = "<!--cd-source-panel-->"
# The front page is a directory of desks, not a guide, so it keeps one plain Sources panel and no tabs.
NO_TAB_PAGES = {"index"}
CONTENT_OPEN = '<div class="wrap content">'
INLINE_CITE_RE = re.compile(
    rf'<span class="src-unit">{_SRC_LINK_RE}\s+{_STATUS_BADGE_RE}</span>|{_SRC_LINK_RE}'
)
_EMPTY_PARENS_RE = re.compile(r"[ \t]*\(\s*\)")
_DOUBLE_STOP_RE = re.compile(r"(?<=[.!?])\s+\.(?=[\s<]|$)")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"(?<=[^\s>])[ \t\n]+(?=[.,;](?:[\s<]|$))")
_SPACE_BEFORE_CLOSE_RE = re.compile(r"[ \t]+(?=</(?:p|li|dd|td|th)>)")
_EMPTY_P_RE = re.compile(r"<p(?![^>]*\bid=)(?:\s[^>]*)?>\s*</p>\s*")
_ID_ATTR_RE = re.compile(r'\bid="([^"]+)"')
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_OPTIONAL_END_TAGS = {"li", "p", "dt", "dd", "tr", "td", "th", "option", "thead", "tbody"}
HEAD_P_CLASSES = {"kicker", "lede", "why-it-matters", "purpose", "checked"}
HEAD_BLOCK_CLASSES = {"hero-split", "intro-split", "page-head", "fp-hero", "guide-switch", "brief-strip", "label-key"}


# ---------- Four-beat page fields (data/pages.yaml) ----------
# answer and why: Beat 1. spec: Beat 3, each row optional. analogies: everyday comparisons that
# explain how an agency system works; each needs a one-sentence boundary and MED-* or ID-* basis.
# Which pages must have them is enforced by tests/test_page_fields.py and data/voice_rollout.yaml.
SPEC_KEYS = ("cost", "clock", "paperwork", "catch")
ANALOGY_PLACEMENTS = {"lead", "inline"}
ANALOGY_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Abbreviations whose periods do not end a sentence (U.S., D.C., a.m., No., Sept., Fed. Reg., v.).
_ABBREV_RE = re.compile(
    r"\b(?:[A-Za-z]\.){2,}|\b(?:No|Nos|Mr|Ms|Mrs|Dr|St|Inc|Co|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
    r"|Ch|Sec|Subsec|Art|Stat|Fed|Reg|Admin|Dept|Div|Gov|Sen|Rep|Mass|Wash|Ore|Or|Wn|App|Rev|Ann|vs|v|etc|approx|p|pp|para|ed)\."
)
_SENTENCE_BREAK_RE = re.compile(r"[.!?][\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")


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


# ---------- Sitemap lastmod (data/lastmod.json) ----------
# lastmod follows page content, not source check dates. The hash covers <main> text, image alt
# text, and absolute links, minus "Sources checked" lines, checked-date spans, the print masthead,
# the Sources panel, per-section source links, and page tabs, so re-checking a source never moves
# lastmod.
LASTMOD_PATH = DATA / "lastmod.json"
_HASH_SKIP_CLASSES = {"checked", "checked-date", "print-masthead", "page-sources", "section-sources-row", "page-tabs"}
_HASH_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "col", "area", "base", "embed", "track"}
CI_ENV_KEYS = ("CI", "GITHUB_ACTIONS", "WORKERS_CI", "WORKERS_CI_COMMIT_SHA", "WORKERS_CI_BRANCH", "CF_PAGES")


class _ContentText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in_main = False
        self._skip = 0
        self._raw = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._in_main = True
            return
        if not self._in_main:
            return
        if tag in ("script", "style"):
            self._raw += 1
            return
        if self._skip:
            if tag not in _HASH_VOID:
                self._skip += 1
            return
        a = dict(attrs)
        if set((a.get("class") or "").split()) & _HASH_SKIP_CLASSES:
            if tag not in _HASH_VOID:
                self._skip = 1
            return
        self._collect(a)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags (<path/> inside inline SVG icons, <img/>) open no element, so they
        # must never change the skip depth; they only contribute alt text and links.
        if self._in_main and not self._skip and not self._raw:
            self._collect(dict(attrs))

    def _collect(self, a: dict) -> None:
        if a.get("alt"):
            self.parts.append(f"[alt:{a['alt']}]")
        href = a.get("href") or ""
        if href.startswith(("http://", "https://")):
            self.parts.append(f"[href:{href}]")

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self._in_main = False
            return
        if not self._in_main:
            return
        if tag in ("script", "style"):
            self._raw = max(0, self._raw - 1)
        elif self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_main and not self._skip and not self._raw:
            self.parts.append(data)


def content_hash(html_text: str) -> str:
    parser = _ContentText()
    parser.feed(html_text)
    parser.close()
    text = " ".join(parser.parts).replace("\u2060", "").replace("\u00ad", "")
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_lastmod(path: Path = LASTMOD_PATH) -> dict[str, dict]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("hash") and v.get("date")}


def dump_lastmod(entries: Mapping[str, dict]) -> str:
    # One page per line keeps merge conflicts to the pages two branches both changed.
    rows = [f"  {json.dumps(slug)}: {json.dumps({'date': e['date'], 'hash': e['hash']})}" for slug, e in sorted(entries.items())]
    return "{\n" + ",\n".join(rows) + "\n}\n"


def head_commit_date(root: Path = ROOT) -> str | None:
    try:
        proc = subprocess.run(["git", "log", "-1", "--format=%cs"], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", out) else None


def strip_inline_citations(body: str) -> str:
    """Remove inline source links, their wrappers and markers, then tidy the punctuation they leave."""
    body = INLINE_CITE_RE.sub("", body)
    body = SRC_MARK_RE.sub("", body)
    body = _EMPTY_PARENS_RE.sub("", body)
    body = _DOUBLE_STOP_RE.sub("", body)
    body = _SPACE_BEFORE_PUNCT_RE.sub("", body)
    body = _SPACE_BEFORE_CLOSE_RE.sub("", body)
    body = _EMPTY_P_RE.sub("", body)
    return body


class _Outline(HTMLParser):
    """Record element offsets, top-level children, and <h2> positions within an HTML fragment."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self._line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.stack: list[dict] = []
        self.top: list[dict] = []
        self.h2s: list[dict] = []
        self.h1_top: dict | None = None
        self._h2: dict | None = None
        self._template = 0
        self._dated: list[str] = []  # open elements with data-show-until inside an <h2>

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        start = self._offset()
        a = {k: (v or "") for k, v in attrs}
        node = {"tag": tag, "start": start, "tag_end": start + len(self.get_starttag_text() or ""),
                "classes": set(a.get("class", "").split()), "id": a.get("id", ""), "end": None}
        if not self.stack:
            self.top.append(node)
        if tag == "h1" and self.h1_top is None:
            self.h1_top = self.stack[0] if self.stack else node
        if tag == "h2" and not self._template:
            self._h2 = {**node, "ancestors": list(self.stack), "text": ""}
        if tag == "template":
            self._template += 1
        if self._h2 is not None and tag not in _VOID_TAGS and (self._dated or "data-show-until" in a):
            # Date-limited heading text is hidden by site.js after its date; keep it out of titles.
            self._dated.append(tag)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS and self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()["end"] = self._offset()

    def handle_endtag(self, tag: str) -> None:
        here = self._offset()
        if tag == "template":
            self._template = max(0, self._template - 1)
        if self._dated and self._dated[-1] == tag:
            self._dated.pop()
        if tag == "h2" and self._h2 is not None:
            self._h2["text"] = re.sub(r"\s+", " ", self._h2["text"]).strip()
            self.h2s.append(self._h2)
            self._h2 = None
        while self.stack and self.stack[-1]["tag"] != tag and self.stack[-1]["tag"] in _OPTIONAL_END_TAGS:
            self.stack.pop()["end"] = here
        if self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()["end"] = here

    def handle_data(self, data: str) -> None:
        if self._h2 is not None and not self._dated:
            self._h2["text"] += data


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].rstrip("-") or "section"


def _is_head_block(node: dict) -> bool:
    if node["tag"] == "h1":
        return True
    if node["tag"] == "p" and node["classes"] & HEAD_P_CLASSES:
        return True
    return bool(node["classes"] & HEAD_BLOCK_CLASSES)


def outline_sections(region: str, existing_ids: set[str], tabs: bool) -> dict:
    """Split a page body into sections at each <h2>.

    Returns the tab insertion offset, ids to add to <h2>s that lack one, and for each section its
    id, title, the offset where its "Sources for this section" link goes, and the source ids used
    in it (in first-use order). Section i runs from the previous link offset to its own.
    """
    ol = _Outline(region)
    ol.feed(region)
    ol.close()

    tabs_at = None
    if tabs:
        seen_h1 = False
        for node in ol.top:
            if seen_h1 and not _is_head_block(node):
                tabs_at = node["start"]
                break
            if node is ol.h1_top or node["tag"] == "h1":
                seen_h1 = True
        if tabs_at is None:
            tabs_at = len(region) if seen_h1 else 0

    add_ids: list[tuple[int, str]] = []
    taken = set(existing_ids)
    sections = [{"id": "top", "title": "Top of the page", "h2": None}]
    for h in ol.h2s:
        sid = h["id"]
        if not sid:
            base = "sec-" + _slugify(h["text"])
            sid, n = base, 2
            while sid in taken:
                sid, n = f"{base}-{n}", n + 1
            taken.add(sid)
            add_ids.append((h["start"] + 3, sid))
        sections.append({"id": sid, "title": h["text"] or "Untitled section", "h2": h})

    # Link offset for each section: the start of the outermost block that holds the next <h2>
    # (below the lowest element shared with this section's <h2>), or the end of the body.
    for i, sec in enumerate(sections):
        nxt = sections[i + 1]["h2"] if i + 1 < len(sections) else None
        if nxt is None:
            sec["link_at"] = len(region)
            continue
        cur_anc = sec["h2"]["ancestors"] if sec["h2"] else []
        nxt_anc = nxt["ancestors"]
        common = 0
        while (common < len(cur_anc) and common < len(nxt_anc)
               and cur_anc[common]["start"] == nxt_anc[common]["start"]):
            common += 1
        holder = nxt_anc[common] if common < len(nxt_anc) else nxt
        sec["link_at"] = holder["start"]
    if tabs_at is not None and sections[0]["link_at"] < tabs_at:
        sections[0]["link_at"] = tabs_at

    marks = [(m.start(), m.group(1)) for m in SRC_MARK_RE.finditer(region)]
    lo = 0
    for sec in sections:
        hi = sec["link_at"]
        ids: list[str] = []
        for pos, sid in marks:
            if lo <= pos < hi and sid not in ids:
                ids.append(sid)
        sec["sources"] = ids
        lo = hi
    return {"tabs_at": tabs_at, "add_ids": add_ids, "sections": sections}


# Trailing punctuation stays on the same line as the inline element it follows. Some engines
# (WebKit with text-wrap: pretty) can otherwise wrap a lone ".", ",", ";", or ":" onto the next
# line after </a>, </code>, or </span>. A WORD JOINER (U+2060, zero width, ignored by screen
# readers) forbids that break without changing what anyone reads or copies.
WORD_JOINER = "&#8288;"
INLINE_CLOSE_PUNCT_RE = re.compile(
    r"(</(?:a|abbr|b|bdi|code|em|i|kbd|q|small|span|strong|sup|time)>)(?=[.,;:!?)\]])"
)
RAW_TEXT_BLOCK_RE = re.compile(r"<!--.*?-->|<(script|style|pre|textarea)\b.*?</\1\s*>", re.S | re.I)


BADGE_LABEL_RE = re.compile(r'(<span class="(?:badge|tag)\b[^"]*">)([^<]*)(</span>)')


def _bind_segment(text: str) -> str:
    # Badge labels ("Not official") stay on one line unless the badge is wider than the line.
    text = BADGE_LABEL_RE.sub(lambda m: m.group(1) + m.group(2).replace(" ", "&nbsp;") + m.group(3), text)
    return INLINE_CLOSE_PUNCT_RE.sub(r"\1" + WORD_JOINER, text)


def bind_trailing_punctuation(html: str) -> str:
    """Insert a word joiner between a closing inline tag and the punctuation right after it, and
    join badge words with no-break spaces, outside comments and script, style, pre, and textarea."""
    out, last = [], 0
    for m in RAW_TEXT_BLOCK_RE.finditer(html):
        out.append(_bind_segment(html[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_bind_segment(html[last:]))
    return "".join(out)


def apply_sources_layer(html: str, used: list[str], render_tabs, render_section_link, render_panel,
                        tabs: bool) -> str:
    """Strip inline citations, add per-section links and tabs, and fill the grouped Sources panel."""
    open_idx = html.find(CONTENT_OPEN)
    panel_idx = html.find(SOURCE_PANEL_MARK)
    if open_idx == -1:
        return SRC_MARK_RE.sub("", html)
    body_start = open_idx + len(CONTENT_OPEN)
    if panel_idx == -1:
        main_end = html.find("</main>", body_start)
        stripped = strip_inline_citations(html[body_start:main_end])
        return SRC_MARK_RE.sub("", html[:body_start] + stripped + html[main_end:])

    region = html[body_start:panel_idx]
    info = outline_sections(region, set(_ID_ATTR_RE.findall(html)), tabs)
    sections = info["sections"]

    placed: list[str] = []
    for sec in sections:
        for sid in sec["sources"]:
            if sid not in placed:
                placed.append(sid)
    rest = [sid for sid in used if sid not in placed]
    groups = [{"id": s["id"], "title": s["title"], "sources": s["sources"]} for s in sections if s["sources"]]
    if rest:
        groups.append({"id": "more", "title": "Also used on this page", "sources": rest})

    # (offset, order at that offset, text). Lower order lands first in the output.
    inserts: list[tuple[int, int, str]] = []
    for pos, new_id in info["add_ids"]:
        inserts.append((pos, 0, f' id="{new_id}"'))
    if info["tabs_at"] is not None:
        inserts.append((info["tabs_at"], 0, str(render_tabs(len(used))) + '\n<div id="guide" class="page-guide">\n'))
        inserts.append((len(region), 2, "\n</div>\n"))
    for sec in sections:
        if sec["sources"]:
            link = str(render_section_link(sec["id"], len(sec["sources"])))
            inserts.append((sec["link_at"], 1, f'\n<p class="section-sources-row">{link}</p>\n'))
    # Apply from the end so earlier offsets stay valid.
    for pos, _order, text in sorted(inserts, key=lambda t: (t[0], t[1]), reverse=True):
        region = region[:pos] + text + region[pos:]

    region = strip_inline_citations(region)
    panel = str(render_panel(groups, len(used)))
    out = html[:body_start] + region + panel + html[panel_idx + len(SOURCE_PANEL_MARK):]
    return SRC_MARK_RE.sub("", out)


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
        if s.get("review_by"):
            as_date(s["review_by"])
        out[sid] = s
    return out


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


VALID_DESK_STATUS = {"active", "upcoming"}


def validate_desks(site: dict, page_slugs: set[str], errors: list[str]) -> list[dict]:
    """Check the desk registry in site.yaml. Active desks must point at real pages."""
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
            pages_needed = [d.get("hub")] + [st.get("page") for st in d.get("states", [])]
            if d.get("tool"):
                pages_needed.append(d["tool"].get("page"))
            for ps in pages_needed:
                if ps not in page_slugs:
                    errors.append(f"{label}: unknown page slug {ps!r}")
            for key in ("source_prefix", "art", "states"):
                if not d.get(key):
                    errors.append(f"{label}: active desk needs '{key}'")
    if not any(d.get("status") == "active" for d in desks):
        errors.append("site.yaml: desks must list at least one active desk")
    return desks


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
        validate_page_voice(p, sources, facts, errors)
        # Every page carries one press print (scripts/make_art.py) beside its heading.
        art = str(p.get("art") or "")
        if not art:
            errors.append(f"page {slug}: missing 'art' (a static/img/art/print-*.webp file)")
        elif not ART_NAME_RE.fullmatch(art) or not (static_dir / "img" / "art" / art).is_file():
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
    # Workers Builds sets WORKERS_CI_BRANCH; any build not from main is a preview.
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

    def mark(sid: str) -> str:
        return f"<!--cd-src:{sid}-->"

    def src(sid: str, label: str | None = None) -> Markup:
        # The inline link is stripped after rendering (apply_sources_layer); the marker records
        # which section used the source so the Sources panel can group it.
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
        # Rule citation for an ID path finder result block (templates/id-records/path.html).
        # data-basis lists the ID-* sources whose published rule sorts a reader onto this pathway;
        # data-basis-label carries their short names for the visible "Sorted by" line in path.js.
        if not sids:
            raise BuildError(f"page {state['page']}: basis() needs at least one source id")
        labels = []
        for sid in sids:
            if not sid.startswith("ID-"):
                raise BuildError(f"page {state['page']}: basis() takes ID-* sources only, got '{sid}'")
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
                {"slug": "id-records-path", "name": "Find your path", "guide": "Step-by-step record-update order", "sep": True},
                {"slug": "medicaid", "name": "2027 Medicaid desk", "guide": "OHP, Apple Health, and Utah Medicaid", "sep": False},
                {"slug": "start", "name": "Start here", "guide": "2-question router & full guide finder", "sep": False},
            ]
        return [
            {"slug": "start", "name": "Start here", "guide": "2-question router & full guide finder", "sep": False},
            {"slug": "medicaid", "name": "2027 Medicaid overview", "guide": "Federal & 3-state summary", "sep": True},
            {"slug": "medicaid-oregon", "name": "Oregon Medicaid (OHP)", "guide": "Rules, exemptions & 1-minute check", "sep": False},
            {"slug": "medicaid-washington", "name": "Washington Apple Health", "guide": "Rules, exemptions & 1-minute check", "sep": False},
            {"slug": "medicaid-utah", "name": "Utah Medicaid", "guide": "Rules, exclusions & 1-minute check", "sep": False},
            {"slug": "id-records-path", "name": "ID & records path finder", "guide": "Step-by-step record-update order", "sep": True},
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
                    (f"medicaid-{st}-reasons", "Exclusions & exceptions" if st == "utah" else "Exemptions"),
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

    public_checked = [as_date(s["checked"]) for s in sources.values() if not s.get("docs_only")]
    site_latest_checked = max(public_checked) if public_checked else as_date(site["last_checked"])

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
    med_public_dates = [as_date(s["checked"]) for s in medicaid_list if not s.get("docs_only")]
    id_public_dates = [as_date(s["checked"]) for s in id_records_list if not s.get("docs_only")]

    page_reports: list[dict] = []

    # data/lastmod.json is rewritten only by a local default build (the required pre-PR
    # `python3 scripts/build.py`, which local pytest also runs). CI and Cloudflare builds read it:
    # a stale entry keeps its previous date rather than jumping forward, and is reported.
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

        html = apply_sources_layer(
            html,
            list(state["used"]),
            render_tabs=lambda n, _p=p: bs_macros.page_tabs(n, "Tool" if _p.get("tool") else "Guide"),
            render_section_link=bs_macros.section_sources,
            render_panel=lambda groups, n: bs_macros.sources_panel(
                [{**g, "sources": [sources[sid] for sid in g["sources"]]} for g in groups], n
            ),
            tabs=slug not in NO_TAB_PAGES,
        )
        body_badges = detect_body_badges(html)
        if body_badges and slug not in {"sources", "medicaid-sources", "id-records-sources", "about", "404"}:
            html = insert_label_key(html, page_url("sources#labels"))

        route = p["route"]
        if slug == "404":
            dest_file = out_dir / "404.html"
        elif route == "/":
            dest_file = out_dir / "index.html"
        else:
            dest_file = out_dir / route.strip("/") / "index.html"
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
        for s in (medicaid_list + id_records_list)
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
        "lastmod_manifest": "updated locally" if lastmod_writable else "read-only",
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
