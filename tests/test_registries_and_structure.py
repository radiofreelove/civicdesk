from __future__ import annotations

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

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


def test_workers_static_assets_config_and_headers(built_site: Path) -> None:
    wrangler_cfg = json.loads((ROOT / "wrangler.jsonc").read_text(encoding="utf-8"))
    assert wrangler_cfg["name"] == "civicdesk"
    assert wrangler_cfg["build"]["command"] == "bash scripts/cloudflare_build.sh"
    assert wrangler_cfg["assets"]["directory"] == "./public"
    assert wrangler_cfg["assets"]["not_found_handling"] == "404-page"
    assert wrangler_cfg["previews"] == {}

    headers_txt = (built_site / "_headers").read_text(encoding="utf-8")
    assert "https://:version.:subdomain.workers.dev/*" in headers_txt
    assert "X-Robots-Tag: noindex" in headers_txt

    redirects_txt = (built_site / "_redirects").read_text(encoding="utf-8")
    for line in redirects_txt.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        source = stripped.split()[0]
        assert source.startswith("/"), f"_redirects source must be a path for Cloudflare Workers Static Assets: {line}"




