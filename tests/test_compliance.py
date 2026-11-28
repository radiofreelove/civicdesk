"""Compliance guardrails: privacy promises backed by headers, tool notices, and intake hygiene."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

TOOL_NOTICE = (
    "No person sees your answers. Your answers stay in this browser and are never sent anywhere. "
    "Using this tool does not create a lawyer-client relationship."
)
NONLAWYER = (
    "CivicDesk is maintained by nonlawyers, not attorneys or licensed legal paraprofessionals, "
    "and nothing on this site is legal advice."
)


def _built_file(built: Path, page: dict) -> Path:
    if page["slug"] == "404":
        return built / "404.html"
    if page["route"] == "/":
        return built / "index.html"
    return built / page["route"].strip("/") / "index.html"


def _csp_directives(policy: str) -> dict[str, str]:
    out = {}
    for part in policy.split(";"):
        bits = part.strip().split(None, 1)
        if bits:
            out[bits[0]] = bits[1].strip() if len(bits) > 1 else ""
    return out


def test_csp_backs_the_never_sent_anywhere_promise(built_site: Path) -> None:
    # "Your answers stay in this browser and are never sent anywhere" holds only while scripts can
    # open no connections and forms can submit nowhere, in both the header and the meta fallback.
    for headers_path in (ROOT / "site-root" / "_headers", built_site / "_headers"):
        text = headers_path.read_text(encoding="utf-8")
        m = re.search(r"^/\*\n((?:[ \t]+.*\n?)+)", text, re.M)
        assert m, f"{headers_path}: missing /* header block"
        csp = re.search(r"Content-Security-Policy:\s*(.+)", m.group(1))
        assert csp, f"{headers_path}: /* block has no Content-Security-Policy"
        d = _csp_directives(csp.group(1))
        assert d.get("connect-src") == "'none'", f"{headers_path}: connect-src must be 'none', got {d.get('connect-src')!r}"
        assert d.get("form-action") == "'none'", f"{headers_path}: form-action must be 'none', got {d.get('form-action')!r}"

    spec = importlib.util.spec_from_file_location("civicdesk_build", ROOT / "scripts" / "build.py")
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    d = _csp_directives(build.CSP_META)
    assert d.get("connect-src") == "'none'" and d.get("form-action") == "'none'"


def test_tool_notice_on_every_interactive_tool(built_site: Path) -> None:
    pages = yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))
    with_tools = 0
    for p in pages:
        raw = _built_file(built_site, p).read_text(encoding="utf-8")
        if not re.search(r'static/js/(?:path|checker|start)\.js', raw):
            continue
        with_tools += 1
        main = raw.split("<main", 1)[1].split("</main>", 1)[0]
        notice = re.search(r'<p class="tool-notice[^"]*" role="note">(.*?)</p>', main, re.S)
        assert notice, f"{p['route']}: interactive tool page is missing macros.tool_notice()"
        text = re.sub(r"<[^>]+>", " ", notice.group(1))
        assert TOOL_NOTICE in re.sub(r"\s+", " ", text).strip(), f"{p['route']}: tool notice text changed"
    assert with_tools == 5, f"expected /start/, the path finder, and 3 state checks, found {with_tools}"


def test_about_nonlawyer_statement_without_workflow_disclosure(built_site: Path) -> None:
    about = (built_site / "about" / "index.html").read_text(encoding="utf-8")
    main = re.sub(r"\s+", " ", about.split("<main", 1)[1].split("</main>", 1)[0])
    assert NONLAWYER in main
    assert "not a substitute for advice from a licensed attorney" in main
    assert "no attorney-client evidentiary privilege" in main
    assert "SIL Open Font License, Version 1.1" in main
    assert "Source Serif 4" in main and "Atkinson Hyperlegible Next" in main
    # Scope boundary: the About page carries no AI or content-production workflow disclosure.
    assert not re.search(r"\b(?:AI|artificial intelligence|language model|LLM|Claude|Antigravity)\b", main)


def test_independence_wording_names_no_specific_agency(built_site: Path) -> None:
    raw = (built_site / "index.html").read_text(encoding="utf-8")
    assert "Not run or endorsed by any court or government agency." in raw
    assert "It is not run or endorsed by any court or government agency and does not provide legal advice." in raw
    for path in (ROOT / "templates" / "base.html", ROOT / "templates" / "about.html", ROOT / "README.md"):
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "run or endorsed" in ln]
        assert lines, f"{path.name}: missing the 'run or endorsed' independence wording"
        for ln in lines:
            assert not re.search(r"\b(?:ODHS|OHA|DHS|HCA|DWS)\b", ln), f"{path.name}: independence wording should stay generic"


def test_issue_intake_templates() -> None:
    cfg = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8"))
    assert cfg["blank_issues_enabled"] is False
    assert [c["url"] for c in cfg["contact_links"]] == ["https://civicdesk.net/about/#report"]

    form = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "correction.yml").read_text(encoding="utf-8"))
    assert form["labels"] == ["correction"]
    checks = [b for b in form["body"] if b["type"] == "checkboxes"]
    assert checks and all(opt.get("required") is True for b in checks for opt in b["attributes"]["options"])
    intro = form["body"][0]["attributes"]["value"]
    assert "Issues here are public." in intro and "cannot answer questions about individual situations" in intro


def test_scheduled_workflows_run_evenings_pacific() -> None:
    # 02:00-02:59 UTC is 6-7 p.m. PST and 7-8 p.m. PDT, outside Oregon state work hours year-round.
    for name in ("radar.yml", "weekly.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        crons = re.findall(r'-\s*cron:\s*"([^"]+)"', text)
        assert crons, name
        for cron in crons:
            minute, hour = cron.split()[:2]
            assert hour == "2", f"{name}: cron {cron!r} should fire at 02:xx UTC (evening Pacific)"
