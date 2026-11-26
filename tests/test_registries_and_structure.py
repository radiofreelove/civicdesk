from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
OPTIONAL_END = {"li", "p", "dt", "dd", "tr", "td", "th", "option", "thead", "tbody"}


class PageAuditor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.headings: list[tuple[int, str]] = []
        self.current_h: list[int | str] | None = None
        self.links: list[tuple[str, dict[str, str]]] = []
        self.lang: str | None = None
        self.meta_csp: str | None = None
        self.meta_referrer: str | None = None
        self.title = ""
        self.in_title = False
        self.label_for: set[str] = set()
        self.fields: list[dict] = []
        self.open_label = 0
        self.open_template = 0
        self.text_targets: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "meta":
            if a.get("http-equiv", "").lower() == "content-security-policy":
                self.meta_csp = a.get("content", "")
            if a.get("name", "").lower() == "referrer":
                self.meta_referrer = a.get("content", "")
        if a.get("id"):
            self.ids.append(a["id"])
        for name in a:
            if name.startswith("on"):
                self.errors.append(f"inline event attribute '{name}' on <{tag}>")
        if "style" in a:
            self.errors.append(f"inline style attribute on <{tag}>")
        if tag == "script" and "src" not in a:
            self.errors.append("inline <script>")
        if tag == "style":
            self.errors.append("inline <style>")
        if tag == "title":
            self.in_title = True
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and not self.open_template:
            self.current_h = [int(tag[1]), ""]
        if tag == "template":
            self.open_template += 1
        if tag == "label":
            self.open_label += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in {"input", "select", "textarea"} and a.get("type") not in {"hidden", "submit", "reset", "button"}:
            self.fields.append({
                "id": a.get("id"),
                "in_label": self.open_label > 0,
                "aria": a.get("aria-label") or a.get("aria-labelledby"),
            })
        if tag in {"a", "button"}:
            self.text_targets.append({"tag": tag, "attrs": a, "text": ""})
            if tag == "a":
                self.links.append((a.get("href", ""), a))
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.current_h is not None and not self.open_template:
            self.headings.append((int(self.current_h[0]), str(self.current_h[1]).strip()))
            self.current_h = None
        if tag == "label":
            self.open_label -= 1
        if tag == "template":
            self.open_template -= 1
        if tag in {"a", "button"} and self.text_targets:
            entry = self.text_targets.pop()
            name = entry["text"].strip() or entry["attrs"].get("aria-label", "").strip()
            if not name:
                self.errors.append(f"<{tag}> missing accessible name: {entry['attrs']}")
        if tag in VOID:
            return
        while self.stack and self.stack[-1] in OPTIONAL_END and self.stack[-1] != tag:
            self.stack.pop()
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"mismatched </{tag}>; stack tail={self.stack[-3:]}")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if self.current_h is not None:
            self.current_h[1] = str(self.current_h[1]) + data
        for entry in self.text_targets:
            entry["text"] += data


def test_five_stripe_mark_up_to_date() -> None:
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_mark.py"), "--check"], cwd=ROOT)
    assert res.returncode == 0
    fav = (ROOT / "static" / "favicon.svg").read_text(encoding="utf-8")
    for color in ("#111827", "#1D70B8", "#C85A70", "#FFFFFF"):
        assert color in fav
    assert fav.count("<rect") == 6  # 1 slate background + 5 Horizon stripes


def test_desk_partitioned_source_registries() -> None:
    med = yaml.safe_load((ROOT / "data" / "sources" / "medicaid.yaml").read_text(encoding="utf-8"))
    idr = yaml.safe_load((ROOT / "data" / "sources" / "id_records.yaml").read_text(encoding="utf-8"))
    allowed = {"official", "not_official", "not_confirmed", "our_reading"}

    assert len(med) >= 130
    assert len(idr) >= 75

    med_ids = {s["id"] for s in med}
    id_ids = {s["id"] for s in idr}
    assert all(i.startswith("MED-") for i in med_ids)
    assert all(i.startswith("ID-") for i in id_ids)
    assert not (med_ids & id_ids)

    for s in med + idr:
        assert s["status"] in allowed
        assert "automattic" not in s["url"].lower()


