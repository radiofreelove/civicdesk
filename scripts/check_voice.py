#!/usr/bin/env python3
"""CivicDesk voice check: tool output describes published rules and never advises one reader.

Utah SCRP Rule 4-802(d) (formerly UCJA Rule 14-802; Utah Code § 78A-9-103), Washington GR 24(d),
and Oregon ORS 9.160 (OSB Formal Ethics Op. 2005-137) protect general legal information, not
advice applied to one person's facts, so the interactive tools say what a state's rules list and
what its agency asks. This check fails on advisory phrasing in four places:

1. The built <main> of the interactive tool pages (data/pages.yaml `tool: true`, plus /start/),
   including <template> result blocks and aria-label, alt, title, and placeholder text. The
   Sources panel is skipped because it quotes outside titles.
2. String literals in the tool scripts (comments are skipped).
3. Voice fields in data/pages.yaml: answer, why, analogy compare and boundary, and spec catch text.
4. The built <main> of non-tool guide pages for core advice patterns (GUIDE_PATTERNS), skipping
   the Sources panel.

Bare sentence-initial verbs ("File", "Use", "Tell", "Skip"), "you must", and "you need to" are
deliberately not enforced: in CivicDesk copy they describe what a rule requires.

Run after scripts/build.py (default build output: public/). pytest runs it via tests/test_voice.py.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Enforced patterns, matched case-insensitively against whitespace-normalized text.
PATTERNS: dict[str, str] = {
    "you-should": r"\byou\s+should\b",
    "we-recommend": r"\bwe\s+recommend\b",
    "we-advise": r"\bwe\s+(?:advise|suggest)\b",
    "your-best": r"\byour\s+best\b",
    "best-option": r"\bbest\s+(?:option|choice|way)\b",
    "personalized": r"\bpersonali[sz]ed\b",
    "my-steps": r"\bmy\s+steps\b",
    # Covers "do not / likely do not / may apply to you"; the trailing \b lets
    # "apply to your age group" (a rule about an age band) pass.
    "apply-to-you": r"\bapply\s+to\s+you\b",
    "your-steps": r"\byour\s+steps\b",
    "my-results": r"\bmy\s+results\b",
    "you-qualify": r"\byou\s+(?:likely\s+)?(?:qualify|are\s+(?:likely\s+)?exempt)\b",
    "you-are-eligible": r"\byou\s+are\s+(?:not\s+|likely\s+)?(?:in)?eligible\b",
    "you-do-not-need": r"\byou\s+(?:do\s+not|don['’]t)\s+need\b",
    "next-step-plan": r"\bnext[- ]steps?\s+(?:plan|for\s+you)\b",
    "fits-your-situation": r"\bfits?\s+your\s+situation\b",
    # Whole-string match only, so headings like "What to do" fail while sentences pass.
    "what-to-do-heading": r"^what\s+(?:to|you\s+can)\s+do$",
}
COMPILED = {name: re.compile(rx, re.IGNORECASE) for name, rx in PATTERNS.items()}

# Core advisory patterns enforced across non-tool guide pages as well as tools.
GUIDE_PATTERNS: tuple[str, ...] = (
    "you-should",
    "we-recommend",
    "we-advise",
    "your-best",
    "best-option",
    "personalized",
    "apply-to-you",
    "you-qualify",
    "you-are-eligible",
    "next-step-plan",
    "fits-your-situation",
)

TOOL_SCRIPTS = (
    "static/js/path.js",
    "static/js/checker.js",
    "static/js/checker-oregon.js",
    "static/js/checker-washington.js",
    "static/js/checker-utah.js",
    "static/js/start.js",
)
EXTRA_TOOL_SLUGS = ("start",)

# Tags that end a run of text. Inline tags (a, strong, em, span, code) do not, so a phrase split
# across inline markup is still checked as one piece.
BLOCK_TAGS = {
    "p", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section",
    "aside", "article", "header", "footer", "nav", "table", "tr", "td", "th", "caption", "summary",
    "details", "button", "label", "legend", "fieldset", "option", "select", "textarea", "template",
    "figure", "figcaption", "blockquote", "form", "br", "hr",
}
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "col", "area", "base", "embed", "track"}
TEXT_ATTRS = ("aria-label", "alt", "title", "placeholder")
SKIP_CLASSES = {"page-sources"}


def normalize(text: str) -> str:
    text = html.unescape(text).replace("\u2060", "").replace("\u00ad", "")
    return re.sub(r"\s+", " ", text).strip()


def find_hits(text: str, patterns: tuple[str, ...] | None = None) -> list[str]:
    norm = normalize(text)
    if not norm:
        return []
    names = patterns if patterns is not None else tuple(COMPILED)
    return [name for name in names if COMPILED[name].search(norm)]


class _MainText(HTMLParser):
    """Collect (line, text) runs from <main>, split at block-level tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.runs: list[tuple[int, str]] = []
        self._buf: list[str] = []
        self._buf_line = 0
        self._in_main = False
        self._skip_depth = 0
        self._raw_depth = 0

    def _flush(self) -> None:
        text = "".join(self._buf)
        if text.strip():
            self.runs.append((self._buf_line, text))
        self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._in_main = True
            return
        if not self._in_main:
            return
        if tag in ("script", "style"):
            self._raw_depth += 1
            return
        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        classes = set((dict(attrs).get("class") or "").split())
        if classes & SKIP_CLASSES:
            self._flush()
            self._skip_depth = 1
            return
        if tag in BLOCK_TAGS:
            self._flush()
        line = self.getpos()[0]
        for name, value in attrs:
            if name in TEXT_ATTRS and value and value.strip():
                self.runs.append((line, value))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags (<path/> in inline SVG icons, <input/>) open no element: they only
        # contribute attribute text and never change the Sources-panel skip depth.
        if not self._in_main or self._skip_depth or self._raw_depth:
            return
        line = self.getpos()[0]
        for name, value in attrs:
            if name in TEXT_ATTRS and value and value.strip():
                self.runs.append((line, value))

    def handle_endtag(self, tag: str) -> None:
        if not self._in_main:
            return
        if tag == "main":
            self._flush()
            self._in_main = False
            return
        if tag in ("script", "style"):
            self._raw_depth = max(0, self._raw_depth - 1)
            return
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._in_main or self._skip_depth or self._raw_depth:
            return
        if not self._buf:
            self._buf_line = self.getpos()[0]
        self._buf.append(data)


