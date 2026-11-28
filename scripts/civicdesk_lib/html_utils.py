"""HTML post-processing helpers: Sources layer, label key, punctuation binding, and content hashing."""
from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

from markupsafe import escape

BADGE_RE = re.compile(r'<span class="badge badge-(not-official|not-confirmed|our-reading)">')
CHECKED_P_RE = re.compile(r'(<p class="checked">.*?</p>)', re.S)
LEDE_P_RE = re.compile(r'(<p class="lede">.*?</p>)', re.S)
WHY_P_RE = re.compile(r'(<p class="why-it-matters">.*?</p>)', re.S)
PURPOSE_P_RE = re.compile(r'(<p class="purpose">.*?</p>)', re.S)

_SRC_LINK_RE = r'<a class="src" [^>]+><span class="src-label">Source:</span>(?:&nbsp;|\s+)[^<]+</a>'
_STATUS_BADGE_RE = r'<span class="badge badge-(?:official|not-official|not-confirmed|our-reading)">[^<]+</span>'

# --- Broadsheet sources layer ------------------------------------------------------------------
SRC_MARK_RE = re.compile(r"<!--cd-src:([A-Za-z0-9_.-]+)-->")
SOURCE_PANEL_MARK = "<!--cd-source-panel-->"
CHECKED_TIME_MARK = "<!--cd-checked-time-->"
CHECKED_TEXT_MARK = "<!--cd-checked-text-->"
CHECKED_OLDEST_TEXT_MARK = "<!--cd-checked-oldest-text-->"
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
HEAD_BLOCK_CLASSES = {"hero-split", "intro-split", "page-head", "fp-hero", "breadcrumb", "brief-strip", "label-key"}

_HASH_SKIP_CLASSES = {"checked", "checked-date", "print-masthead", "page-sources", "section-sources-row", "page-tabs"}
_HASH_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "col", "area", "base", "embed", "track"}

WORD_JOINER = "&#8288;"
INLINE_CLOSE_PUNCT_RE = re.compile(
    r"(</(?:a|abbr|b|bdi|code|em|i|kbd|q|small|span|strong|sup|time)>)(?=[.,;:!?)\]])"
)
RAW_TEXT_BLOCK_RE = re.compile(r"<!--.*?-->|<(script|style|pre|textarea)\b.*?</\1\s*>", re.S | re.I)
BADGE_LABEL_RE = re.compile(r'(<span class="(?:badge|tag)\b[^"]*">)([^<]*)(</span>)')


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
        self._dated: list[str] = []

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
    """Split a page body into sections at each <h2>."""
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


def _bind_segment(text: str) -> str:
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
    for pos, _order, text in sorted(inserts, key=lambda t: (t[0], t[1]), reverse=True):
        region = region[:pos] + text + region[pos:]

    region = strip_inline_citations(region)
    panel = str(render_panel(groups, len(used)))
    out = html[:body_start] + region + panel + html[panel_idx + len(SOURCE_PANEL_MARK):]
    return SRC_MARK_RE.sub("", out)