def test_all_built_pages_html_links_and_scoping(built_site: Path) -> None:
    pages_yaml = yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))
    route_to_file: dict[str, Path] = {}
    for p in pages_yaml:
        route = p["route"]
        if p["slug"] == "404":
            fpath = built_site / "404.html"
        elif route == "/":
            fpath = built_site / "index.html"
        else:
            fpath = built_site / route.strip("/") / "index.html"
        assert fpath.is_file(), f"Missing built file for {route}: {fpath}"
        route_to_file[route] = fpath

    for artifact in ("/sources.json", "/facts.json", "/build-report.json", "/sitemap.xml", "/robots.txt"):
        assert (built_site / artifact.lstrip("/")).is_file()
        route_to_file[artifact] = built_site / artifact.lstrip("/")

    parsed_by_route: dict[str, PageAuditor] = {}
    ids_by_route: dict[str, set[str]] = {}
    for route, fpath in route_to_file.items():
        if fpath.suffix != ".html":
            ids_by_route[route] = set()
            continue
        auditor = PageAuditor()
        auditor.feed(fpath.read_text(encoding="utf-8"))
        parsed_by_route[route] = auditor
        ids_by_route[route] = set(auditor.ids)

    for route, auditor in parsed_by_route.items():
        raw = route_to_file[route].read_text(encoding="utf-8")
        assert not auditor.errors, f"{route}: HTML errors: {auditor.errors}"
        assert auditor.lang == "en", f"{route}: missing lang=en"
        assert auditor.title.strip(), f"{route}: empty <title>"
        assert auditor.meta_referrer == "no-referrer", f"{route}: missing no-referrer"
        assert auditor.meta_csp and "connect-src 'none'" in auditor.meta_csp, f"{route}: weak CSP"
        assert "&lt;span" not in raw and "&lt;a " not in raw, f"{route}: escaped HTML markup"

        h1s = [text for level, text in auditor.headings if level == 1]
        assert len(h1s) == 1, f"{route}: expected 1 <h1>, found {h1s}"
        prev = 0
        for level, _ in auditor.headings:
            if prev:
                assert level <= prev + 1, f"{route}: heading skipped from h{prev} to h{level}"
            prev = level

        dupes = {i for i in auditor.ids if auditor.ids.count(i) > 1}
        assert not dupes, f"{route}: duplicate element IDs: {dupes}"

        for field in auditor.fields:
            assert field["in_label"] or (field["id"] and field["id"] in auditor.label_for) or field["aria"], (
                f"{route}: unlabeled field {field}"
            )

        for href, attrs in auditor.links:
            assert href, f"{route}: empty href on {attrs}"
            u = urlparse(href)
            if u.scheme in {"http", "https"}:
                assert "noreferrer" in attrs.get("rel", ""), f"{route}: external link missing rel=noreferrer: {href}"
                continue
            if u.scheme in {"mailto", "tel"}:
                continue
            # Resolve relative or root-relative link
            if not u.path:
                target_route = route
            elif u.path.startswith("/"):
                target_route = u.path
            else:
                base_dir = route if route.endswith("/") else "/"
                parts = [seg for seg in (base_dir + u.path).split("/") if seg and seg != "."]
                resolved: list[str] = []
                for seg in parts:
                    if seg == "..":
                        if resolved:
                            resolved.pop()
                    else:
                        resolved.append(seg)
                target_route = "/" + "/".join(resolved) + ("/" if u.path.endswith("/") else "")
            assert target_route in ids_by_route, f"{route}: broken internal link '{href}' -> '{target_route}'"
            if u.fragment:
                assert u.fragment in ids_by_route[target_route], (
                    f"{route}: missing anchor '#{u.fragment}' on '{target_route}'"
                )

    # Verify front door exact H1, equal-footing desk cards, and NO Leave quickly on /
    front_html = route_to_file["/"].read_text(encoding="utf-8")
    assert "<h1>Government rules and paperwork, explained in plain words.</h1>" in front_html
    assert 'href="/medicaid/"' in front_html
    assert 'href="/id-records/"' in front_html
    assert "data-quick-exit" not in front_html
    assert 'class="exit-bar"' not in front_html

    # Verify Leave quickly scoping across all routes
    for route, fpath in route_to_file.items():
        if fpath.suffix != ".html":
            continue
        html_text = fpath.read_text(encoding="utf-8")
        if route.startswith("/id-records/"):
            assert 'class="exit-bar"' in html_text, f"{route}: missing top .exit-bar"
            assert 'id="quick-exit"' in html_text, f"{route}: missing #quick-exit"
        else:
            assert 'class="exit-bar"' not in html_text, f"{route}: .exit-bar must NEVER render outside /id-records/*"
            is_safety_page = route in {
                "/medicaid/oregon/reasons/",
                "/medicaid/washington/reasons/",
                "/medicaid/utah/reasons/",
                "/medicaid/utah/immigration/",
            }
            if is_safety_page:
                assert 'class="safety-exit-link"' in html_text, f"{route}: missing domestic-safety Leave quickly link"
            else:
                assert "data-quick-exit" not in html_text, f"{route}: unexpected data-quick-exit outside scoped pages"

    # Verify cross-desk ID name-mismatch callout on /medicaid/ and state apply pages
    for med_route, expected_id_link in [
        ("/medicaid/", 'href="/id-records/"'),
        ("/medicaid/oregon/apply/", 'href="/id-records/oregon/"'),
        ("/medicaid/washington/apply/", 'href="/id-records/washington/"'),
        ("/medicaid/utah/apply/", 'href="/id-records/utah/"'),
    ]:
        med_html = route_to_file[med_route].read_text(encoding="utf-8")
        assert expected_id_link in med_html, f"{med_route}: missing cross-desk ID mismatch link {expected_id_link}"


