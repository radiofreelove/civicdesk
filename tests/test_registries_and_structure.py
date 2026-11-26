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
    assert not (ROOT / "templates" / "illustrations.html").exists(), (
        "templates/illustrations.html clip-art macro file must remain deleted"
    )

    design_doc = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    for required_term in (
        "Zero decorative clip-art",
        "--viz-surface",
        "--viz-fill-1",
        "--viz-accent",
        "tabular-nums",
        "WCAG 2.2 AAA",
    ):
        assert required_term in design_doc

    svg_open_re = re.compile(r"<svg\b([^>]*)>", re.I | re.S)
    svg_text_re = re.compile(r"<text\b[^>]*>(.*?)</text>", re.I | re.S)
    inline_style_attr_re = re.compile(r"\sstyle\s*=", re.I)
    flow_body_re = re.compile(r'<p class="viz-flow-body">(.*?)</p>', re.S)
    timeline_note_re = re.compile(r'<span class="viz-timeline-note"[^>]*>(.*?)</span>', re.S)
    strip_tags_re = re.compile(r"<[^>]+>")

    retired_clip_art_markers = (
        "illo-",
        "state-silhouette",
        "viz-medallion",
        "id-card-facsimile",
        "doc-mockup-id",
        "doc-mockup-court",
        "doc-mockup-birth",
        "doc-mockup-federal",
        "viz-trust-diagram",
        "viz-404-art",
        "viz-jargon-grid",
        "viz-channel-grid",
        "viz-theme-showcase",
    )

    pages_yaml = yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))
    assert len(pages_yaml) == 68
    html_files = sorted(built_site.rglob("*.html"))
    assert len(html_files) == 68

    for p in pages_yaml:
        route = p["route"]
        if p["slug"] == "404":
            html_path = built_site / "404.html"
        elif route == "/":
            html_path = built_site / "index.html"
        else:
            html_path = built_site / route.strip("/") / "index.html"
        raw = html_path.read_text(encoding="utf-8")

        assert "<style" not in raw.lower(), f"{route}: inline <style> forbidden by CSP"
        assert not inline_style_attr_re.search(raw), f"{route}: inline style=... attribute forbidden by CSP"

        for retired in retired_clip_art_markers:
            assert retired not in raw, f"{route}: retired clip-art/cartoon class {retired!r} found"

        # SVG <text> may only be short <=5-char single-token visual stamps inside aria-hidden SVGs
        for tm in svg_text_re.finditer(raw):
            stamp = strip_tags_re.sub("", tm.group(1)).strip()
            assert len(stamp) <= 5 and " " not in stamp, (
                f"{route}: multi-word or long SVG <text> forbidden (got {stamp!r}); keep prose in HTML"
            )

        # Concise <=120-char copy cap on flow cards and <=140-char cap on timeline notes
        for fm in flow_body_re.finditer(raw):
            plain = re.sub(r"\s+", " ", strip_tags_re.sub("", fm.group(1))).strip()
            assert len(plain) <= 120, f"{route}: .viz-flow-body exceeds 120 chars ({len(plain)}): {plain!r}"
        for tnm in timeline_note_re.finditer(raw):
            plain = re.sub(r"\s+", " ", strip_tags_re.sub("", tnm.group(1))).strip()
            assert len(plain) <= 140, f"{route}: .viz-timeline-note exceeds 140 chars ({len(plain)}): {plain!r}"

        for m in svg_open_re.finditer(raw):
            attrs = m.group(1)
            is_hidden = 'aria-hidden="true"' in attrs
            is_labeled_img = 'role="img"' in attrs and ("aria-label=" in attrs or "aria-labelledby=" in attrs)
            assert is_hidden or is_labeled_img, (
                f"{route}: <svg> must have aria-hidden=\"true\" or role=\"img\" with accessible label: {attrs!r}"
            )

    # Core purposeful structural diagrams render only on routes where they earn their space
    expected_markers = {
        "sources/index.html": ('class="viz-stat-strip"',),
        "medicaid/index.html": ('class="viz-timeline',),
        "medicaid/oregon/index.html": ('class="viz-stat-strip"', 'class="viz-timeline'),
        "medicaid/washington/index.html": ('class="viz-stat-strip"', 'class="viz-timeline'),
        "medicaid/utah/index.html": ('class="viz-stat-strip"', 'class="viz-timeline'),
        "medicaid/oregon/rules/index.html": ("toc-pills", 'class="viz-hours"', 'class="viz-calendar"', "data-deeper"),
        "medicaid/washington/rules/index.html": ("toc-pills", 'class="viz-hours"', 'class="viz-calendar"', "data-deeper"),
        "medicaid/utah/rules/index.html": ("toc-pills", 'class="viz-hours"', 'class="viz-calendar"', "data-deeper"),
        "medicaid/oregon/reasons/index.html": ('class="viz-split-board"',),
        "medicaid/washington/reasons/index.html": ('class="viz-split-board"',),
        "medicaid/utah/reasons/index.html": ('class="viz-split-board"',),
        "medicaid/oregon/letter/index.html": ("doc-mockup-letter", 'class="glossary-grid"'),
        "medicaid/washington/letter/index.html": ("doc-mockup-letter",),
        "medicaid/utah/letter/index.html": ("doc-mockup-letter", 'class="glossary-grid"'),
        "medicaid/oregon/denied/index.html": ('class="viz-swimlane"',),
        "medicaid/washington/denied/index.html": ('class="viz-swimlane"',),
        "medicaid/utah/denied/index.html": ('class="viz-swimlane"',),
        "medicaid/oregon/bills/index.html": ('class="viz-swimlane"',),
        "medicaid/washington/bills/index.html": ('class="viz-swimlane"',),
        "medicaid/utah/bills/index.html": ('class="viz-swimlane"',),
        "medicaid/oregon/log/index.html": ('class="viz-calendar"', 'class="viz-proof-tiles"'),
        "medicaid/washington/log/index.html": ('class="viz-calendar"', 'class="viz-proof-tiles"'),
        "medicaid/utah/log/index.html": ('class="viz-calendar"', 'class="viz-proof-tiles"'),
        "medicaid/oregon/check/index.html": ('id="checker-steps-track"', 'class="viz-progress"'),
        "medicaid/washington/check/index.html": ('id="checker-steps-track"', 'class="viz-progress"'),
        "medicaid/utah/check/index.html": ('id="checker-steps-track"', 'class="viz-progress"'),
        "medicaid/oregon/words/index.html": ('class="glossary-grid"',),
        "medicaid/washington/words/index.html": ('class="glossary-grid"',),
        "medicaid/utah/words/index.html": ('class="glossary-grid"',),
        "id-records/index.html": ('class="viz-flow',),
        "id-records/oregon/index.html": ('class="viz-stat-strip"', 'class="viz-flow'),
        "id-records/washington/index.html": ('class="viz-stat-strip"', 'class="viz-flow'),
        "id-records/utah/index.html": ('class="viz-stat-strip"', 'class="viz-flow'),
        "id-records/oregon/court/index.html": ('class="viz-stat-strip"', 'class="viz-flow', "toc-pills"),
        "id-records/washington/court/index.html": ('class="viz-stat-strip"', 'class="viz-flow', "toc-pills"),
        "id-records/utah/court/index.html": ('class="viz-stat-strip"', 'class="viz-flow', "toc-pills"),
        "id-records/oregon/birth/index.html": ('class="viz-stat-strip"', 'class="viz-split-board"', "toc-pills"),
        "id-records/washington/birth/index.html": ('class="viz-stat-strip"', 'class="viz-split-board"', "toc-pills"),
        "id-records/utah/birth/index.html": ('class="viz-stat-strip"',),
        "id-records/oregon/id/index.html": ('class="viz-stat-strip"',),
        "id-records/washington/id/index.html": ('class="viz-stat-strip"',),
        "id-records/utah/id/index.html": ('class="viz-stat-strip"',),
        "id-records/federal/index.html": ("passport-form-matrix",),
        "id-records/compare/index.html": ('class="viz-compare-board"',),
        "id-records/glossary/index.html": ('class="glossary-grid"',),
        "id-records/after/index.html": ('class="viz-flow',),
        "id-records/path/index.html": ('id="path-form"', 'class="result-emblem'),
    }
    for rel_file, markers in expected_markers.items():
        html = (built_site / rel_file).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in html, f"{rel_file}: missing expected visual marker {marker!r}"

    # Trust and utility pages stay clean and direct without forced diagram widgets
    for clean_page in ("privacy/index.html", "about/index.html", "accessibility/index.html", "404.html"):
        clean_html = (built_site / clean_page).read_text(encoding="utf-8")
        assert "doc-mockup" not in clean_html, f"{clean_page}: unexpected doc-mockup widget"
        assert 'class="viz-' not in clean_html, f"{clean_page}: unexpected forced .viz-* widget"


