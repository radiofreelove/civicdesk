"""Four-beat page fields (answer, why, spec, analogies), their rollout ledger, and sitemap lastmod."""
from __future__ import annotations

import importlib.util
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent


def _load_build():
    spec = importlib.util.spec_from_file_location("civicdesk_build_fields", ROOT / "scripts" / "build.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_build()
PAGES = yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))
LEDGER = yaml.safe_load((ROOT / "data" / "voice_rollout.yaml").read_text(encoding="utf-8"))
SOURCES = {
    s["id"]: s
    for f in sorted((ROOT / "data" / "sources").glob("*.yaml"))
    for s in (yaml.safe_load(f.read_text(encoding="utf-8")) or [])
}
FACTS = yaml.safe_load((ROOT / "data" / "facts.yaml").read_text(encoding="utf-8"))


# ---------- Rollout ledger ----------

def test_rollout_ledger_is_consistent() -> None:
    slugs = [p["slug"] for p in PAGES]
    exempt, pending = LEDGER["analogy_exempt"], LEDGER["pending"]
    assert len(exempt) == len(set(exempt)) and len(pending) == len(set(pending))
    assert set(exempt) <= set(slugs), sorted(set(exempt) - set(slugs))
    assert set(pending) <= set(slugs) - {"404"}, sorted(set(pending) - set(slugs))
    assert "404" in exempt
    non_exempt = set(slugs) - set(exempt)
    assert len(non_exempt) > 0 and len(exempt) + len(non_exempt) == len(slugs)
    for slug in ("medicaid-oregon-immigration", "medicaid-washington-immigration", "medicaid-utah-immigration"):
        assert slug in exempt


def test_four_beat_fields_ratchet() -> None:
    exempt, pending = set(LEDGER["analogy_exempt"]), set(LEDGER["pending"])
    for p in PAGES:
        slug = p["slug"]
        if slug == "404":
            continue
        has_lead = build.analogy_for(p) is not None
        complete = bool(p.get("answer")) and bool(p.get("why")) and (slug in exempt or has_lead)
        if slug in pending:
            assert not complete, f"{slug}: fields are complete; remove it from pending in data/voice_rollout.yaml"
        else:
            assert complete, (
                f"{slug}: needs answer, why" + ("" if slug in exempt else ", and a lead analogy")
                + " in data/pages.yaml (or list it under pending in data/voice_rollout.yaml)"
            )


# ---------- Field validation ----------

def _errors(page: dict) -> list[str]:
    errors: list[str] = []
    build.validate_page_voice({"slug": "sample", **page}, SOURCES, FACTS, errors)
    return errors


GOOD_ANALOGY = {
    "id": "sample-analogy",
    "placement": "lead",
    "compare": "A court judgment works like a permission slip that the person carries to each office.",
    "boundary": "Some offices use their own form instead of a court order.",
    "basis": ["ID-OR-OJD-ADULT-PACKET"],
}


def test_valid_fields_pass_validation() -> None:
    assert _errors({
        "answer": "Oregon courts handle name and sex changes in one petition.",
        "why": "One filing fee covers both.",
        "spec": {"cost": "or_fee_petition", "clock": None, "paperwork": "ID-OR-OJD-ADULT-PACKET",
                 "catch": {"text": "Name-only cases are public.", "basis": ["ID-OR-ORS-33"]}},
        "analogies": [GOOD_ANALOGY, {**GOOD_ANALOGY, "id": "second", "placement": "inline"}],
    }) == []