def test_path_finder_template_rules() -> None:
    tpl = (ROOT / "templates" / "id-records" / "path.html").read_text(encoding="utf-8")
    blocks = dict(re.findall(r'<template id="(t-[a-z-]+)">(.*?)</template>', tpl, re.S))
    changes = {"marriage", "divorce", "name", "marker", "both"}
    who_vals = {"adult", "parent", "minor"}

    for live in ("or", "wa", "ut", "other"):
        assert f"t-marriage-{live}" in blocks
        assert f"t-divorce-{live}" in blocks
        assert f"t-court-{live}" in blocks
        assert f"t-id-{live}" in blocks
        assert f"t-help-{live}" in blocks
    for born in ("or", "wa", "ut", "other-us", "outside"):
        assert f"t-birth-{born}" in blocks
    assert "t-federal" in blocks and "t-after" in blocks

    for bid, body in blocks.items():
        rows = []
        for attrs, text in re.findall(r"<li([^>]*)>(.*?)</li>", body, re.S):
            ch_m = re.search(r'data-change="([^"]+)"', attrs)
            wh_m = re.search(r'data-who="([^"]+)"', attrs)
            ch_set = set(ch_m.group(1).split()) if ch_m else None
            wh_set = set(wh_m.group(1).split()) if wh_m else None
            if ch_set:
                assert ch_set <= changes, f"{bid}: invalid data-change {ch_set}"
            if wh_set:
                assert wh_set <= who_vals, f"{bid}: invalid data-who {wh_set}"
            rows.append((ch_set, wh_set, re.sub(r"\s+", " ", text).strip()[:60]))

        if bid == "t-court-or":
            continue
        for w in who_vals:
            shown_name = {t for c, wh, t in rows if (c is None or "name" in c) and (wh is None or w in wh)}
            shown_marker = {t for c, wh, t in rows if (c is None or "marker" in c) and (wh is None or w in wh)}
            shown_both = {t for c, wh, t in rows if (c is None or "both" in c) and (wh is None or w in wh)}
            assert (shown_name | shown_marker) <= shown_both, f"{bid} (who={w}): 'both' omits items from name/marker"


