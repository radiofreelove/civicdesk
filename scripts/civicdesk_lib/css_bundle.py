"""CSS bundling, font URL cache-busting, and string-safe CSS minification for compiled output."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

_FONT_URL_RE = re.compile(r'url\((["\']?)\.\./fonts/([^"\')?]+)(?:\?[^"\')]*)?\1\)')
_TOKEN_RE = re.compile(r'/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)


def rewrite_font_urls(fonts_css: str, hash_for_rel: Callable[[str], str]) -> str:
    """Append ?v=<sha256[:8]> to every ../fonts/<name> reference inside fonts.css."""
    def _sub(match: re.Match[str]) -> str:
        quote = match.group(1) or '"'
        fname = match.group(2)
        digest = hash_for_rel(f"fonts/{fname}")
        return f"url({quote}../fonts/{fname}?v={digest}{quote})"

    return _FONT_URL_RE.sub(_sub, fonts_css)


def minify_css(css_text: str) -> str:
    """Safely strip comments and collapse redundant whitespace while preserving strings and calc()."""
    no_comments = _TOKEN_RE.sub(lambda m: " " if m.group(0).startswith("/*") else m.group(0), css_text)
    parts: list[str] = []
    pos = 0

    def _compact_chunk(chunk: str) -> str:
        chunk = re.sub(r"\s+", " ", chunk)
        chunk = re.sub(r"\s*([{};,])\s*", r"\1", chunk)
        chunk = chunk.replace(";}", "}")
        return chunk

    for m in _TOKEN_RE.finditer(no_comments):
        parts.append(_compact_chunk(no_comments[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_compact_chunk(no_comments[pos:]))
    return "".join(parts).strip() + "\n"


def build_compiled_css(
    static_dir: Path,
    hash_for_rel: Callable[[str], str],
) -> tuple[str, str]:
    """Return (rewritten_fonts_css, combined_minified_site_css)."""
    fonts_path = static_dir / "css" / "fonts.css"
    site_path = static_dir / "css" / "site.css"
    fonts_raw = fonts_path.read_text(encoding="utf-8") if fonts_path.is_file() else ""
    rewritten_fonts = rewrite_font_urls(fonts_raw, hash_for_rel) if fonts_raw else ""
    site_raw = site_path.read_text(encoding="utf-8")
    combined = (rewritten_fonts + "\n" + site_raw) if rewritten_fonts else site_raw
    return rewritten_fonts, minify_css(combined)
