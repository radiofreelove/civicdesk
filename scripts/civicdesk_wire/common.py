"""Shared constants, sanitization, URL/date parsing, HTTP fetchers, and wire state helpers."""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import yaml

try:
    from http_retry import USER_AGENT, compute_page_fingerprint, fetch_with_retry
except ImportError:  # pragma: no cover - package-qualified import fallback
    from scripts.http_retry import (  # type: ignore[no-redef]
        USER_AGENT,
        compute_page_fingerprint,
        fetch_with_retry,
    )

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
WIRE_DIR = DATA_DIR / "wire"
TEMPLATES_DIR = ROOT / "templates"

REQUEST_TIMEOUT = 35
PAUSE_SECONDS = 0.30
MAX_ISSUE_BODY_CHARS = 60000
OVERLAP_DAYS = 3
MAX_LOOKBACK_DAYS = 60
MAX_STATE_REPORTED_IDS = 2000
URGENT_EFFECTIVE_DAYS = 45
DEFAULT_EMPTY_STREAK_THRESHOLD = 3

RADAR_MARKER_RE = re.compile(r"<!--\s*radar-item:([a-f0-9]{12})\s*-->")
WIRE_STATE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
COURTLISTENER_DOCKET_RE = re.compile(r"https?://(?:www\.)?courtlistener\.com/docket/(\d+)(?:/([^/?#]+))?")
TAG_RE = re.compile(r"<[^>]+>")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->|<!--|-->", re.DOTALL)
MD_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MD_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
AT_MENTION_RE = re.compile(r"(?<![A-Za-z0-9._%+-])@([A-Za-z0-9](?:[A-Za-z0-9_-]{0,38}))")
WS_RE = re.compile(r"\s+")

KEY_PARAM_RE = re.compile(r"([?&](?:key|api_key|token)=)[^&\s\"']+", re.IGNORECASE)
AUTH_HEADER_RE = re.compile(r"\b(Bearer|Token)\s+[A-Za-z0-9._~+/-]{6,}", re.IGNORECASE)
TAVILY_KEY_RE = re.compile(r"\btvly-[A-Za-z0-9_-]+")
GOOGLE_KEY_RE = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}")

FACT_CALL_RE = re.compile(r"\bfact(?:_src)?\(\s*['\"]([a-zA-Z0-9_-]+)['\"]\s*\)")
STATUTE_TOKEN_RE = re.compile(
    r"\b(?:"
    r"\d+\s+c\.?f\.?r\.?\s*(?:part\s+)?\d+(?:\.\d+)?"
    r"|435\.\d+|212\.\d+|422\.\d+|51\.\d+"
    r"|ors\s+\d+\.\d+|oar\s+\d+-\d+"
    r"|rcw\s+\d+\.\d+(?:\.\d+)?|wac\s+\d+-\d+"
    r"|26b-\d+-\d+(?:\.\d+)?|78a-\d+-\d+|63g-\d+-\d+|42-1-\d+|53-3-\d+|68-3-\d+(?:\.\d+)?"
    r"|ds-11|ds-82|ds-5504|ss-5"
    r"|orr\s+v\.?\s+trump"
    r")\b",
    re.IGNORECASE,
)
LEGISCAN_ENACTED_RE = re.compile(
    r"\b(signed by governor|approved by governor|governor signed|enacted|chapter\s+\d+|became law)\b",
    re.IGNORECASE,
)
COURT_ORDER_OPINION_RE = re.compile(
    r"\b(order|opinion|judgment|injunction|mandate|decree|memorandum decision|ruling|vacat\w+|stay\s+granted|stay\s+denied)\b",
    re.IGNORECASE,
)
NON_ORDER_FILING_RE = re.compile(
    r"\b(proposed order|motion for(?:\s+an?)?\s+order|stipulation and proposed order|"
    r"joint status report|status report|notice of appeal|brief|memorandum in support|memorandum in opposition)\b",
    re.IGNORECASE,
)

GEMINI_TRIAGE_MODELS = tuple(
    m.strip()
    for m in os.environ.get(
        "GEMINI_TRIAGE_MODELS",
        "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-2.5-flash-lite",
    ).split(",")
    if m.strip()
)
GEMINI_GROUNDING_MODELS = tuple(
    m.strip()
    for m in os.environ.get(
        "GEMINI_GROUNDING_MODELS",
        "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-2.5-flash-lite",
    ).split(",")
    if m.strip()
)

GEMINI_TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "kept": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "urgency": {"type": "STRING", "enum": ["urgent", "watch"]},
                    "jurisdiction": {"type": "STRING"},
                    "summary": {"type": "STRING"},
                    "check_hint": {"type": "STRING"},
                },
                "required": ["index", "urgency", "summary"],
            },
        }
    },
    "required": ["kept"],
}