def _load_script(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_workers_static_assets_config_and_headers(built_site: Path) -> None:
    wrangler_cfg = json.loads((ROOT / "wrangler.jsonc").read_text(encoding="utf-8"))
    assert wrangler_cfg["name"] == "civicdesk"
    assert wrangler_cfg["build"]["command"] == "bash scripts/cloudflare_build.sh"
    assert wrangler_cfg["assets"]["directory"] == "./public"
    assert wrangler_cfg["assets"]["not_found_handling"] == "404-page"
    assert wrangler_cfg["previews"] == {}

    headers_txt = (built_site / "_headers").read_text(encoding="utf-8")
    assert "Strict-Transport-Security: max-age=31536000; includeSubDomains" in headers_txt
    assert "interest-cohort" not in headers_txt
    assert "/static/*" in headers_txt
    assert "Cache-Control: public, max-age=31536000, immutable" in headers_txt
    assert "https://:version.:subdomain.workers.dev/*" in headers_txt
    assert "https://:subdomain.workers.dev/*" in headers_txt
    assert "https://www.civicdesk.net/*" in headers_txt
    assert "X-Robots-Tag: noindex" in headers_txt

    redirects_txt = (built_site / "_redirects").read_text(encoding="utf-8")
    assert "/or-check /medicaid/oregon/check/ 301" in redirects_txt
    assert "/wa-check /medicaid/washington/check/ 301" in redirects_txt
    assert "/ut-check /medicaid/utah/check/ 301" in redirects_txt
    for line in redirects_txt.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        source = stripped.split()[0]
        assert source.startswith("/"), f"_redirects source must be a path for Cloudflare Workers Static Assets: {line}"


def test_report_mistake_mailto_and_github_options(built_site: Path) -> None:
    pages_yaml = yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))

    for p in pages_yaml:
        route = p["route"]
        if p["slug"] == "404":
            fpath = built_site / "404.html"
        elif route == "/":
            fpath = built_site / "index.html"
        else:
            fpath = built_site / route.strip("/") / "index.html"

        raw = fpath.read_text(encoding="utf-8")
        auditor = PageAuditor()
        auditor.feed(raw)

        # Top independent-bar 'Report a mistake' anchor must target #report on /about/ and #report-mistake elsewhere
        expected_top_anchor = "#report" if p["slug"] == "about" else "#report-mistake"
        assert f'<a href="{expected_top_anchor}">Report a mistake</a>' in raw, f"{route}: missing top Report a mistake anchor"

        # Every page must expose a direct mailto:corrections@civicdesk.net link pre-filling ONLY CivicDesk correction: <route>
        correction_links = [
            href for href, _ in auditor.links if href.startswith("mailto:corrections@civicdesk.net")
        ]
        assert correction_links, f"{route}: missing mailto:corrections@civicdesk.net link"
        expected_subject = f"CivicDesk correction: {route}"
        for href in correction_links:
            parsed = urlparse(href)
            assert parsed.scheme == "mailto"
            assert parsed.path == "corrections@civicdesk.net"
            params = parse_qs(parsed.query, keep_blank_values=True)
            assert list(params.keys()) == ["subject"], f"{route}: mailto must pre-fill ONLY subject, got {href}"
            assert params["subject"] == [expected_subject], f"{route}: expected subject={expected_subject!r}, got {params['subject']!r}"

        # GitHub issue option + plain privacy note must appear alongside the email option
        assert 'href="https://github.com/radiofreelove/civicdesk/issues"' in raw, f"{route}: missing GitHub issues link"
        assert "Never include personal health or case details." in raw, f"{route}: missing plain privacy note"
        assert "corrections at civicdesk.net" not in raw, f"{route}: stale obfuscated email text"

    # Specifically verify /about/ main body #report section has both contact options and the plain privacy warning
    about_main = (built_site / "about" / "index.html").read_text(encoding="utf-8").split("<main", 1)[1].split("</main>", 1)[0]
    assert '<h2 id="report">Report a mistake</h2>' in about_main
    assert 'href="mailto:corrections@civicdesk.net?subject=CivicDesk%20correction%3A%20%2Fabout%2F"' in about_main
    assert 'href="https://github.com/radiofreelove/civicdesk/issues"' in about_main
    assert "Never include personal health or case details." in about_main