def html_runs(text: str) -> list[tuple[int, str]]:
    parser = _MainText()
    parser.feed(text)
    parser.close()
    parser._flush()
    return parser.runs


_REGEX_PREV_CHARS = set("([={,:;!?&|+-*~^%<>\n")
_REGEX_PREV_WORDS = {"return", "case", "in", "of", "typeof", "void", "delete", "throw", "new", "else", "do"}


def _starts_js_regex(prev_char: str, prev_word: str) -> bool:
    return not prev_char or prev_char in _REGEX_PREV_CHARS or prev_word in _REGEX_PREV_WORDS


def js_string_literals(source: str) -> list[tuple[int, str]]:
    """Return (line, value) for each string literal, skipping comments and regex literals."""
    out: list[tuple[int, str]] = []
    i, n, line = 0, len(source), 1
    prev_char = ""
    prev_word = ""
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif ch == "/" and nxt == "/":
            end = source.find("\n", i)
            i = n if end == -1 else end
        elif ch == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            line += source.count("\n", i, end)
            i = end
        elif ch == "/" and _starts_js_regex(prev_char, prev_word):
            j = i + 1
            in_class = False
            while j < n and source[j] != "\n":
                c = source[j]
                if c == "\\" and j + 1 < n:
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    j += 1
                    while j < n and source[j].isalpha():
                        j += 1
                    break
                j += 1
            else:
                j = i + 1
            i = j
            prev_char = "/"
            prev_word = ""
        elif ch in "\"'`":
            start_line, j, buf = line, i + 1, []
            while j < n and source[j] != ch:
                if source[j] == "\\" and j + 1 < n:
                    esc = source[j + 1]
                    if esc == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", source[j + 2:j + 6]):
                        buf.append(chr(int(source[j + 2:j + 6], 16)))
                        j += 6
                        continue
                    buf.append(" " if esc in "ntr" else esc)
                    j += 2
                    continue
                if source[j] == "\n":
                    line += 1
                buf.append(source[j])
                j += 1
            out.append((start_line, "".join(buf)))
            i = j + 1
            prev_char = ch
            prev_word = ""
        elif ch.isalpha() or ch in "_$":
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] in "_$"):
                j += 1
            prev_word = source[i:j]
            prev_char = source[j - 1]
            i = j
        else:
            prev_char = ch
            prev_word = ""
            i += 1
    return out