GEMINI_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "bullets": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        }
    },
    "required": ["bullets"],
}

PREFERRED_DESK_ORDER = ("medicaid", "id-records", "public-records")

FEDERAL_REGISTER_FIELDS = (
    "document_number",
    "publication_date",
    "type",
    "action",
    "title",
    "abstract",
    "agencies",
    "html_url",
    "effective_on",
    "comments_close_on",
    "docket_ids",
    "regulation_id_numbers",
)


def redact_secrets(text: str, secrets: tuple[str, ...] = ()) -> str:
    cleaned = KEY_PARAM_RE.sub(r"\1[REDACTED]", text)
    cleaned = AUTH_HEADER_RE.sub(r"\1 [REDACTED]", cleaned)
    cleaned = TAVILY_KEY_RE.sub("[REDACTED]", cleaned)
    cleaned = GOOGLE_KEY_RE.sub("[REDACTED]", cleaned)
    for secret in secrets:
        tok = (secret or "").strip()
        if tok and len(tok) >= 4:
            cleaned = cleaned.replace(tok, "[REDACTED]")
    return cleaned


def strip_html(raw: str | None, max_len: int = 240) -> str:
    if not raw:
        return ""
    without_comments = HTML_COMMENT_RE.sub(" ", str(raw))
    text = html.unescape(TAG_RE.sub(" ", without_comments))
    text = WS_RE.sub(" ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def sanitize_issue_text(raw: str | None, max_len: int = 240) -> str:
    """Sanitize untrusted feed, web, or LLM text before rendering into a GitHub issue."""
    if not raw:
        return ""
    cleaned = HTML_COMMENT_RE.sub(" ", str(raw))
    cleaned = html.unescape(TAG_RE.sub(" ", cleaned))
    cleaned = MD_INLINE_LINK_RE.sub(r"\1 (\2)", cleaned)
    cleaned = MD_REF_LINK_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("`", "'").replace("[", "(").replace("]", ")")
    cleaned = AT_MENTION_RE.sub(r"`@\1`", cleaned)
    cleaned = WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
        # Ensure we never leave an unclosed inline backtick after truncation.
        if cleaned.count("`") % 2 == 1:
            cleaned = cleaned.replace("`", "")
    return cleaned


def sanitize_link_label(raw: str | None, max_len: int = 190) -> str:
    """Sanitize text placed inside `[label](url)` Markdown links."""
    cleaned = sanitize_issue_text(raw, max_len=max_len)
    return cleaned.replace("`", "")


def safe_url(url: str | None) -> str:
    if not url:
        return ""
    cleaned = str(url).strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if any(ch in cleaned for ch in (" ", "\n", "\r", "\t", ")")):
        cleaned = cleaned.replace(")", "%29").replace(" ", "%20")
    return cleaned


def normalize_url_key(url: str | None) -> str:
    cleaned = safe_url(url)
    if not cleaned:
        return ""
    parsed = urllib.parse.urlparse(cleaned)
    host = parsed.netloc.lower().removeprefix("www.")
    path = urllib.parse.unquote(parsed.path or "/").rstrip("/") or "/"
    query = parsed.query.lower()
    frag = parsed.fragment.lower()
    suffix = f"?{query}" if query and ("cite=" in query or "id=" in query) else ""
    if frag and ("entry-" in frag or "minute-entry-" in frag):
        return f"{host}{path.lower()}{suffix}#{frag}"
    return f"{host}{path.lower()}{suffix}"


def extract_host(url: str | None) -> str:
    cleaned = safe_url(url)
    if not cleaned:
        return ""
    return urllib.parse.urlparse(cleaned).netloc.lower().removeprefix("www.")


def host_matches_domain(host: str, domain: str) -> bool:
    h = host.lower().removeprefix("www.")
    d = domain.lower().removeprefix("www.").lstrip(".")
    return bool(h and d and (h == d or h.endswith("." + d)))


def item_fingerprint(desk: str, source_kind: str, unique_key: str) -> str:
    digest = hashlib.sha256(f"{desk}:{source_kind}:{unique_key.strip()}".encode("utf-8")).hexdigest()
    return digest[:12]


def _content_snippet_hash(title: str, snippet: str) -> str:
    norm = WS_RE.sub(" ", f"{title} {snippet}".lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]


def parse_any_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        try:
            return dt.date.fromisoformat(raw[:10])
        except ValueError:
            pass
    try:
        parsed_dt = email.utils.parsedate_to_datetime(raw)
        if parsed_dt.tzinfo is not None:
            parsed_dt = parsed_dt.astimezone(dt.timezone.utc)
        return parsed_dt.date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def default_fetcher(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> bytes:
    status, _resp_headers, payload = fetch_with_retry(
        url,
        headers=headers,
        method=method,
        body=body,
        timeout=REQUEST_TIMEOUT,
        max_retries=2,
        base_delay=1.0,
    )
    if status >= 400:
        raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, None)  # type: ignore[arg-type]
    return payload


def _fetch_with_meta(
    fetcher: Callable[..., Any],
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Invoke `fetcher` while preserving HTTP 304 and response headers when available."""
    if fetcher is default_fetcher:
        return fetch_with_retry(
            url,
            headers=headers,
            method="GET",
            timeout=REQUEST_TIMEOUT,
            max_retries=2,
            base_delay=1.0,
        )
    try:
        res = fetcher(url, headers=headers) if headers else fetcher(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            hdrs = {str(k).lower(): str(v).strip() for k, v in (exc.headers.items() if exc.headers else ())}
            return 304, hdrs, b""
        raise
    if isinstance(res, tuple) and len(res) == 3:
        st, hdrs, raw = res
        return int(st), {str(k).lower(): str(v) for k, v in dict(hdrs or {}).items()}, bytes(raw)
    return 200, {}, bytes(res)


def empty_wire_state(desk: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "desk": desk,
        "updated_at": "",
        "channels": {},
        "reported_ids": [],
        "legiscan_change_hashes": {},
        "tavily_url_hashes": {},
        "page_fingerprints": {},
        "wsr_checked_filings": [],
    }


def load_wire_state(state_path: Path | None, desk: str = "") -> dict[str, Any]:
    base = empty_wire_state(desk)
    if not state_path or not state_path.is_file():
        return base
    raw = state_path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return base
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Also accept Markdown body from a `wire:state` GitHub issue containing a ```json fence.
        m = WIRE_STATE_JSON_RE.search(raw)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError:
                parsed = None
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "body" in parsed[0]:
        body_str = str(parsed[0].get("body") or "")
        m = WIRE_STATE_JSON_RE.search(body_str)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError:
                parsed = None
        else:
            try:
                parsed = json.loads(body_str)
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return base
    for key in ("channels", "legiscan_change_hashes", "tavily_url_hashes", "page_fingerprints"):
        if isinstance(parsed.get(key), dict):
            base[key] = dict(parsed[key])
    if isinstance(parsed.get("reported_ids"), list):
        base["reported_ids"] = [str(x) for x in parsed["reported_ids"] if x][-MAX_STATE_REPORTED_IDS:]
    if isinstance(parsed.get("wsr_checked_filings"), list):
        base["wsr_checked_filings"] = [str(x) for x in parsed["wsr_checked_filings"] if x][-400:]
    if parsed.get("desk"):
        base["desk"] = str(parsed["desk"])
    if parsed.get("updated_at"):
        base["updated_at"] = str(parsed["updated_at"])
    return base


def format_state_issue_body(state: dict[str, Any]) -> str:
    desk = state.get("desk") or "wire"
    compact_json = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return (
        f"### CivicDesk Policy Wire Machine State (`{desk}`)\n\n"
        "> Automated read-only deduplication, lookback cursor, LegiScan `change_hash`, and registry "
        "fingerprint ledger maintained by `.github/workflows/radar.yml`. Do not edit manually.\n\n"
        f"```json\n{compact_json}\n```\n"
    )


def load_seen_markers(seen_issues_path: Path | None) -> set[str]:
    if not seen_issues_path or not seen_issues_path.is_file():
        return set()
    try:
        issues = json.loads(seen_issues_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    seen: set[str] = set()
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict):
            continue
        seen.update(RADAR_MARKER_RE.findall(str(issue.get("body") or "")))
        for comment in issue.get("comments") or []:
            if isinstance(comment, dict):
                seen.update(RADAR_MARKER_RE.findall(str(comment.get("body") or "")))
    return seen



VALID_STATE_REGISTER_KINDS = frozenset({"ut_rss", "rss", "atom", "wa_wsr", "or_olis_odata"})

BASE_STATUTE_PATTERNS = (
    r"\d+\s+c\.?f\.?r\.?\s*(?:part\s+)?\d+(?:\.\d+)?",
    r"ors\s+\d+\.\d+",
    r"oar\s+\d+-\d+",
    r"rcw\s+\d+\.\d+(?:\.\d+)?",
    r"wac\s+\d+-\d+",
)


def compile_statute_regex(extra_patterns: tuple[str, ...] = ()) -> re.Pattern[str]:
    parts = list(BASE_STATUTE_PATTERNS) + [str(p).strip() for p in extra_patterns if str(p).strip()]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)