def test_commit_sha_resolution_and_build_report(built_site: Path) -> None:
    build_mod = _load_script("civicdesk_build", "scripts/build.py")

    # Env var precedence: WORKERS_CI_COMMIT_SHA > GITHUB_SHA > CF_PAGES_COMMIT_SHA > git rev-parse > localdev
    assert build_mod.resolve_commit_sha(
        {
            "WORKERS_CI_COMMIT_SHA": "aaaaaaa111111122222223333333444444455555",
            "GITHUB_SHA": "bbbbbbb111111122222223333333444444455555",
            "CF_PAGES_COMMIT_SHA": "ccccccc111111122222223333333444444455555",
        },
        run_git=False,
    ) == "aaaaaaa111111122222223333333444444455555"
    assert build_mod.resolve_commit_sha(
        {
            "GITHUB_SHA": "bbbbbbb111111122222223333333444444455555",
            "CF_PAGES_COMMIT_SHA": "ccccccc111111122222223333333444444455555",
        },
        run_git=False,
    ) == "bbbbbbb111111122222223333333444444455555"
    assert build_mod.resolve_commit_sha(
        {"CF_PAGES_COMMIT_SHA": "ccccccc111111122222223333333444444455555"},
        run_git=False,
    ) == "ccccccc111111122222223333333444444455555"
    assert build_mod.resolve_commit_sha({}, run_git=False) == "localdev"

    git_resolved = build_mod.resolve_commit_sha({}, repo_root=ROOT, run_git=True)
    assert re.fullmatch(r"[0-9a-f]{40}", git_resolved), f"Expected 40-char git SHA, got {git_resolved!r}"

    report = json.loads((built_site / "build-report.json").read_text(encoding="utf-8"))
    assert report["commit"] != "localdev"
    assert re.fullmatch(r"[0-9a-f]{7,40}", report["commit"])
    assert report["commit_short"] == report["commit"][:7]
    assert report["commit_url"] == f"https://github.com/radiofreelove/civicdesk/commit/{report['commit']}"

    index_html = (built_site / "index.html").read_text(encoding="utf-8")
    assert f'<a href="{report["commit_url"]}" rel="noreferrer"><code>{report["commit_short"]}</code></a>' in index_html