@pytest.mark.parametrize(("page", "fragment"), [
    ({"answer": ""}, "'answer' must be a non-empty string"),
    ({"spec": {"fee": "or_fee_petition"}}, "unknown spec key 'fee'"),
    ({"spec": {"cost": "no_such_fact"}}, "spec.cost 'no_such_fact' is not a data/facts.yaml key"),
    ({"spec": {"cost": ["or_fee_petition"]}}, "spec.cost"),
    ({"spec": {"paperwork": "ID-NOPE"}}, "spec.paperwork 'ID-NOPE' is not a source id"),
    ({"spec": {"catch": {"text": "x", "basis": []}}}, "spec.catch needs at least one basis"),
    ({"spec": {"catch": {"text": "x", "basis": "ID-OR-ORS-33"}}}, "spec.catch needs at least one basis"),
    ({"spec": {"catch": {"text": "x", "basis": [["ID-OR-ORS-33"]]}}}, "is not a source id"),
    ({"analogies": [{**GOOD_ANALOGY, "boundary": ""}]}, "'boundary' (where the comparison stops) is required"),
    ({"analogies": [{**GOOD_ANALOGY, "boundary": "It stops here. It also stops there."}]}, "exactly one sentence"),
    ({"analogies": [{**GOOD_ANALOGY, "compare": " "}]}, "'compare' (how the system works) is required"),
    ({"analogies": [{**GOOD_ANALOGY, "basis": []}]}, "needs at least one MED-* or ID-* source id"),
    ({"analogies": [{**GOOD_ANALOGY, "basis": ["MED-NOPE"]}]}, "basis 'MED-NOPE' is not in data/sources"),
    ({"analogies": [{**GOOD_ANALOGY, "basis": [{"id": "ID-OR-ORS-33"}]}]}, "is not in data/sources"),
    ({"analogies": [GOOD_ANALOGY, {**GOOD_ANALOGY, "id": "other"}]}, "at most one analogy can have placement 'lead'"),
    ({"analogies": [{**GOOD_ANALOGY, "placement": "drawer"}]}, "placement must be one of"),
    ({"analogies": [{**GOOD_ANALOGY, "advice": "x"}]}, "unknown keys ['advice']"),
])
def test_invalid_fields_fail_validation(page: dict, fragment: str) -> None:
    assert any(fragment in e for e in _errors(page)), _errors(page)


@pytest.mark.parametrize(("text", "ok"), [
    ("U.S. passports print sex assigned at birth.", True),
    ("The reply window is on OHA's journey maps (p. 2).", True),
    ("Offices close at 5 p.m. Pacific on weekdays.", True),
    ("In State of Oregon v. Oz, the court has not issued a final ruling.", True),
    ("The federal interim final rule (91 Fed. Reg. 33348) sets the baseline.", True),
    ("It stops here. It also stops there.", False),
    ("No closing punctuation", False),
])
def test_boundary_is_one_sentence(text: str, ok: bool) -> None:
    assert build.is_one_sentence(text) is ok


# ---------- Macros ----------

def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=True, undefined=StrictUndefined)
    env.globals.update(
        analogy_for=build.analogy_for,
        src=lambda sid, label=None: Markup(f'<a class="src" data-src="{sid}">{sid}</a>'),
        fact=lambda fid: Markup(f'<span data-fact="{fid}">{FACTS[fid]["value"]}</span>'),
        ext=lambda sid, text=None: Markup(f'<a class="ext" data-src="{sid}">{sid}</a>'),
    )
    return env


def _render(call: str, page: dict) -> str:
    return _env().from_string('{% import "macros.html" as m %}' + call).render(page=page)


def test_analogy_macro_renders_boundary_basis_and_badge() -> None:
    inline = {**GOOD_ANALOGY, "id": "inline-one", "placement": "inline", "basis": ["ID-WA-DOH-SEX"]}
    page = {"slug": "sample", "analogies": [GOOD_ANALOGY, inline]}
    lead = _render("{{ m.analogy(page) }}", page)
    assert '<aside class="analogy" id="analogy-sample-analogy"' in lead
    assert "In everyday terms" in lead and GOOD_ANALOGY["compare"] in lead
    assert "<strong>Where the comparison stops:</strong> " + GOOD_ANALOGY["boundary"] in lead
    assert 'badge-our-reading' in lead and 'data-src="ID-OR-OJD-ADULT-PACKET"' in lead
    # Visible in the Short version: none of the classes brief mode hides.
    assert not re.search(r'class="[^"]*\b(?:deeper|brief-extra|more)\b', lead) and "data-deeper" not in lead

    rendered_inline = _render("{{ m.analogy(page, 'inline-one') }}", page)
    assert 'class="analogy analogy-inline"' in rendered_inline and 'data-src="ID-WA-DOH-SEX"' in rendered_inline
    assert _render("{{ m.analogy(page) }}", {"slug": "none"}).strip() == ""
    with pytest.raises(build.BuildError):
        _render("{{ m.analogy(page, 'missing') }}", page)