def page_voice_fields(page: dict) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for key in ("answer", "why"):
        if isinstance(page.get(key), str):
            fields.append((key, page[key]))
    for a in page.get("analogies") or []:
        if not isinstance(a, dict):
            continue
        for key in ("compare", "boundary"):
            if isinstance(a.get(key), str):
                fields.append((f"analogies[{a.get('id')}].{key}", a[key]))
    spec = page.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("catch"), dict) and isinstance(spec["catch"].get("text"), str):
        fields.append(("spec.catch.text", spec["catch"]["text"]))
    return fields


def built_file(built: Path, page: dict) -> Path:
    if page["slug"] == "404":
        return built / "404.html"
    if page["route"] == "/":
        return built / "index.html"
    return built / page["route"].strip("/") / "index.html"


def scan(root: Path = ROOT, built: Path | None = None) -> list[str]:
    """Return one "location: [pattern] text" line per hit."""
    built = built or (root / "public")
    pages = yaml.safe_load((root / "data" / "pages.yaml").read_text(encoding="utf-8"))
    hits: list[str] = []

    tool_slugs = {p["slug"] for p in pages if p.get("tool") or p["slug"] in EXTRA_TOOL_SLUGS}
    for p in pages:
        fpath = built_file(built, p)
        if not fpath.is_file():
            raise FileNotFoundError(f"{fpath} is missing; run python3 scripts/build.py first")
        rel = fpath.relative_to(built)
        if p["slug"] in tool_slugs:
            active_patterns = None
        elif p["slug"] == "404":
            continue
        else:
            active_patterns = GUIDE_PATTERNS
        for line, text in html_runs(fpath.read_text(encoding="utf-8")):
            for name in find_hits(text, active_patterns):
                hits.append(f"{built.name}/{rel}:{line}: [{name}] {normalize(text)[:120]}")

    for rel in TOOL_SCRIPTS:
        for line, text in js_string_literals((root / rel).read_text(encoding="utf-8")):
            for name in find_hits(text):
                hits.append(f"{rel}:{line}: [{name}] {normalize(text)[:120]}")

    for p in pages:
        for field, text in page_voice_fields(p):
            for name in find_hits(text):
                hits.append(f"data/pages.yaml ({p['slug']}: {field}): [{name}] {normalize(text)[:120]}")
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--built", type=Path, default=ROOT / "public", help="Build output directory (default: public/)")
    args = parser.parse_args(argv)
    try:
        hits = scan(ROOT, args.built)
    except FileNotFoundError as exc:
        print(f"check_voice: {exc}", file=sys.stderr)
        return 2
    if hits:
        print("VOICE CHECK FAILED: tool output must describe published rules, not advise one reader.", file=sys.stderr)
        for h in hits:
            print("  " + h, file=sys.stderr)
        return 1
    print(f"Voice check passed ({len(PATTERNS)} patterns).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