def test_no_stranded_parentheses_around_citations_and_social_meta(built_site: Path) -> None:
    build_mod = _load_script("civicdesk_build_cite", "scripts/build.py")

    sample = (
        '<main><p>Deadline rule (<span class="src-unit"><a class="src" href="#src-a">'
        '<span class="src-label">Source:</span>&nbsp;A</a> '
        '<span class="badge badge-official">Official</span></span> '
        '<span class="src-unit"><a class="src" href="#src-b">'
        '<span class="src-label">Source:</span>&nbsp;B</a> '
        '<span class="badge badge-official">Official</span></span>)</p></main>'
    )
    grouped = build_mod.group_citations(sample)
    assert "(<span class=\"cite-row\"" not in grouped
    assert "</span>)</p>" not in grouped
    assert 'class="cite-row"' in grouped

    assert (built_site / "static" / "og-card.png").is_file()

    orphan_open = re.compile(r"\(\s*<span class=\"cite-row\"")
    orphan_close = re.compile(r"</span>\s*\)\s*</(?:p|li|td|dd|span)>")
    asset_hash_re = re.compile(r'/static/css/site\.css\?v=[0-9a-f]{8}"')

    for html_path in sorted(built_site.rglob("*.html")):
        raw = html_path.read_text(encoding="utf-8")
        assert not orphan_open.search(raw), f"{html_path}: stranded '(' before .cite-row"
        assert not orphan_close.search(raw), f"{html_path}: stranded ')' after .cite-row"
        assert asset_hash_re.search(raw), f"{html_path}: missing content-hash cache buster on site.css"
        assert '<meta property="og:title"' in raw, f"{html_path}: missing og:title"
        assert '<meta property="og:image"' in raw, f"{html_path}: missing og:image"
        assert "og-card.png?v=" in raw, f"{html_path}: missing cache-busted og-card.png"
        assert '<meta name="twitter:card" content="summary_large_image">' in raw


def test_404_page_completeness_and_no_quick_exit(built_site: Path) -> None:
    not_found = built_site / "404.html"
    assert not_found.is_file()
    raw = not_found.read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex">' in raw
    assert 'href="/id-records/federal/"' in raw
    assert 'href="/sources/"' in raw
    assert "data-quick-exit" not in raw


def test_check_brand_purge_from_non_root_cwd(tmp_path: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_brand_purge.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"check_brand_purge failed from {tmp_path}: {res.stderr}"
    assert "Brand purge check passed." in res.stdout


def test_verify_sources_dynamic_discovery_dedup_retry_and_bot_gated(tmp_path: Path, capsys) -> None:
    verify = _load_script("civicdesk_verify", "scripts/verify_sources.py")

    (tmp_path / "medicaid.yaml").write_text(
        yaml.safe_dump(
            [
                {"id": "MED-DUP-1", "url": "https://agency.example.gov/broken", "checked": "2026-11-20"},
                {"id": "MED-DUP-2", "url": "https://agency.example.gov/broken", "checked": "2026-11-20"},
                {"id": "MED-FLAKY", "url": "https://agency.example.gov/flaky", "checked": "2026-11-20"},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "id_records.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "ID-GATED-OK",
                    "url": "https://agency.example.gov/waf-403",
                    "checked": "2026-11-20",
                    "bot_gated": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "public_records.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "PR-UNGATED-403",
                    "url": "https://agency.example.gov/real-403",
                    "checked": "2026-01-01",
                }
            ]
        ),
        encoding="utf-8",
    )

    stale = verify.check_freshness(max_age_days=120, sources_dir=tmp_path, today=dt.date(2026, 11, 25))
    assert len(stale) == 1
    assert stale[0].startswith("PR-UNGATED-403:")

    calls: dict[str, int] = {}

    def fake_fetcher(url: str, _timeout: int) -> int:
        calls[url] = calls.get(url, 0) + 1
        if url.endswith("/broken"):
            return 404
        if url.endswith("/flaky"):
            return 503 if calls[url] == 1 else 200
        if url.endswith("/waf-403") or url.endswith("/real-403"):
            return 403
        return 200

    issues = verify.check_links(
        sources_dir=tmp_path,
        max_retries=2,
        pause_seconds=0.0,
        fetcher=fake_fetcher,
        sleeper=lambda _s: None,
    )
    out = capsys.readouterr().out
    assert "ID-GATED-OK" in out
    assert calls["https://agency.example.gov/broken"] == 1
    assert calls["https://agency.example.gov/flaky"] == 2
    assert any(i.startswith("MED-DUP-1 ") and "HTTP 404" in i for i in issues)
    assert any(i.startswith("MED-DUP-2 ") and "HTTP 404" in i for i in issues)
    assert not any("MED-FLAKY" in i for i in issues)
    assert not any("ID-GATED-OK" in i for i in issues)
    assert any(i.startswith("PR-UNGATED-403 ") and "HTTP 403" in i for i in issues)