def test_drop_in_artist_logo_system_and_make_mark_custom_mode(tmp_path: Path, built_site: Path) -> None:
    import shutil

    build_mod = _load_script("civicdesk_build_brand", "scripts/build.py")
    make_mark = _load_script("civicdesk_make_mark_brand", "scripts/make_mark.py")

    # 1. Default built site keeps inline 5-stripe mark, visible wordmark, and generated apple-touch-icon.png
    assert (built_site / "static" / "apple-touch-icon.png").is_file()
    assert (built_site / "static" / "apple-touch-icon.png").stat().st_size > 0
    assert not (built_site / "static" / "brand" / ".gitkeep").exists()

    default_index = (built_site / "index.html").read_text(encoding="utf-8")
    assert 'class="brand-mark"' in default_index
    assert "apple-touch-icon.png?v=" in default_index
    assert '<span class="brand-name">CivicDesk</span>' in default_index

    # 2. Custom artist SVGs in both mark_and_wordmark and lockup_only modes build cleanly and pass PageAuditor
    custom_static = tmp_path / "static"
    shutil.copytree(ROOT / "static", custom_static)
    (custom_static / "brand").mkdir(parents=True, exist_ok=True)
    (custom_static / "brand" / "artist-light.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 44">'
        '<rect x="2" y="2" width="40" height="40" rx="8" fill="#1D70B8"/>'
        '<path d="M56 30h120" stroke="#111827" stroke-width="4"/></svg>\n',
        encoding="utf-8",
    )
    (custom_static / "brand" / "artist-dark.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 44">'
        '<rect x="2" y="2" width="40" height="40" rx="8" fill="#60A5FA"/>'
        '<path d="M56 30h120" stroke="#FFFFFF" stroke-width="4"/></svg>\n',
        encoding="utf-8",
    )

    base_site = yaml.safe_load((ROOT / "data" / "site.yaml").read_text(encoding="utf-8"))

    site_wordmark = tmp_path / "site-wordmark.yaml"
    site_wordmark.write_text(
        yaml.safe_dump(
            {
                **base_site,
                "brand": {
                    **base_site["brand"],
                    "custom_artwork": True,
                    "mode": "mark_and_wordmark",
                    "logo": "brand/artist-light.svg",
                    "logo_dark": "brand/artist-dark.svg",
                },
            }
        ),
        encoding="utf-8",
    )
    out_wordmark = tmp_path / "public-wordmark"
    assert (
        build_mod.main(
            [
                "--out",
                str(out_wordmark),
                "--site-yaml",
                str(site_wordmark),
                "--static-dir",
                str(custom_static),
            ]
        )
        == 0
    )
    wordmark_html = (out_wordmark / "index.html").read_text(encoding="utf-8")
    auditor_w = PageAuditor()
    auditor_w.feed(wordmark_html)
    assert not auditor_w.errors, f"mark_and_wordmark PageAuditor errors: {auditor_w.errors}"
    assert 'class="brand-mark"' not in wordmark_html
    assert 'class="brand-logo brand-logo-light" src="/static/brand/artist-light.svg?v=' in wordmark_html
    assert 'class="brand-logo brand-logo-dark" src="/static/brand/artist-dark.svg?v=' in wordmark_html
    assert '<span class="brand-name">CivicDesk</span>' in wordmark_html

    site_lockup = tmp_path / "site-lockup.yaml"
    site_lockup.write_text(
        yaml.safe_dump(
            {
                **base_site,
                "brand": {
                    **base_site["brand"],
                    "custom_artwork": True,
                    "mode": "lockup_only",
                    "logo": "brand/artist-light.svg",
                    "logo_dark": "brand/artist-dark.svg",
                },
            }
        ),
        encoding="utf-8",
    )
    out_lockup = tmp_path / "public-lockup"
    assert (
        build_mod.main(
            [
                "--out",
                str(out_lockup),
                "--site-yaml",
                str(site_lockup),
                "--static-dir",
                str(custom_static),
            ]
        )
        == 0
    )
    lockup_html = (out_lockup / "index.html").read_text(encoding="utf-8")
    auditor_l = PageAuditor()
    auditor_l.feed(lockup_html)
    assert not auditor_l.errors, f"lockup_only PageAuditor errors: {auditor_l.errors}"
    assert 'class="brand brand-lockup"' in lockup_html
    assert '<span class="brand-name vh sr-only">CivicDesk</span>' in lockup_html

    # 3. Invalid custom SVG / brand config cases are rejected cleanly
    (custom_static / "brand" / "no-viewbox.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>',
        encoding="utf-8",
    )
    (custom_static / "brand" / "ext-ref.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<image href="https://cdn.example.com/font.png" width="10" height="10"/></svg>',
        encoding="utf-8",
    )
    (custom_static / "brand" / "inline-style.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<style>.a{fill:red}</style><rect class="a" width="10" height="10"/></svg>',
        encoding="utf-8",
    )

    for bad_logo, expected_substr in (
        ("brand/no-viewbox.svg", "viewBox"),
        ("brand/ext-ref.svg", "external or unsafe reference"),
        ("brand/inline-style.svg", "<style>"),
        ("../outside.svg", "clean relative path"),
    ):
        errs: list[str] = []
        build_mod.validate_brand_config(
            {"brand": {"custom_artwork": True, "mode": "mark_and_wordmark", "logo": bad_logo}},
            custom_static,
            errs,
        )
        assert any(expected_substr in e for e in errs), f"Expected {expected_substr!r} for {bad_logo}, got {errs}"

    lockup_no_logo_errs: list[str] = []
    build_mod.validate_brand_config(
        {"brand": {"custom_artwork": True, "mode": "lockup_only", "logo": ""}},
        custom_static,
        lockup_no_logo_errs,
    )
    assert any("requires a non-empty brand.logo" in e for e in lockup_no_logo_errs)

    # 4. make_mark.py --check honors custom_artwork: true and protects custom files
    mark_ws = tmp_path / "mark-ws"
    (mark_ws / "data").mkdir(parents=True)
    (mark_ws / "static" / "brand").mkdir(parents=True)
    (mark_ws / "data" / "site.yaml").write_text(
        yaml.safe_dump(
            {
                "brand": {
                    "custom_artwork": True,
                    "mode": "lockup_only",
                    "logo": "brand/custom-logo.svg",
                    "favicon_svg": "brand/custom-fav.svg",
                    "apple_touch_icon": "brand/custom-touch.png",
                    "og_image": "brand/custom-og.png",
                }
            }
        ),
        encoding="utf-8",
    )
    assert make_mark.run_cli(["--check"], root=mark_ws) == 1  # missing files

    for fname in ("custom-logo.svg", "custom-fav.svg", "custom-touch.png", "custom-og.png"):
        (mark_ws / "static" / "brand" / fname).write_bytes(b"custom-artist-bytes")
    assert make_mark.run_cli(["--check"], root=mark_ws) == 0
    assert make_mark.run_cli([], root=mark_ws) == 1  # refuses to clobber custom files without --force-default