def test_answer_and_spec_strip_render_only_what_is_filled() -> None:
    page = {"slug": "sample", "answer": "One petition covers both.", "why": "One fee.",
            "spec": {"cost": "or_fee_petition", "clock": None, "paperwork": None, "catch": None}}
    out = _render("{{ m.answer(page) }}{{ m.spec_strip(page) }}", page)
    assert '<p class="lede"><span class="label">The big picture:</span> One petition covers both.</p>' in out
    assert "<strong>Why it matters:</strong> One fee." in out
    assert "<dt>Cost</dt>" in out and 'data-fact="or_fee_petition"' in out
    for label in ("Time", "Official forms", "The catch", "N/A"):
        assert f"<dt>{label}</dt>" not in out and "N/A" not in out
    empty = {"slug": "sample", "spec": {"cost": None, "clock": None, "paperwork": None, "catch": None}}
    assert _render("{{ m.spec_strip(page) }}", empty).strip() == ""
    assert _render("{{ m.spec_strip(page) }}", {"slug": "sample"}).strip() == ""


def test_label_key_and_page_tabs_keep_answer_and_why_together() -> None:
    # Regression: an analogy triggers insert_label_key() via its Our reading badge; neither the
    # label key nor Guide/Sources tabs may land between Beat 1's .lede and .why-it-matters.
    sample = (
        '<main><div class="wrap content">'
        '<h1>Court name change</h1>'
        '<p class="lede"><span class="label">The big picture:</span> One petition covers both.</p>'
        '<p class="why-it-matters"><strong>Why it matters:</strong> One fee.</p>'
        '<aside class="analogy"><p>Compare <span class="badge badge-our-reading">Our reading</span><!--cd-src:A--></p></aside>'
        '<h2>Steps</h2><p>File.</p><!--cd-source-panel--></div></main>'
    )
    layered = build.apply_sources_layer(
        sample, ["A"],
        render_tabs=lambda n: f'<nav class="page-tabs">{n}</nav>',
        render_section_link=lambda sid, n: f'<a href="#src-{sid}">({n})</a>',
        render_panel=lambda groups, n: '<section class="page-sources"></section>',
        tabs=True,
    )
    with_key = build.insert_label_key(layered, "/sources/#labels")
    assert with_key.index('class="lede"') < with_key.index('class="why-it-matters"')
    assert with_key.index('class="why-it-matters"') < with_key.index('class="label-key"')
    assert with_key.index('class="why-it-matters"') < with_key.index('class="page-tabs"')


def test_short_version_css_never_hides_page_fields() -> None:
    css = (ROOT / "static" / "css" / "site.css").read_text(encoding="utf-8")
    for block in re.findall(r"([^{}]*html\[data-brief=[^{}]*)\{\s*display:\s*none;?\s*\}", css):
        for cls in (".analogy", ".spec-strip", ".why-it-matters", ".lede"):
            assert cls not in block, f"brief mode hides {cls}"


