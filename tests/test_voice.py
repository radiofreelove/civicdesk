"""Voice check: interactive tool output describes published rules and never advises one reader."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_check_voice():
    spec = importlib.util.spec_from_file_location("check_voice", ROOT / "scripts" / "check_voice.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cv = _load_check_voice()


def test_tool_output_voice_is_clean(built_site: Path) -> None:
    hits = cv.scan(ROOT, built_site)
    assert not hits, "Advisory phrasing in tool output:\n" + "\n".join(hits)


def test_enforced_pattern_list_is_the_reviewed_set() -> None:
    assert sorted(cv.PATTERNS) == sorted([
        "you-should", "we-recommend", "your-best", "personalized", "my-steps", "apply-to-you",
        "your-steps", "my-results", "you-qualify", "fits-your-situation", "what-to-do-heading",
    ])


@pytest.mark.parametrize(("text", "name"), [
    ("You should file the packet first.", "you-should"),
    ("We recommend the court route.", "we-recommend"),
    ("Your best option is the court packet.", "your-best"),
    ("Personalized record-update order", "personalized"),
    ("Show my steps", "my-steps"),
    ("The new rules likely do not apply to you.", "apply-to-you"),
    ("The new rules may apply to you.", "apply-to-you"),
    ("Your steps, in order", "your-steps"),
    ("Print my results", "my-results"),
    ("You qualify for this exception.", "you-qualify"),
    ("You are likely exempt.", "you-qualify"),
    ("See only the steps that fit your situation.", "fits-your-situation"),
    ("What to do", "what-to-do-heading"),
    ("What you can do", "what-to-do-heading"),
])
def test_each_pattern_fires(text: str, name: str) -> None:
    assert name in cv.find_hits(text)


@pytest.mark.parametrize("text", [
    "The new rules do not apply to your age group.",
    "File fees are listed on the court's fee page.",
    "Use form OHA 2673, notarized, to change name, sex, or both.",
    "Tell OHP. No proof is needed.",
    "Skip: I am not updating a birth certificate",
    "You must ask the state for this exception.",
    "If you do not live with them and are not related, you need to give at least 80 hours of care a month.",
    "Here is what to do if a letter arrives.",
    "What DWS asks people in this group to do",
])
def test_rule_descriptions_pass(text: str) -> None:
    assert cv.find_hits(text) == []


def test_js_literals_skip_comments_and_decode_escapes() -> None:
    src = (
        '/* Never says "you are exempt" or "you qualify". */\n'
        '// "you should" in a line comment\n'
        'var url = "https://example.gov/a//b"; // trailing "my steps"\n'
        'var a = "Print my\\u00a0results";\n'
        "var b = 'Published steps';\n"
    )
    literals = cv.js_string_literals(src)
    texts = [t for _, t in literals]
    assert texts == ["https://example.gov/a//b", "Print my results", "Published steps"]
    assert [line for line, _ in literals] == [3, 4, 5]
    assert cv.find_hits(texts[1]) == ["my-results"]


def test_html_runs_join_inline_markup_and_skip_sources_panel() -> None:
    page = (
        "<header><p>you should not be scanned</p></header>"
        "<main><p>You <strong>should</strong>\n file it.</p>"
        '<button aria-label="Print my results">Print</button>'
        '<section class="page-sources"><h2>Sources</h2><p>Does this apply to you?</p></section>'
        "<template><li><h3>What to do</h3></li></template></main>"
    )
    hits = sorted({name for _, text in cv.html_runs(page) for name in cv.find_hits(text)})
    assert hits == ["my-results", "what-to-do-heading", "you-should"]