def test_civic_horizon_visual_system_invariants(built_site: Path) -> None:
    design_doc = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    for required_term in ("--viz-surface", "--viz-accent", "tabular-nums", "WCAG 2.2 AAA"):
        assert required_term in design_doc

    svg_open_re = re.compile(r"<svg\b([^>]*)>", re.I | re.S)
    inline_style_attr_re = re.compile(r"\sstyle\s*=", re.I)

    html_files = sorted(built_site.rglob("*.html"))
    assert len(html_files) == 68
    total_svgs = 0

    for html_path in html_files:
        raw = html_path.read_text(encoding="utf-8")
        assert "<style" not in raw.lower(), f"{html_path}: inline <style> forbidden by CSP"
        assert not inline_style_attr_re.search(raw), f"{html_path}: inline style=... attribute forbidden by CSP"
        assert "<text" not in raw.lower(), f"{html_path}: SVG <text> forbidden (must use Hybrid HTML + SVG)"

        for m in svg_open_re.finditer(raw):
            total_svgs += 1
            attrs = m.group(1)
            is_hidden = 'aria-hidden="true"' in attrs
            is_labeled_img = 'role="img"' in attrs and ("aria-label=" in attrs or "aria-labelledby=" in attrs)
            assert is_hidden or is_labeled_img, (
                f"{html_path}: <svg> must have aria-hidden=\"true\" or role=\"img\" with accessible label: {attrs!r}"
            )

    assert total_svgs >= 200

    expected_markers = {
        "index.html": ('class="hero-stage"', 'class="horizon-svg"', 'class="desk-emblem"', 'class="viz-flow'),
        "start/index.html": ('class="choice-icon"', 'class="guide-dir-icon"'),
        "medicaid/index.html": ('class="viz-timeline',),
        "medicaid/oregon/index.html": ('class="viz-timeline',),
        "medicaid/washington/index.html": ('class="viz-timeline',),
        "medicaid/utah/index.html": ('class="viz-timeline',),
        "medicaid/oregon/rules/index.html": ('class="viz-hours"', 'class="viz-hours-svg"'),
        "medicaid/washington/rules/index.html": ('class="viz-hours"', 'class="viz-hours-svg"'),
        "medicaid/utah/rules/index.html": ('class="viz-hours"', 'class="viz-hours-svg"'),
        "medicaid/oregon/reasons/index.html": ('class="viz-grid', "data-deeper"),
        "medicaid/washington/reasons/index.html": ('class="viz-grid', "data-deeper"),
        "medicaid/utah/reasons/index.html": ('class="viz-grid', "data-deeper"),
        "medicaid/oregon/check/index.html": ('id="checker-steps-track"', 'class="viz-progress"'),
        "medicaid/washington/check/index.html": ('id="checker-steps-track"', 'class="viz-progress"'),
        "medicaid/utah/check/index.html": ('id="checker-steps-track"', 'class="viz-progress"'),
        "id-records/index.html": ('class="viz-flow',),
        "id-records/oregon/index.html": ('class="viz-flow',),
        "id-records/washington/index.html": ('class="viz-flow',),
        "id-records/utah/index.html": ('class="viz-flow',),
        "id-records/federal/index.html": ('class="viz-grid',),
        "id-records/after/index.html": ('class="viz-flow',),
        "id-records/path/index.html": ('class="choice-icon"', 'class="result-emblem'),
    }
    for rel_file, markers in expected_markers.items():
        html = (built_site / rel_file).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in html, f"{rel_file}: missing expected visual marker {marker!r}"