def test_ordered_list_badges_aria_hidden_and_dynamic_letter_pins(built_site: Path) -> None:
    badge_classes = {"mockup-pin", "viz-flow-num", "viz-progress-dot", "after-num"}

    class OrderedListBadgeParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.list_stack: list[str] = []
            self.unhidden_badges: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            a = {k: (v or "") for k, v in attrs}
            if tag in {"ol", "ul"}:
                self.list_stack.append(tag)
            if self.list_stack and self.list_stack[-1] == "ol" and tag == "span":
                classes = set(a.get("class", "").split())
                hit = classes & badge_classes
                if hit and a.get("aria-hidden") != "true":
                    self.unhidden_badges.append(f"{sorted(hit)}: {a}")

        def handle_endtag(self, tag: str) -> None:
            if tag in {"ol", "ul"} and self.list_stack:
                self.list_stack.pop()

    for html_path in sorted(built_site.rglob("*.html")):
        parser = OrderedListBadgeParser()
        parser.feed(html_path.read_text(encoding="utf-8"))
        assert not parser.unhidden_badges, (
            f"{html_path.relative_to(built_site)}: ordered-list number badge missing aria-hidden=\"true\": "
            f"{parser.unhidden_badges}"
        )

    sheet_block_re = re.compile(r'<div class="mockup-sheet"[^>]*>(.*?)<ol class="mockup-legend">', re.S)
    sheet_pin_re = re.compile(
        r'<div class="mockup-pin-spot[^"]*">\s*<span class="mockup-pin" aria-hidden="true">(\d+)</span>',
        re.S,
    )
    legend_item_re = re.compile(
        r'<li class="mockup-legend-item"[^>]*>\s*<span class="mockup-pin" aria-hidden="true">(\d+)</span>\s*'
        r'<div class="mockup-legend-copy">\s*<strong>(.*?)</strong>',
        re.S,
    )
    for st in ("oregon", "washington", "utah"):
        letter_html = (built_site / "medicaid" / st / "letter" / "index.html").read_text(encoding="utf-8")
        sheet_match = sheet_block_re.search(letter_html)
        assert sheet_match, f"{st} letter: missing .mockup-sheet block"
        sheet_html = sheet_match.group(1)
        sheet_pins = sheet_pin_re.findall(sheet_html)
        legend_items = legend_item_re.findall(letter_html)
        legend_pins = [num for num, _ in legend_items]
        legend_titles = [title.strip() for _, title in legend_items]
        assert sheet_pins == ["1", "2", "3", "4"], f"{st} letter: expected 4 numbered sheet pins, got {sheet_pins}"
        assert legend_pins == ["1", "2", "3", "4"], f"{st} letter: expected 4 numbered legend items, got {legend_pins}"
        for title in legend_titles:
            assert title and title not in sheet_html, (
                f"{st} letter: legend title {title!r} must not be printed verbatim on the sample notice sheet"
            )