class _LeadInDeeper(HTMLParser):
    _BRIEF_CLASSES = {"deeper", "brief-extra", "more"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.bad = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "aside" and "analogy" in classes and "analogy-inline" not in classes:
            if any(hidden for _, hidden in self.stack):
                self.bad += 1
        if tag not in self._VOID:
            is_hidden = bool(classes & self._BRIEF_CLASSES) or "data-deeper" in a
            self.stack.append((tag, is_hidden))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_lead_analogies_sit_outside_deeper_drawers(built_site: Path) -> None:
    for f in built_site.rglob("*.html"):
        parser = _LeadInDeeper()
        parser.feed(f.read_text(encoding="utf-8"))
        assert parser.bad == 0, f"{f}: a lead analogy is inside a brief-hidden container"


def test_every_configured_analogy_renders_in_compiled_html(built_site: Path) -> None:
    for p in PAGES:
        if p["slug"] == "404":
            continue
        route = p["route"].strip("/")
        html_path = built_site / (f"{route}/index.html" if route else "index.html")
        rendered = html_path.read_text(encoding="utf-8")
        for a in p.get("analogies") or []:
            expected_id = f'id="analogy-{a["id"]}"'
            assert expected_id in rendered, (
                f"page {p['slug']} defines analogy '{a['id']}' in data/pages.yaml "
                f"but {html_path} never renders m.analogy()"
            )


# ---------- Sitemap lastmod ----------

BASE_PAGE = (
    '<html><body><header>Edition {edition}</header><main id="main">'
    '<p class="checked">Sources checked: <time datetime="{checked}">{checked}</time>.</p>'
    '<p class="wrap print-masthead">CivicDesk · Sources checked {checked}</p>'
    '<nav class="page-tabs">Sources ({count})</nav>'
    '<h1>Court name change</h1><p>{body}</p>'
    '<p class="card-meta"><span class="checked-date">Sources checked {checked}</span></p>'
    '<p class="source-meta"><code>ID-A</code> · <span class="checked-date">Checked: <time>{checked}</time></span></p>'
    '<a href="https://www.courts.oregon.gov/{form}">Form</a> <img src="/static/a.webp?v={asset}" alt="Courthouse">'
    '<p class="section-sources-row"><a href="#src-group-a">{count} sources</a></p>'
    '<section class="page-sources"><h2>Sources</h2><p>Last checked {checked}</p></section>'
    "</main><footer>Built from {edition}</footer></body></html>"
)


def _page(**kw) -> str:
    vals = {"edition": "abc1234", "checked": "2026-11-20", "count": 3, "body": "File in your county.",
            "form": "packet", "asset": "11111111"}
    vals.update(kw)
    return BASE_PAGE.format(**vals)


def test_lastmod_hash_follows_content_not_check_dates() -> None:
    base = build.content_hash(_page())
    # Re-checking sources, rebuilding, and cache-busting assets never move lastmod.
    assert build.content_hash(_page(checked="2026-11-27", count=4, edition="def5678", asset="22222222")) == base
    # Page text and outside link targets do.
    assert build.content_hash(_page(body="File in any county.")) != base
    assert build.content_hash(_page(form="new-packet")) != base


def test_lastmod_hash_reads_past_self_closing_svg() -> None:
    # Regression: self-closing <path/> inside a skipped region (page tabs carry inline SVG icons)
    # once left the skip depth open, so the hash ignored everything after the first icon.
    page = (
        '<main><nav class="page-tabs"><a>Guide</a><a><svg class="icon"><circle r="1"/><path d="M0 0"/></svg>'
        'Sources</a></nav><p>{body}</p><img src="x.webp" alt="Courthouse"/></main>'
    )
    assert build.content_hash(page.format(body="File in your county.")) != build.content_hash(page.format(body="File in any county."))


def test_lastmod_hash_covers_all_of_main_and_skips_check_dates_on_every_page(built_site: Path) -> None:
    for f in sorted(built_site.rglob("*.html")):
        parser = build._ContentText()
        parser.feed(f.read_text(encoding="utf-8"))
        parser.close()
        assert parser._skip == 0 and parser._raw == 0, (
            f"{f}: a skipped or raw region never closed (skip={parser._skip}, raw={parser._raw})"
        )
        hashed_text = re.sub(r"\s+", " ", " ".join(parser.parts))
        assert "Sources checked" not in hashed_text and "Checked:" not in hashed_text, (
            f"{f}: source check date leaked into content_hash()"
        )


def test_sitemap_lastmod_comes_from_the_manifest(built_site: Path) -> None:
    report = json.loads((built_site / "build-report.json").read_text(encoding="utf-8"))
    by_slug = {p["slug"]: p for p in report["pages"]}
    assert all(p["lastmod_status"] in {"current", "updated", "stale", "new"} for p in report["pages"])
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ET.parse(built_site / "sitemap.xml")
    routes = {p["route"]: p["slug"] for p in PAGES}
    for url in tree.getroot().findall("s:url", ns):
        loc = url.find("s:loc", ns).text
        route = "/" + loc.split("civicdesk.net/", 1)[1]
        assert url.find("s:lastmod", ns).text == by_slug[routes[route]]["lastmod"]

    text = (ROOT / "data" / "lastmod.json").read_text(encoding="utf-8")
    manifest = build.load_lastmod()
    assert build.dump_lastmod(manifest) == text, "data/lastmod.json is not in canonical one-line-per-page form"
    assert set(manifest) <= {p["slug"] for p in PAGES}
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["date"]) for e in manifest.values())
