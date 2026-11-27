#!/usr/bin/env python3
"""CivicDesk Policy Wire (`scripts/policy_radar.py`).

Declarative, multi-desk civic news wire and primary-source radar driven by
`data/wire/*.yaml`. Polls keyless registry page fingerprints (`poll_page_watch`),
primary official endpoints (Federal Register API v1 + Public Inspection,
eCFR Versioner API v1, official state registers & legislative feeds,
CourtListener RECAP Atom feeds + REST v4 dockets/opinions), and triaged
policy/legislative/web leads (LegiScan API + `getBill` calendar/state_link,
Tavily `.gov`/court search paired with Gemini structured JSON triage, and
States Newsroom / KFF RSS), cross-referencing every dispatch against
CivicDesk's live source registries, tracked facts (`data/facts.yaml`), and
built routes (`data/pages.yaml`).

Read-only toward repository contents: never modifies repository data, templates,
or built assets. Always sends `User-Agent: CivicDesk-Verify/1.0`.
"""
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

ROOT = Path(__file__).resolve().parent.parent
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


def _resolve_repo_path(raw_path: str | Path, base_root: Path = ROOT) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else (base_root / p)


def load_wire_profiles(
    wire_dir: Path = WIRE_DIR,
    *,
    include_disabled: bool = True,
    base_root: Path = ROOT,
) -> dict[str, dict[str, Any]]:
    if not wire_dir.is_dir():
        return {}
    raw_profiles: list[dict[str, Any]] = []
    for path in sorted(wire_dir.glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not loaded.get("desk"):
            continue
        enabled = bool(loaded.get("enabled", True))
        if not enabled and not include_disabled:
            continue
        desk_slug = str(loaded["desk"]).strip()
        channels = loaded.get("channels") or {}
        pw_cfg = channels.get("page_watch") or {}
        sr_cfg = channels.get("state_registers") or ()
        cl_cfg = channels.get("courtlistener") or {}
        rss_cfg = channels.get("rss") or {}
        fr_cfg = channels.get("federal_register") or {}
        leg_cfg = channels.get("legiscan") or {}
        tav_cfg = channels.get("tavily") or {}

        jurisdictions = loaded.get("jurisdictions") or {}
        tavily_domains: list[str] = []
        registry_only_domains: list[str] = []
        for jur_meta in jurisdictions.values():
            if isinstance(jur_meta, dict):
                for dom in jur_meta.get("domains") or []:
                    d = str(dom).strip().lower()
                    if d and d not in tavily_domains:
                        tavily_domains.append(d)
                    if d and d not in registry_only_domains:
                        registry_only_domains.append(d)
        for dom in tav_cfg.get("extra_domains") or []:
            d = str(dom).strip().lower()
            if d and d not in tavily_domains:
                tavily_domains.append(d)

        tracked_bills: list[dict[str, str]] = []
        for tb in leg_cfg.get("tracked_bills") or ():
            if isinstance(tb, int | str) and str(tb).strip():
                tracked_bills.append({"bill_id": str(tb).strip(), "state": "", "label": ""})
            elif isinstance(tb, dict) and tb.get("bill_id"):
                tracked_bills.append(
                    {
                        "bill_id": str(tb["bill_id"]).strip(),
                        "state": str(tb.get("state") or "").strip().upper(),
                        "label": str(tb.get("label") or "").strip(),
                    }
                )

        state_registers: list[dict[str, Any]] = []
        for sr in sr_cfg if isinstance(sr_cfg, list | tuple) else ():
            if not isinstance(sr, dict) or not sr.get("url") or not sr.get("kind"):
                continue
            pat_str = str(sr.get("match_pattern") or "").strip()
            state_registers.append(
                {
                    "kind": str(sr["kind"]).strip().lower(),
                    "jurisdiction": str(sr.get("jurisdiction") or "US").strip().upper(),
                    "name": str(sr.get("name") or sr["url"]).strip(),
                    "url": str(sr["url"]).strip(),
                    "match_re": re.compile(pat_str, re.IGNORECASE) if pat_str else None,
                }
            )

        profile: dict[str, Any] = {
            "desk": desk_slug,
            "enabled": enabled,
            "label": str(loaded.get("label") or desk_slug),
            "registry": _resolve_repo_path(str(loaded.get("registry") or f"data/sources/{desk_slug}.yaml"), base_root),
            "source_prefix": str(loaded.get("source_prefix") or f"{desk_slug.upper()}-"),
            "route_prefix": str(loaded.get("route_prefix") or f"/{desk_slug}/"),
            "editorial_brief": WS_RE.sub(" ", str(loaded.get("editorial_brief") or "")).strip(),
            "jurisdictions": jurisdictions,
            "channels": channels,
            "page_watch_enabled": bool(pw_cfg.get("enabled", True)) if isinstance(pw_cfg, dict) else bool(pw_cfg),
            "state_registers": tuple(state_registers),
            "default_dockets": {str(k): str(v) for k, v in (cl_cfg.get("watched_dockets") or {}).items()},
            "federal_register_terms": tuple(str(t) for t in (fr_cfg.get("terms") or ())),
            "fr_public_inspection": bool(fr_cfg.get("public_inspection", True)),
            "fr_always_keep_re": (
                re.compile(str(fr_cfg["always_keep_pattern"]), re.IGNORECASE)
                if fr_cfg.get("always_keep_pattern")
                else None
            ),
            "fr_must_match_re": (
                re.compile(str(fr_cfg["must_match_pattern"]), re.IGNORECASE)
                if fr_cfg.get("must_match_pattern")
                else None
            ),
            "fr_context_re": (
                re.compile(str(fr_cfg["context_pattern"]), re.IGNORECASE)
                if fr_cfg.get("context_pattern")
                else None
            ),
            "ecfr_watch": tuple(
                (
                    str(entry["title"]),
                    str(entry["part"]),
                    str(entry.get("label") or f"{entry['title']} CFR Part {entry['part']}"),
                    tuple(str(p) for p in (entry.get("section_prefixes") or ())),
                )
                for entry in (channels.get("ecfr") or ())
                if isinstance(entry, dict) and entry.get("title") and entry.get("part")
            ),
            "courtlistener_v4_queries": tuple(
                {
                    "search_type": str(q.get("search_type") or "r"),
                    "label": str(q.get("label") or "CourtListener v4"),
                    "query": str(q["query"]),
                }
                for q in (cl_cfg.get("v4_queries") or ())
                if isinstance(q, dict) and q.get("query")
            ),
            "legiscan_min_relevance": int(leg_cfg.get("min_relevance") or 50),
            "legiscan_year": str(leg_cfg.get("year") or "3"),
            "legiscan_tracked_bills": tuple(tracked_bills),
            "legiscan_queries": tuple(
                (str(q["state"]), str(q["query"]))
                for q in (leg_cfg.get("queries") or ())
                if isinstance(q, dict) and q.get("state") and q.get("query")
            ),
            "rss_feeds": tuple(
                (str(f["name"]), str(f["url"]), bool(f.get("prefiltered", False)))
                for f in (rss_cfg.get("feeds") or ())
                if isinstance(f, dict) and f.get("name") and f.get("url")
            ),
            "news_primary_re": (
                re.compile(str(rss_cfg["primary_pattern"]), re.IGNORECASE)
                if rss_cfg.get("primary_pattern")
                else None
            ),
            "news_context_re": (
                re.compile(str(rss_cfg["context_pattern"]), re.IGNORECASE)
                if rss_cfg.get("context_pattern")
                else None
            ),
            "tavily_domains": tuple(tavily_domains),
            "tavily_registry_domains": tuple(registry_only_domains or tavily_domains),
            "tavily_queries": tuple(str(q) for q in (tav_cfg.get("queries") or ())),
            "tavily_undated_queries": tuple(str(q) for q in (tav_cfg.get("undated_queries") or ())),
            "tavily_search_depth": str(tav_cfg.get("search_depth") or "basic"),
            "tavily_max_results": int(tav_cfg.get("max_results_per_query") or 6),
            "tavily_min_score_dated": float(tav_cfg.get("min_score_dated") or 0.55),
            "tavily_min_score_undated": float(tav_cfg.get("min_score_undated") or 0.65),
            "profile_path": path,
        }
        raw_profiles.append(profile)

    def _sort_key(p: dict[str, Any]) -> tuple[int, str]:
        slug = p["desk"]
        idx = PREFERRED_DESK_ORDER.index(slug) if slug in PREFERRED_DESK_ORDER else len(PREFERRED_DESK_ORDER)
        return (idx, slug)

    raw_profiles.sort(key=_sort_key)
    return {p["desk"]: p for p in raw_profiles}


DESK_CONFIG: dict[str, dict[str, Any]] = load_wire_profiles(include_disabled=False)


def list_active_desks(wire_dir: Path = WIRE_DIR, *, include_disabled: bool = False) -> list[str]:
    profiles = load_wire_profiles(wire_dir, include_disabled=include_disabled)
    return list(profiles.keys())


def _get_desk_cfg(desk: str, profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    catalog = profiles if profiles is not None else DESK_CONFIG
    if desk in catalog:
        return catalog[desk]
    all_profiles = load_wire_profiles(include_disabled=True)
    if desk in all_profiles:
        return all_profiles[desk]
    raise ValueError(f"Unknown desk: {desk!r} (available: {', '.join(sorted(all_profiles))})")


def discover_dockets(desk: str, profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    cfg = _get_desk_cfg(desk, profiles)
    dockets = dict(cfg["default_dockets"])
    reg_path: Path = cfg["registry"]
    if reg_path.is_file():
        entries = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or []
        for s in entries if isinstance(entries, list) else []:
            m = COURTLISTENER_DOCKET_RE.search(str(s.get("url") or ""))
            if m and m.group(1) not in dockets:
                dockets[m.group(1)] = str(s.get("short") or s.get("title") or f"Docket {m.group(1)}")
    return dockets


def _extract_statute_tokens(text: str) -> set[str]:
    return {WS_RE.sub(" ", m.group(0).lower()).strip() for m in STATUTE_TOKEN_RE.finditer(text or "")}


MINISTERIAL_DOCKET_RE = re.compile(
    r"\b(pro hac vice|notice of appearance|withdraw as attorney|corporate disclosure|"
    r"case opening notice|summons issued|case assigned/reassigned|case assigned to district judge|"
    r"certificate of service|affidavit of service)\b",
    re.IGNORECASE,
)
SUPPORTING_FILING_RE = re.compile(r"^(?:declaration|affidavit|exhibit|proposed order)s?$", re.IGNORECASE)


def _is_court_order_or_opinion(title: str, summary: str, *, search_type: str = "") -> bool:
    if search_type == "o":
        return True
    combined = f"{title} {summary}".strip()
    if NON_ORDER_FILING_RE.search(combined):
        return False
    return bool(COURT_ORDER_OPINION_RE.search(combined))


def build_desk_impact_index(
    cfg: dict[str, Any],
    *,
    data_dir: Path = DATA_DIR,
    templates_dir: Path = TEMPLATES_DIR,
) -> dict[str, Any]:
    prefix = cfg["source_prefix"]
    source_id_re = re.compile(rf"\b{re.escape(prefix)}[A-Z0-9-]+\b")

    reg_path: Path = cfg["registry"]
    sources_by_id: dict[str, dict[str, Any]] = {}
    by_norm_url: dict[str, list[str]] = {}
    by_docket_id: dict[str, list[str]] = {}
    by_token: dict[str, list[str]] = {}

    if reg_path.is_file():
        entries = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or []
        for s in entries if isinstance(entries, list) else []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            sid = str(s["id"])
            sources_by_id[sid] = s
            norm_u = normalize_url_key(s.get("url"))
            if norm_u:
                by_norm_url.setdefault(norm_u, []).append(sid)
            m = COURTLISTENER_DOCKET_RE.search(str(s.get("url") or ""))
            if m:
                by_docket_id.setdefault(m.group(1), []).append(sid)
            src_text = f"{s.get('title', '')} {s.get('short', '')} {s.get('public_note', '')} {s.get('url', '')}"
            for tok in _extract_statute_tokens(src_text):
                if sid not in by_token.setdefault(tok, []):
                    by_token[tok].append(sid)

    facts_by_source: dict[str, list[str]] = {}
    facts_path = data_dir / "facts.yaml"
    facts_map: dict[str, dict[str, Any]] = {}
    if facts_path.is_file():
        loaded_facts = yaml.safe_load(facts_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded_facts, dict):
            facts_map = loaded_facts
            for fid, fmeta in loaded_facts.items():
                if not isinstance(fmeta, dict):
                    continue
                note_tokens = _extract_statute_tokens(str(fmeta.get("note") or ""))
                for sid in fmeta.get("sources") or []:
                    sid_str = str(sid)
                    if sid_str in sources_by_id:
                        facts_by_source.setdefault(sid_str, []).append(
                            f"{fid} ({strip_html(str(fmeta.get('value') or ''), 48)})"
                        )
                        for tok in note_tokens:
                            if sid_str not in by_token.setdefault(tok, []):
                                by_token[tok].append(sid_str)

    routes_by_source: dict[str, list[str]] = {}
    pages_path = data_dir / "pages.yaml"
    if pages_path.is_file():
        pages_list = yaml.safe_load(pages_path.read_text(encoding="utf-8")) or []
        for page in pages_list if isinstance(pages_list, list) else []:
            if not isinstance(page, dict):
                continue
            route = str(page.get("route") or "")
            if not route or page.get("slug") in {"sources", "medicaid-sources", "id-records-sources", "404"}:
                continue
            cited: set[str] = {str(sid) for sid in (page.get("sources") or []) if str(sid) in sources_by_id}
            tpl_rel = page.get("template")
            if tpl_rel and (templates_dir / str(tpl_rel)).is_file():
                tpl_text = (templates_dir / str(tpl_rel)).read_text(encoding="utf-8")
                for sid in source_id_re.findall(tpl_text):
                    if sid in sources_by_id:
                        cited.add(sid)
                for fid in FACT_CALL_RE.findall(tpl_text):
                    fentry = facts_map.get(fid)
                    if isinstance(fentry, dict):
                        for sid in fentry.get("sources") or []:
                            if str(sid) in sources_by_id:
                                cited.add(str(sid))
            for sid in sorted(cited):
                routes = routes_by_source.setdefault(sid, [])
                if route not in routes:
                    routes.append(route)

    return {
        "sources_by_id": sources_by_id,
        "by_norm_url": by_norm_url,
        "by_docket_id": by_docket_id,
        "by_token": by_token,
        "facts_by_source": facts_by_source,
        "routes_by_source": routes_by_source,
    }


def _word_match(needle: str, haystack_lower: str) -> bool:
    n = needle.strip().lower()
    if not n:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", haystack_lower))


def detect_item_jurisdictions(item: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    found: list[str] = []
    explicit = str(item.get("jurisdiction") or "").strip().upper()
    if explicit and explicit in (cfg.get("jurisdictions") or {}):
        found.append(explicit)

    host = extract_host(item.get("url"))
    text_blob = f"{item.get('title', '')} {item.get('summary', '')} {item.get('source', '')}".lower()

    for code, jur_meta in (cfg.get("jurisdictions") or {}).items():
        code_str = str(code)
        if not isinstance(jur_meta, dict) or code_str in found:
            continue
        all_domains = [
            *(str(d) for d in (jur_meta.get("domains") or [])),
            *(str(d) for d in (jur_meta.get("media_domains") or [])),
        ]
        if host and any(host_matches_domain(host, d) for d in all_domains):
            found.append(code_str)
            continue
        keywords = [str(k) for k in (jur_meta.get("keywords") or []) if k]
        jur_name = str(jur_meta.get("name") or "").strip()
        if jur_name and code_str not in {"US", "WA"}:
            keywords.append(jur_name)
        if any(_word_match(kw, text_blob) for kw in keywords):
            found.append(code_str)

    if not found:
        found.append("US")
    return found[:3]


def annotate_item_impact(
    item: dict[str, Any],
    cfg: dict[str, Any],
    impact_index: dict[str, Any],
    *,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Cross-reference an item against cited sources, facts, and routes, and calibrate urgency.

    `urgent` (`wire:urgent`) is strictly reserved for:
    1. Direct registry URL matches or Page Watch fingerprint changes on cited sources;
    2. Codified eCFR / state permanent or emergency rules, or Federal Register final rules /
       presidential documents with an effective date within `URGENT_EFFECTIVE_DAYS` (45 days);
    3. Court orders, opinions, injunctions, or judgments;
    4. Newly enacted / governor-signed statutes.
    """
    now_date = today or dt.date.today()
    enriched = dict(item)
    enriched["title"] = sanitize_link_label(enriched.get("title"), 195)
    if enriched.get("summary"):
        enriched["summary"] = sanitize_issue_text(enriched.get("summary"), 300)

    jurisdictions = detect_item_jurisdictions(enriched, cfg)
    enriched["jurisdictions"] = jurisdictions

    matched_sids: list[str] = list(enriched.get("matched_sources") or [])
    direct_url_match = bool(enriched.get("direct_url_match"))
    docket_match = False

    norm_u = normalize_url_key(enriched.get("url"))
    if norm_u and norm_u in impact_index["by_norm_url"]:
        direct_url_match = True
        for sid in impact_index["by_norm_url"][norm_u]:
            if sid not in matched_sids:
                matched_sids.append(sid)

    docket_m = COURTLISTENER_DOCKET_RE.search(str(enriched.get("url") or ""))
    if docket_m and docket_m.group(1) in impact_index["by_docket_id"]:
        docket_match = True
        for sid in impact_index["by_docket_id"][docket_m.group(1)]:
            if sid not in matched_sids:
                matched_sids.append(sid)

    text_blob = f"{enriched.get('title', '')} {enriched.get('summary', '')} {enriched.get('url', '')}"
    for tok in _extract_statute_tokens(text_blob):
        for sid in impact_index["by_token"].get(tok, []):
            src_jur = str(impact_index["sources_by_id"][sid].get("jurisdiction") or "")
            if src_jur in jurisdictions or "US" in jurisdictions or len(matched_sids) < 2:
                if sid not in matched_sids:
                    matched_sids.append(sid)

    matched_sids = matched_sids[:4]
    matched_facts: list[str] = []
    matched_routes: list[str] = []
    for sid in matched_sids:
        for fdesc in impact_index["facts_by_source"].get(sid, []):
            if fdesc not in matched_facts:
                matched_facts.append(fdesc)
        for route in impact_index["routes_by_source"].get(sid, []):
            if route not in matched_routes:
                matched_routes.append(route)

    enriched["matched_sources"] = matched_sids
    enriched["matched_facts"] = matched_facts[:4]
    enriched["matched_routes"] = matched_routes[:4]
    enriched["direct_registry_match"] = bool(direct_url_match or docket_match)

    summary_clean = str(enriched.get("summary") or "").strip()
    title_clean = str(enriched.get("title") or "").strip()
    channel_kind = str(enriched.get("channel_kind") or "")

    is_supporting_docket = bool(
        docket_m
        and (
            MINISTERIAL_DOCKET_RE.search(f"{title_clean} {summary_clean}")
            or SUPPORTING_FILING_RE.match(summary_clean)
        )
    )
    is_court_order = bool(enriched.get("is_court_order_or_opinion")) or (
        bool(docket_m or channel_kind.startswith("courtlistener"))
        and not is_supporting_docket
        and _is_court_order_or_opinion(title_clean, summary_clean)
    )

    is_near_term_final_rule = False
    if channel_kind == "ecfr" or enriched.get("is_final_state_rule"):
        is_near_term_final_rule = True
    elif channel_kind == "fedreg":
        doc_type = str(enriched.get("doc_type") or "").strip()
        if doc_type in {"Rule", "Presidential Document"}:
            eff_dt = parse_any_date(enriched.get("effective_on"))
            if eff_dt is None or (eff_dt - now_date).days <= URGENT_EFFECTIVE_DAYS:
                is_near_term_final_rule = True

    is_enacted_law = bool(enriched.get("is_enacted_law"))

    if is_supporting_docket:
        urgency = "watch"
    elif channel_kind == "page_watch" or direct_url_match or is_court_order or is_near_term_final_rule or is_enacted_law:
        urgency = "urgent"
    elif enriched.get("tier") == "primary" or enriched.get("wire_group") == "gov_and_legislative":
        urgency = "watch"
    else:
        urgency = "lead"

    enriched["urgency"] = urgency
    return enriched


def poll_page_watch(
    desk: str,
    fetcher: Callable[..., Any],
    *,
    today: dt.date | None = None,
    wire_state: dict[str, Any] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], int, str]:
    """Keyless Page Watch channel monitoring normalized fingerprints of cited registry sources."""
    cfg = _get_desk_cfg(desk, profiles)
    if not cfg.get("page_watch_enabled", True):
        return [], {}, 0, "skipped (page_watch disabled)"
    reg_path: Path = cfg["registry"]
    if not reg_path.is_file():
        return [], {}, 0, "skipped (registry file not present)"

    entries = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or []
    watchable = [
        s
        for s in (entries if isinstance(entries, list) else [])
        if isinstance(s, dict)
        and s.get("id")
        and safe_url(s.get("url"))
        and s.get("link_check") != "by hand"
        and s.get("page_watch", True) is not False
    ]
    if not watchable:
        return [], {}, 0, "skipped (0 watchable registry entries)"

    now_date = today or dt.date.today()
    prior_fps: dict[str, dict[str, Any]] = {}
    if isinstance(wire_state, dict) and isinstance(wire_state.get("page_fingerprints"), dict):
        prior_fps = {str(k): dict(v) for k, v in wire_state["page_fingerprints"].items() if isinstance(v, dict)}

    updated_fps: dict[str, dict[str, Any]] = dict(prior_fps)
    url_outcomes: dict[str, tuple[str, Any]] = {}
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    bot_skipped = 0
    not_modified_count = 0
    baselines_recorded = 0
    probed_ok = 0

    for idx, src in enumerate(watchable):
        sid = str(src["id"])
        url = safe_url(src.get("url"))
        prior_entry = prior_fps.get(sid) or {}
        yaml_fp = str(src.get("fingerprint") or "").strip()

        if url not in url_outcomes:
            if idx > 0 and pause > 0:
                time.sleep(pause)
            cond_headers: dict[str, str] = {}
            # Only send conditional headers when we already have a matching baseline for this URL.
            if not yaml_fp and prior_entry.get("url") == url and prior_entry.get("fingerprint"):
                if prior_entry.get("etag"):
                    cond_headers["If-None-Match"] = str(prior_entry["etag"])
                if prior_entry.get("last_modified"):
                    cond_headers["If-Modified-Since"] = str(prior_entry["last_modified"])
            try:
                st_code, resp_hdrs, raw_bytes = _fetch_with_meta(fetcher, url, headers=cond_headers or None)
                if st_code == 304:
                    url_outcomes[url] = ("not_modified", {})
                elif st_code >= 400:
                    url_outcomes[url] = ("http_error", st_code)
                else:
                    fp_info = compute_page_fingerprint(raw_bytes, resp_hdrs, url=url)
                    url_outcomes[url] = ("ok", fp_info)
            except urllib.error.HTTPError as exc:
                url_outcomes[url] = ("http_error", int(exc.code))
            except Exception as exc:  # noqa: BLE001
                url_outcomes[url] = ("exc", f"{type(exc).__name__}: {exc}")

        outcome_kind, detail = url_outcomes[url]
        if outcome_kind == "not_modified":
            not_modified_count += 1
            probed_ok += 1
            continue
        if outcome_kind == "http_error":
            if detail == 403 and src.get("bot_gated") is True:
                bot_skipped += 1
                continue
            errors.append(f"{sid}: HTTP {detail}")
            continue
        if outcome_kind == "exc":
            errors.append(f"{sid}: {detail}")
            continue

        probed_ok += 1
        fp_info = detail
        live_fp = str(fp_info["fingerprint"])
        checked_str = str(src.get("checked") or "unknown")
        short_title = str(src.get("short") or src.get("title") or sid)

        if yaml_fp:
            updated_fps[sid] = {
                "url": url,
                "fingerprint": live_fp,
                "baseline_date": checked_str,
                "etag": fp_info.get("etag", ""),
                "last_modified": fp_info.get("last_modified", ""),
            }
            if live_fp != yaml_fp:
                hdr_bits = [
                    b
                    for b in (
                        f"Last-Modified {fp_info['last_modified']}" if fp_info.get("last_modified") else "",
                        f"ETag {fp_info['etag']}" if fp_info.get("etag") else "",
                    )
                    if b
                ]
                hdr_note = f" ({'; '.join(hdr_bits)})" if hdr_bits else ""
                items.append(
                    {
                        "id": item_fingerprint(desk, "page-watch", f"{sid}:{live_fp}"),
                        "tier": "primary",
                        "wire_group": "primary",
                        "channel_kind": "page_watch",
                        "direct_url_match": True,
                        "matched_sources": [sid],
                        "jurisdiction": str(src.get("jurisdiction") or "US"),
                        "source": f"Registry Page Watch ({sid})",
                        "date": now_date.isoformat(),
                        "title": f"Cited source changed: {short_title} ({sid})",
                        "url": url,
                        "summary": (
                            f"Changed since you last read it on {checked_str} "
                            f"({fp_info['method']} fingerprint updated{hdr_note})."
                        ),
                    }
                )
        else:
            prev_fp = str(prior_entry.get("fingerprint") or "").strip()
            prev_url = str(prior_entry.get("url") or "")
            baseline_dt = str(prior_entry.get("baseline_date") or now_date.isoformat())
            updated_fps[sid] = {
                "url": url,
                "fingerprint": live_fp,
                "baseline_date": baseline_dt if (prev_fp and prev_url == url) else now_date.isoformat(),
                "etag": fp_info.get("etag", ""),
                "last_modified": fp_info.get("last_modified", ""),
            }
            if not prev_fp or prev_url != url:
                baselines_recorded += 1
            elif live_fp != prev_fp:
                # Advance baseline_date on change so the new state reflects this revision.
                updated_fps[sid]["baseline_date"] = now_date.isoformat()
                items.append(
                    {
                        "id": item_fingerprint(desk, "page-watch", f"{sid}:{live_fp}"),
                        "tier": "primary",
                        "wire_group": "primary",
                        "channel_kind": "page_watch",
                        "direct_url_match": True,
                        "matched_sources": [sid],
                        "jurisdiction": str(src.get("jurisdiction") or "US"),
                        "source": f"Registry Page Watch ({sid})",
                        "date": now_date.isoformat(),
                        "title": f"Cited source changed: {short_title} ({sid})",
                        "url": url,
                        "summary": (
                            f"Changed since baseline recorded on {baseline_dt} "
                            f"(last verified in registry on {checked_str}; {fp_info['method']} fingerprint updated)."
                        ),
                    }
                )

    unique_url_count = len(url_outcomes)
    if errors and probed_ok == 0:
        return [], updated_fps, 0, f"error ({'; '.join(errors[:5])})"

    notes: list[str] = [f"{len(items)} changed across {probed_ok} source(s) ({unique_url_count} unique URL(s))"]
    if baselines_recorded:
        notes.append(f"{baselines_recorded} baseline(s) initialized")
    if not_modified_count:
        notes.append(f"{not_modified_count} HTTP 304 unchanged")
    if bot_skipped:
        notes.append(f"{bot_skipped} bot-gated skipped")
    if errors:
        notes.append(f"partial warning ({len(errors)} error(s): {'; '.join(errors[:3])})")
    return items, updated_fps, probed_ok, f"ok ({', '.join(notes)})"


def _matches_federal_register(cfg: dict[str, Any], title: str, abstract: str, agencies: str) -> bool:
    combined = f"{title} {abstract} {agencies}"
    always_re = cfg.get("fr_always_keep_re")
    if always_re and always_re.search(combined):
        return True
    must_re = cfg.get("fr_must_match_re")
    ctx_re = cfg.get("fr_context_re")
    if must_re and not must_re.search(combined):
        return False
    if ctx_re and not ctx_re.search(combined):
        return False
    return True


def _ingest_fedreg_doc(
    doc: dict[str, Any],
    *,
    desk: str,
    cfg: dict[str, Any],
    since: dt.date,
    seen_docs: set[str],
    skip_regex_filter: bool = False,
    public_inspection: bool = False,
) -> dict[str, Any] | None:
    doc_num = str(doc.get("document_number") or "").strip()
    if not doc_num or doc_num in seen_docs:
        return None
    pub_raw = str(doc.get("publication_date") or doc.get("filed_at") or "")
    pub_parsed = parse_any_date(pub_raw)
    if pub_parsed and pub_parsed < since:
        return None
    html_url = safe_url(doc.get("html_url"))
    if not html_url:
        return None
    agencies = ", ".join(
        str(a.get("name") or a.get("raw_name") or "")
        for a in (doc.get("agencies") or [])
        if isinstance(a, dict) and (a.get("name") or a.get("raw_name"))
    )
    title_clean = sanitize_link_label(doc.get("title") or doc_num, 195)
    abstract_clean = sanitize_issue_text(doc.get("abstract") or doc.get("editorial_note") or "", 220)
    if not skip_regex_filter and not _matches_federal_register(cfg, title_clean, abstract_clean, agencies):
        return None

    seen_docs.add(doc_num)
    doc_type = str(doc.get("type") or "").strip()
    eff_on = str(doc.get("effective_on") or "").strip()
    comments_close = str(doc.get("comments_close_on") or "").strip()

    deadline_parts: list[str] = []
    if eff_on:
        deadline_parts.append(f"Effective {eff_on}")
    if comments_close:
        deadline_parts.append(f"Comments close {comments_close}")
    deadline_str = f" [{' · '.join(deadline_parts)}]" if deadline_parts else ""

    prefix_note = f"{doc_type} ({doc_num})" if doc_type else f"({doc_num})"
    if agencies:
        prefix_note = f"{prefix_note} — {agencies}"
    base_summary = f"{prefix_note}{deadline_str}: {abstract_clean}" if abstract_clean else f"{prefix_note}{deadline_str}"
    source_name = "Federal Register (Public Inspection)" if public_inspection else "Federal Register"
    return {
        "id": item_fingerprint(desk, "fedreg", doc_num),
        "tier": "primary",
        "wire_group": "primary",
        "channel_kind": "fedreg",
        "doc_type": doc_type,
        "effective_on": eff_on,
        "comments_close_on": comments_close,
        "jurisdiction": "US",
        "source": source_name,
        "date": (pub_parsed or since).isoformat(),
        "title": title_clean,
        "url": html_url,
        "summary": sanitize_issue_text(base_summary, 280),
    }


def poll_federal_register(
    desk: str,
    since: dt.date,
    fetcher: Callable[..., bytes],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
    max_pages: int = 5,
) -> tuple[list[dict[str, Any]], str]:
    cfg = _get_desk_cfg(desk, profiles)
    terms = cfg["federal_register_terms"]
    cfr_watches = cfg["ecfr_watch"]
    if not terms and not cfr_watches:
        return [], "skipped (no Federal Register terms or CFR watches configured)"

    items: list[dict[str, Any]] = []
    seen_docs: set[str] = set()
    req_count = 0

    def _fetch_paginated_documents(initial_url: str, *, skip_regex: bool = False) -> None:
        nonlocal req_count
        next_url: str | None = initial_url
        visited_urls: set[str] = set()
        pages = 0
        while next_url and next_url not in visited_urls and pages < max_pages:
            if req_count > 0 and pause > 0:
                time.sleep(pause)
            req_count += 1
            visited_urls.add(next_url)
            pages += 1
            data = json.loads(fetcher(next_url).decode("utf-8", errors="replace"))
            if not isinstance(data, dict):
                break
            for doc in data.get("results") or []:
                if not isinstance(doc, dict):
                    continue
                built = _ingest_fedreg_doc(
                    doc,
                    desk=desk,
                    cfg=cfg,
                    since=since,
                    seen_docs=seen_docs,
                    skip_regex_filter=skip_regex,
                )
                if built:
                    items.append(built)
            raw_next = data.get("next_page_url")
            next_url = safe_url(raw_next) if raw_next else None

    field_params = [("fields[]", f) for f in FEDERAL_REGISTER_FIELDS]
    for term in terms:
        params = urllib.parse.urlencode(
            [
                ("conditions[term]", term),
                ("conditions[publication_date][gte]", since.isoformat()),
                ("per_page", "100"),
                ("order", "newest"),
                *field_params,
            ]
        )
        _fetch_paginated_documents(f"https://www.federalregister.gov/api/v1/documents.json?{params}")

    # Direct CFR title/part queries catch rules amending watched parts regardless of keyword phrasing.
    for title_num, part_num, _label, _prefixes in cfr_watches:
        cfr_params = urllib.parse.urlencode(
            [
                ("conditions[cfr][title]", title_num),
                ("conditions[cfr][part]", part_num),
                ("conditions[publication_date][gte]", since.isoformat()),
                ("per_page", "100"),
                ("order", "newest"),
                *field_params,
            ]
        )
        _fetch_paginated_documents(
            f"https://www.federalregister.gov/api/v1/documents.json?{cfr_params}",
            skip_regex=False,
        )

    # Pre-publication Public Inspection filings on the primary term(s).
    if cfg.get("fr_public_inspection", True) and terms:
        for term in terms[:2]:
            if req_count > 0 and pause > 0:
                time.sleep(pause)
            req_count += 1
            pi_params = urllib.parse.urlencode(
                [
                    ("conditions[term]", term),
                    ("per_page", "50"),
                ]
            )
            pi_url = f"https://www.federalregister.gov/api/v1/public-inspection-documents.json?{pi_params}"
            try:
                pi_data = json.loads(fetcher(pi_url).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(pi_data, dict):
                continue
            for doc in pi_data.get("results") or []:
                if not isinstance(doc, dict):
                    continue
                built = _ingest_fedreg_doc(
                    doc,
                    desk=desk,
                    cfg=cfg,
                    since=since,
                    seen_docs=seen_docs,
                    public_inspection=True,
                )
                if built:
                    items.append(built)

    return items, f"ok ({len(items)} match(es))"


def poll_ecfr(
    desk: str,
    since: dt.date,
    fetcher: Callable[..., bytes],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], str]:
    cfg = _get_desk_cfg(desk, profiles)
    watches = cfg["ecfr_watch"]
    if not watches:
        return [], "skipped (no eCFR parts configured)"

    items: dict[str, dict[str, Any]] = {}
    for idx, (title_num, part_num, label, prefixes) in enumerate(watches):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        url = f"https://www.ecfr.gov/api/versioner/v1/versions/title-{title_num}.json?part={part_num}"
        data = json.loads(fetcher(url).decode("utf-8", errors="replace"))
        for v in data.get("content_versions") or []:
            issue_date = parse_any_date(v.get("issue_date"))
            amend_date = parse_any_date(v.get("amendment_date"))
            # Filter primarily on `issue_date` so newly added eCFR versions carrying an earlier
            # `amendment_date` are never skipped, while also catching future/current `amendment_date`s.
            in_window = bool((issue_date and issue_date >= since) or (amend_date and amend_date >= since))
            if not in_window:
                continue
            display_date = issue_date or amend_date
            assert display_date is not None
            ident = str(v.get("identifier") or "").strip()
            if prefixes and not any(ident.startswith(p) for p in prefixes):
                continue
            amend_iso = (amend_date or display_date).isoformat()
            issue_iso = (issue_date or display_date).isoformat()
            key = f"{title_num}:{part_num}:{ident}:{issue_iso}:{amend_iso}"
            sec_url = (
                f"https://www.ecfr.gov/current/title-{title_num}/section-{ident}"
                if ident
                else f"https://www.ecfr.gov/current/title-{title_num}/part-{part_num}"
            )
            sec_name = sanitize_issue_text(v.get("name"), 180)
            date_note = (
                f"Amendment date: {amend_iso} (eCFR issue date: {issue_iso})"
                if amend_iso != issue_iso
                else f"Amendment date: {amend_iso}"
            )
            summary_str = f"{sec_name} — {date_note}" if sec_name else date_note
            items[key] = {
                "id": item_fingerprint(desk, "ecfr", key),
                "tier": "primary",
                "wire_group": "primary",
                "channel_kind": "ecfr",
                "jurisdiction": "US",
                "source": f"eCFR ({label})",
                "date": display_date.isoformat(),
                "title": sanitize_link_label(f"{label} section {ident or part_num} amended", 190),
                "url": safe_url(sec_url),
                "summary": sanitize_issue_text(summary_str, 250),
            }
    return list(items.values()), f"ok ({len(items)} match(es))"


_WA_WSR_ROW_RE = re.compile(
    r'href\s*=\s*["\']?(https?://lawfilesext\.leg\.wa\.gov/law/wsr/(\d{4})/[^"\'\s>]+\.htm)["\']?[^>]*>'
    r"\s*([0-9]{2}-[0-9]{2}-[0-9]{3})\s*<.*?"
    r">\s*(PERM|PROP|EMER|EXPE|PREP|MISC)\s*<",
    re.IGNORECASE | re.DOTALL,
)
_WA_WSR_FILED_RE = re.compile(r"\[Filed\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", re.IGNORECASE)
_WA_WSR_REGTYPE_RE = re.compile(
    r"<!--\s*field:\s*WSRRegType\s*-->\s*<div[^>]*>(.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)


def poll_state_registers(
    desk: str,
    since: dt.date,
    fetcher: Callable[..., bytes],
    *,
    wire_state: dict[str, Any] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Keyless primary channel polling UT Administrative Rules RSS, WA State Register, and OR OLIS OData."""
    cfg = _get_desk_cfg(desk, profiles)
    registers = cfg.get("state_registers") or ()
    if not registers:
        return [], [], "skipped (no state registers configured)"

    checked_wsr: list[str] = []
    if isinstance(wire_state, dict) and isinstance(wire_state.get("wsr_checked_filings"), list):
        checked_wsr = [str(x) for x in wire_state["wsr_checked_filings"] if x]
    checked_wsr_set = set(checked_wsr)

    items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    atom_ns = {"a": "http://www.w3.org/2005/Atom"}

    for idx, spec in enumerate(registers):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        kind = spec["kind"]
        jur = spec["jurisdiction"]
        name = spec["name"]
        url = spec["url"]
        match_re = spec["match_re"]

        try:
            if kind in {"ut_rss", "rss", "atom"}:
                raw_xml = fetcher(url)
                root = ET.fromstring(raw_xml)
                rss_els = root.findall(".//item")
                entries: list[tuple[dt.date | None, str, str, str]] = []
                if rss_els:
                    for el in rss_els:
                        entries.append(
                            (
                                parse_any_date(
                                    el.findtext("pubDate")
                                    or el.findtext("{http://purl.org/dc/elements/1.1/}date")
                                ),
                                sanitize_link_label(el.findtext("title"), 190),
                                sanitize_issue_text(el.findtext("description"), 240),
                                safe_url(el.findtext("link")),
                            )
                        )
                else:
                    for el in root.findall("a:entry", atom_ns):
                        lk_href = ""
                        for lk in el.findall("a:link", atom_ns):
                            if lk.attrib.get("rel", "alternate") == "alternate":
                                lk_href = safe_url(lk.attrib.get("href"))
                                if lk_href:
                                    break
                        entries.append(
                            (
                                parse_any_date(
                                    el.findtext("a:published", "", atom_ns)
                                    or el.findtext("a:updated", "", atom_ns)
                                ),
                                sanitize_link_label(el.findtext("a:title", "", atom_ns), 190),
                                sanitize_issue_text(el.findtext("a:summary", "", atom_ns), 240),
                                lk_href,
                            )
                        )
                for pub_dt, title, desc, link in entries:
                    if not pub_dt or pub_dt < since or not link:
                        continue
                    if match_re and not match_re.search(f"{title} {desc}"):
                        continue
                    items[link] = {
                        "id": item_fingerprint(desk, f"state-reg-{jur.lower()}", link),
                        "tier": "primary",
                        "wire_group": "primary",
                        "channel_kind": "state_register",
                        "jurisdiction": jur,
                        "source": name,
                        "date": pub_dt.isoformat(),
                        "title": title,
                        "url": link,
                        "summary": desc or f"Official rulemaking notice from {name}.",
                    }

            elif kind == "wa_wsr":
                raw_html = fetcher(url).decode("utf-8", errors="replace")
                inspected_this_agency = 0
                for m in _WA_WSR_ROW_RE.finditer(raw_html):
                    doc_url_raw, year_str, wsr_num, wsr_type = m.groups()
                    wsr_type_up = wsr_type.upper()
                    if wsr_type_up == "MISC":
                        continue
                    if int(year_str) < since.year - 1:
                        break
                    if wsr_num in checked_wsr_set:
                        continue
                    if inspected_this_agency >= 6:
                        break
                    inspected_this_agency += 1
                    doc_url = safe_url(doc_url_raw.replace("http://", "https://"))
                    if not doc_url:
                        continue
                    if pause > 0:
                        time.sleep(pause)
                    filing_html = fetcher(doc_url).decode("utf-8", errors="replace")
                    filed_m = _WA_WSR_FILED_RE.search(filing_html)
                    filed_dt = parse_any_date(filed_m.group(1)) if filed_m else None
                    if filed_dt and filed_dt < since:
                        checked_wsr_set.add(wsr_num)
                        checked_wsr.append(wsr_num)
                        continue
                    reg_m = _WA_WSR_REGTYPE_RE.search(filing_html)
                    reg_heading = strip_html(reg_m.group(1) if reg_m else wsr_type_up, 90)
                    body_snip = sanitize_issue_text(filing_html, 260)
                    if match_re and not match_re.search(filing_html):
                        continue
                    items[doc_url] = {
                        "id": item_fingerprint(desk, "wa-wsr", wsr_num),
                        "tier": "primary",
                        "wire_group": "primary",
                        "channel_kind": "state_register",
                        "is_final_state_rule": wsr_type_up in {"PERM", "EMER"},
                        "jurisdiction": jur,
                        "source": name,
                        "date": (filed_dt or since).isoformat(),
                        "title": sanitize_link_label(f"WSR {wsr_num} ({wsr_type_up}): {reg_heading}", 190),
                        "url": doc_url,
                        "summary": body_snip,
                    }

            elif kind == "or_olis_odata":
                odata_filter = f"ModifiedDate ge datetime'{since.isoformat()}T00:00:00'"
                q_str = urllib.parse.urlencode(
                    [
                        ("$filter", odata_filter),
                        ("$orderby", "ModifiedDate desc"),
                        ("$top", "40"),
                        ("$format", "json"),
                    ]
                )
                odata_url = f"{url.rstrip('/')}?{q_str}"
                raw_payload = fetcher(odata_url).decode("utf-8", errors="replace").strip()
                # Gracefully ignore non-JSON fallback payloads in offline XML mock tests.
                if not raw_payload.startswith("{"):
                    continue
                payload = json.loads(raw_payload)
                rows = payload.get("value") if isinstance(payload, dict) else []
                for row in rows if isinstance(rows, list) else []:
                    if not isinstance(row, dict):
                        continue
                    sess = str(row.get("SessionKey") or "").strip()
                    prefix = str(row.get("MeasurePrefix") or "").strip()
                    num = str(row.get("MeasureNumber") or "").strip()
                    catchline = str(row.get("CatchLine") or row.get("RelatingTo") or "").strip()
                    curr_loc = str(row.get("CurrentLocation") or "").strip()
                    mod_dt = parse_any_date(str(row.get("ModifiedDate") or "")) or since
                    combined = f"{prefix} {num} {catchline} {row.get('RelatingTo', '')} {curr_loc}"
                    if not sess or not prefix or not num:
                        continue
                    if match_re and not match_re.search(combined):
                        continue
                    measure_url = safe_url(
                        f"https://olis.oregonlegislature.gov/liz/{sess}/Measures/Overview/{prefix}{num}"
                    )
                    if not measure_url:
                        continue
                    fp_key = f"{sess}:{prefix}{num}:{mod_dt.isoformat()}:{curr_loc}"
                    items[measure_url] = {
                        "id": item_fingerprint(desk, "or-olis", fp_key),
                        "tier": "primary",
                        "wire_group": "primary",
                        "channel_kind": "state_register",
                        "is_enacted_law": bool(LEGISCAN_ENACTED_RE.search(curr_loc)),
                        "jurisdiction": jur,
                        "source": name,
                        "date": mod_dt.isoformat(),
                        "title": sanitize_link_label(f"OR {prefix} {num} ({sess}): {catchline}", 190),
                        "url": measure_url,
                        "summary": sanitize_issue_text(
                            f"Current location/status ({mod_dt.isoformat()}): {curr_loc or 'Updated in OLIS'}",
                            240,
                        ),
                    }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    if errors and len(errors) == len(registers):
        return [], checked_wsr[-400:], f"error ({'; '.join(errors)})"
    if errors:
        return (
            list(items.values()),
            checked_wsr[-400:],
            f"ok ({len(items)} match(es); partial warning: {'; '.join(errors)})",
        )
    return list(items.values()), checked_wsr[-400:], f"ok ({len(items)} match(es) across {len(registers)} state source(s))"


def poll_courtlistener_feeds(
    desk: str,
    since: dt.date,
    fetcher: Callable[..., bytes],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], str]:
    dockets = discover_dockets(desk, profiles)
    if not dockets:
        return [], "skipped (0 tracked dockets)"

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    filtered_ministerial = 0

    for idx, (docket_id, case_label) in enumerate(sorted(dockets.items())):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        feed_url = f"https://www.courtlistener.com/docket/{docket_id}/feed/"
        try:
            raw_xml = fetcher(feed_url)
            root = ET.fromstring(raw_xml)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{docket_id}: {type(exc).__name__}: {exc}")
            continue
        for entry in root.findall("atom:entry", ns):
            pub_el = entry.find("atom:published", ns)
            upd_el = entry.find("atom:updated", ns)
            pub_dt = parse_any_date(pub_el.text if pub_el is not None and pub_el.text else None)
            upd_dt = parse_any_date(upd_el.text if upd_el is not None and upd_el.text else None)
            # Check max(published, updated) so late-uploaded RECAP PDFs carrying an older filing
            # date still enter the lookback window when newly posted to CourtListener.
            candidates_dt = [d for d in (pub_dt, upd_dt) if d is not None]
            if not candidates_dt or max(candidates_dt) < since:
                continue
            entry_date = pub_dt or upd_dt
            assert entry_date is not None
            href = ""
            for link_el in entry.findall("atom:link", ns):
                if link_el.attrib.get("rel", "alternate") == "alternate":
                    href = safe_url(link_el.attrib.get("href"))
                    if href:
                        break
            if not href:
                link_el = entry.find("atom:link", ns)
                href = safe_url(link_el.attrib.get("href") if link_el is not None else "")
            if not href:
                continue
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            title_clean = sanitize_link_label(title_el.text if title_el is not None else case_label, 195)
            summary_clean = sanitize_issue_text(summary_el.text if summary_el is not None else "", 240)
            if MINISTERIAL_DOCKET_RE.search(f"{title_clean} {summary_clean}"):
                filtered_ministerial += 1
                continue
            if upd_dt and pub_dt and pub_dt < since <= upd_dt:
                summary_clean = sanitize_issue_text(
                    f"{summary_clean} (Filed {pub_dt.isoformat()}; RECAP updated {upd_dt.isoformat()})".strip(),
                    260,
                )
            items[href] = {
                "id": item_fingerprint(desk, "recap", href),
                "tier": "primary",
                "wire_group": "primary",
                "channel_kind": "courtlistener_recap",
                "is_court_order_or_opinion": _is_court_order_or_opinion(title_clean, summary_clean),
                "jurisdiction": "US",
                "source": f"CourtListener RECAP ({case_label})",
                "date": entry_date.isoformat(),
                "title": title_clean,
                "url": href,
                "summary": summary_clean or f"New docket entry on {case_label}.",
            }

    filt_note = f", {filtered_ministerial} routine appearance/clerk notice(s) filtered" if filtered_ministerial else ""
    if errors and len(errors) == len(dockets):
        return [], f"error ({'; '.join(errors)})"
    if errors:
        return (
            list(items.values()),
            f"ok ({len(dockets) - len(errors)}/{len(dockets)} docket feed(s), {len(items)} match(es){filt_note}; partial warning: {'; '.join(errors)})",
        )
    return list(items.values()), f"ok ({len(dockets)} docket feed(s), {len(items)} match(es){filt_note})"


def poll_courtlistener_v4(
    desk: str,
    since: dt.date,
    token: str,
    fetcher: Callable[..., bytes],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], str]:
    tok = token.strip()
    if not tok:
        return [], "skipped (COURTLISTENER_TOKEN not set; keyless RECAP feeds ran above)"
    cfg = _get_desk_cfg(desk, profiles)
    queries = cfg["courtlistener_v4_queries"]
    if not queries:
        return [], "skipped (no CourtListener v4 queries configured)"

    headers = {"Authorization": f"Token {tok}"}
    items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for idx, qspec in enumerate(queries):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        search_type = qspec["search_type"]
        params = urllib.parse.urlencode(
            [
                ("type", search_type),
                ("order_by", "dateFiled desc"),
                ("filed_after", since.isoformat()),
                ("q", qspec["query"]),
            ]
        )
        url = f"https://www.courtlistener.com/api/rest/v4/search/?{params}"
        try:
            payload = json.loads(fetcher(url, headers=headers).decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{qspec['label']}: {type(exc).__name__}: {redact_secrets(str(exc), (tok,))}")
            continue

        for row in (payload.get("results") or [])[:12]:
            if not isinstance(row, dict):
                continue
            recap_docs = row.get("recap_documents") or []
            first_doc_url = (
                recap_docs[0].get("absolute_url")
                if recap_docs and isinstance(recap_docs[0], dict)
                else ""
            )
            first_doc_desc = (
                str(recap_docs[0].get("description") or recap_docs[0].get("short_description") or "")
                if recap_docs and isinstance(recap_docs[0], dict)
                else ""
            )
            docket_id = row.get("docket_id")
            rel_url = (
                row.get("docket_absolute_url")
                or row.get("absolute_url")
                or first_doc_url
                or (f"/docket/{docket_id}/" if docket_id else "")
            )
            if not rel_url:
                continue
            full_url = safe_url(urllib.parse.urljoin("https://www.courtlistener.com", str(rel_url)))
            if not full_url:
                continue
            case_name = sanitize_link_label(
                row.get("caseName") or row.get("case_name") or row.get("docketNumber") or "Federal court filing",
                175,
            )
            court = sanitize_issue_text(row.get("court") or row.get("court_id") or "", 80)
            date_str = row.get("dateFiled") or row.get("date_filed") or row.get("date_created") or since.isoformat()
            parsed_dt = parse_any_date(str(date_str)) or since
            kind_badge = "Opinion" if search_type == "o" else "Docket"
            snippet = ""
            opinions = row.get("opinions") or []
            if opinions and isinstance(opinions[0], dict):
                snippet = sanitize_issue_text(opinions[0].get("snippet") or "", 200)
            if not snippet:
                snippet = sanitize_issue_text(
                    first_doc_desc or row.get("snippet") or row.get("docketNumber") or qspec["label"],
                    220,
                )
            title_full = sanitize_link_label(f"{case_name} ({court})" if court else case_name, 195)
            items[full_url] = {
                "id": item_fingerprint(desk, f"cl-v4-{search_type}", full_url),
                "tier": "primary",
                "wire_group": "primary",
                "channel_kind": f"courtlistener_v4_{search_type}",
                "is_court_order_or_opinion": _is_court_order_or_opinion(
                    title_full,
                    f"{first_doc_desc} {snippet}",
                    search_type=search_type,
                ),
                "jurisdiction": "US",
                "source": f"CourtListener REST v4 ({kind_badge})",
                "date": parsed_dt.isoformat(),
                "title": title_full,
                "url": full_url,
                "summary": snippet,
            }

    if errors and not items and len(errors) == len(queries):
        return [], f"error ({'; '.join(errors)})"
    if errors:
        return list(items.values()), f"ok ({len(items)} match(es); partial warning: {'; '.join(errors)})"
    return list(items.values()), f"ok ({len(items)} match(es) across {len(queries)} v4 search(es))"


def _fetch_legiscan_bill_detail(
    bill_id: str,
    key_clean: str,
    fetcher: Callable[..., bytes],
) -> dict[str, Any] | None:
    params = urllib.parse.urlencode([("key", key_clean), ("op", "getBill"), ("id", bill_id)])
    url = f"https://api.legiscan.com/?{params}"
    try:
        payload = json.loads(fetcher(url).decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict) or str(payload.get("status") or "OK").upper() != "OK":
        return None
    bill_obj = payload.get("bill")
    return bill_obj if isinstance(bill_obj, dict) else None


def _extract_upcoming_hearings(bill_obj: dict[str, Any], since: dt.date) -> list[str]:
    notes: list[str] = []
    for ev in bill_obj.get("calendar") or []:
        if not isinstance(ev, dict):
            continue
        ev_dt = parse_any_date(str(ev.get("date") or ""))
        if not ev_dt or ev_dt < since:
            continue
        ev_type = str(ev.get("type") or "Hearing").strip()
        ev_time = str(ev.get("time") or "").strip()
        ev_loc = str(ev.get("location") or "").strip()
        ev_desc = str(ev.get("description") or "").strip()
        when_str = f"{ev_dt.isoformat()} {ev_time}".strip()
        where_str = f" ({ev_loc})" if ev_loc else ""
        desc_str = f": {ev_desc}" if ev_desc else ""
        notes.append(sanitize_issue_text(f"{ev_type} {when_str}{where_str}{desc_str}", 140))
    return notes[:2]


def poll_legiscan(
    desk: str,
    since: dt.date,
    api_key: str,
    fetcher: Callable[..., bytes],
    *,
    today: dt.date | None = None,
    wire_state: dict[str, Any] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    key_clean = api_key.strip()
    if not key_clean:
        return [], {}, "skipped (LEGISCAN_API_KEY not set)"
    cfg = _get_desk_cfg(desk, profiles)
    queries = cfg["legiscan_queries"]
    tracked_bills = cfg.get("legiscan_tracked_bills") or ()
    if not queries and not tracked_bills:
        return [], {}, "skipped (no LegiScan queries or tracked_bills configured)"

    anchor = max(today or dt.date.today(), since)
    action_window = f"action:{since:%Y%m%d}..{anchor:%Y%m%d}"
    min_rel = cfg["legiscan_min_relevance"]
    year_param = cfg["legiscan_year"]

    prior_hashes: dict[str, str] = {}
    if isinstance(wire_state, dict) and isinstance(wire_state.get("legiscan_change_hashes"), dict):
        prior_hashes = {str(k): str(v) for k, v in wire_state["legiscan_change_hashes"].items() if v}
    updated_hashes = dict(prior_hashes)

    items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    detail_calls = 0

    # 1. Poll explicitly tracked bill IDs via `getBill` + `change_hash` diffing.
    for tb in tracked_bills:
        bill_id = tb["bill_id"]
        if detail_calls > 0 and pause > 0:
            time.sleep(pause)
        detail_calls += 1
        bill_obj = _fetch_legiscan_bill_detail(bill_id, key_clean, fetcher)
        if bill_obj is None:
            errors.append(f"tracked bill {bill_id}: getBill failed")
            continue
        change_hash = str(bill_obj.get("change_hash") or "").strip()
        prev_hash = prior_hashes.get(bill_id, "")
        state_code = str(bill_obj.get("state") or tb.get("state") or "US").upper()
        bill_num = str(bill_obj.get("bill_number") or tb.get("label") or bill_id).strip()
        history = bill_obj.get("history") or []
        latest_hist = history[-1] if history and isinstance(history[-1], dict) else {}
        action_dt = (
            parse_any_date(str(latest_hist.get("date") or bill_obj.get("status_date") or ""))
            or anchor
        )
        last_action = sanitize_issue_text(str(latest_hist.get("action") or ""), 180)
        hearings = _extract_upcoming_hearings(bill_obj, since)
        if change_hash:
            updated_hashes[bill_id] = change_hash

        # Report if `change_hash` changed since last stored hash, or if recent action / upcoming hearing.
        changed = bool(change_hash and prev_hash and change_hash != prev_hash)
        if not changed and action_dt < since and not hearings:
            continue

        official_link = safe_url(bill_obj.get("state_link"))
        fallback_link = safe_url(bill_obj.get("url"))
        target_url = official_link or fallback_link
        if not target_url:
            continue
        source_note = (
            f"LegiScan tracked bill ({state_code} — official legislature link)"
            if official_link
            else f"LegiScan tracked bill ({state_code} — verify in official legislature)"
        )
        summary_parts = [f"Last action ({action_dt.isoformat()}): {last_action or 'Updated'}"]
        if hearings:
            summary_parts.append("Calendar: " + "; ".join(hearings))
        fp_key = f"tracked:{state_code}:{bill_id}:{change_hash or action_dt.isoformat()}"
        items[bill_id] = {
            "id": item_fingerprint(desk, "legiscan", fp_key),
            "tier": "lead",
            "wire_group": "gov_and_legislative",
            "channel_kind": "legiscan",
            "is_enacted_law": bool(LEGISCAN_ENACTED_RE.search(last_action)),
            "jurisdiction": state_code,
            "source": source_note,
            "date": action_dt.isoformat(),
            "title": sanitize_link_label(f"{state_code} {bill_num}: {bill_obj.get('title') or ''}", 190),
            "url": target_url,
            "summary": sanitize_issue_text(" · ".join(summary_parts), 260),
        }

    # 2. Run `getSearch` queries and enrich top matches with `getBill` (`state_link` + `calendar`).
    for idx, (state_code, query) in enumerate(queries):
        if (idx > 0 or detail_calls > 0) and pause > 0:
            time.sleep(pause)
        scoped_query = f"({query}) AND {action_window}"
        params = urllib.parse.urlencode(
            [
                ("key", key_clean),
                ("op", "getSearch"),
                ("state", state_code),
                ("year", year_param),
                ("query", scoped_query),
            ]
        )
        url = f"https://api.legiscan.com/?{params}"
        try:
            payload = json.loads(fetcher(url).decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{state_code}: {type(exc).__name__}: {redact_secrets(str(exc), (key_clean,))}")
            continue

        api_status = str(payload.get("status") or "OK").upper()
        if api_status != "OK":
            # Retry once with `action:month` if a date-range syntax error is ever returned.
            fallback_params = urllib.parse.urlencode(
                [
                    ("key", key_clean),
                    ("op", "getSearch"),
                    ("state", state_code),
                    ("year", year_param),
                    ("query", f"({query}) AND action:month"),
                ]
            )
            try:
                payload = json.loads(
                    fetcher(f"https://api.legiscan.com/?{fallback_params}").decode("utf-8", errors="replace")
                )
                api_status = str(payload.get("status") or "OK").upper()
            except Exception:  # noqa: BLE001
                api_status = "ERROR"

        if api_status != "OK":
            alert = payload.get("alert")
            alert_msg = alert.get("message") if isinstance(alert, dict) else str(alert or "LegiScan API error")
            errors.append(f"{state_code}: {redact_secrets(str(alert_msg), (key_clean,))}")
            continue

        sr = payload.get("searchresult") or {}
        if not isinstance(sr, dict):
            continue
        for k, row in sr.items():
            if k == "summary" or not isinstance(row, dict):
                continue
            try:
                relevance = int(row.get("relevance", 100))
            except (TypeError, ValueError):
                relevance = 100
            if relevance < min_rel:
                continue
            action_date = parse_any_date(str(row.get("last_action_date") or ""))
            if not action_date or action_date < since:
                continue
            bill_num = str(row.get("bill_number") or "").strip()
            raw_bill_id = str(row.get("bill_id") or "").strip()
            bill_id = raw_bill_id or safe_url(row.get("url")) or bill_num
            if not bill_id or bill_id in items:
                continue

            bill_obj: dict[str, Any] | None = None
            if raw_bill_id and detail_calls < 8:
                if pause > 0:
                    time.sleep(pause)
                detail_calls += 1
                bill_obj = _fetch_legiscan_bill_detail(raw_bill_id, key_clean, fetcher)

            official_link = safe_url(
                (bill_obj.get("state_link") if bill_obj else None) or row.get("state_link")
            )
            legiscan_link = safe_url(row.get("url") or row.get("text_url") or row.get("research_url"))
            bill_url = official_link or legiscan_link
            if not bill_url:
                continue

            change_hash = str((bill_obj or {}).get("change_hash") or row.get("change_hash") or "").strip()
            if raw_bill_id and change_hash:
                updated_hashes[raw_bill_id] = change_hash

            hearings = _extract_upcoming_hearings(bill_obj, since) if bill_obj else []
            last_action = sanitize_issue_text(str(row.get("last_action") or ""), 190)
            rel_note = f", {relevance}% match" if "relevance" in row else ""
            summary_parts = [f"Last action ({action_date.isoformat()}{rel_note}): {last_action}"]
            if hearings:
                summary_parts.append("Calendar: " + "; ".join(hearings))

            src_label = (
                f"LegiScan ({state_code} — linked to official legislature)"
                if official_link
                else f"LegiScan ({state_code} — verify in official legislature)"
            )
            fp_key = f"{state_code}:{bill_id}:{change_hash or action_date.isoformat()}"
            items[bill_id] = {
                "id": item_fingerprint(desk, "legiscan", fp_key),
                "tier": "lead",
                "wire_group": "gov_and_legislative",
                "channel_kind": "legiscan",
                "is_enacted_law": bool(LEGISCAN_ENACTED_RE.search(last_action)),
                "jurisdiction": state_code,
                "source": src_label,
                "date": action_date.isoformat(),
                "title": sanitize_link_label(f"{state_code} {bill_num}: {row.get('title') or ''}", 190),
                "url": bill_url,
                "summary": sanitize_issue_text(" · ".join(summary_parts), 260),
            }

    total_specs = len(queries) + len(tracked_bills)
    if errors and not items and len(errors) >= total_specs:
        return [], updated_hashes, f"error ({'; '.join(errors)})"
    states_label = ", ".join(s for s, _ in queries) or "tracked bills"
    if errors:
        return (
            list(items.values()),
            updated_hashes,
            f"ok ({len(items)} bill action(s); partial warning: {'; '.join(errors)})",
        )
    return list(items.values()), updated_hashes, f"ok ({len(items)} bill action(s) across {states_label})"


def poll_rss(
    desk: str,
    since: dt.date,
    fetcher: Callable[..., bytes],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], str]:
    cfg = _get_desk_cfg(desk, profiles)
    feeds = cfg["rss_feeds"]
    if not feeds:
        return [], "skipped (no RSS feeds configured)"

    primary_re = cfg["news_primary_re"]
    context_re = cfg["news_context_re"]
    atom_ns = {"a": "http://www.w3.org/2005/Atom"}

    items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for idx, (feed_name, feed_url, prefiltered) in enumerate(feeds):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        try:
            raw_xml = fetcher(feed_url)
            root = ET.fromstring(raw_xml)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{feed_name}: {type(exc).__name__}: {exc}")
            continue

        entries: list[tuple[dt.date | None, str, str, str]] = []
        rss_items = root.findall(".//item")
        if rss_items:
            for el in rss_items:
                entries.append(
                    (
                        parse_any_date(el.findtext("pubDate") or el.findtext("{http://purl.org/dc/elements/1.1/}date")),
                        sanitize_link_label(el.findtext("title"), 195),
                        sanitize_issue_text(el.findtext("description"), 240),
                        safe_url(el.findtext("link")),
                    )
                )
        else:
            for el in root.findall("a:entry", atom_ns):
                link_href = ""
                for lk in el.findall("a:link", atom_ns):
                    if lk.attrib.get("rel", "alternate") == "alternate":
                        link_href = safe_url(lk.attrib.get("href"))
                        if link_href:
                            break
                entries.append(
                    (
                        parse_any_date(el.findtext("a:published", "", atom_ns) or el.findtext("a:updated", "", atom_ns)),
                        sanitize_link_label(el.findtext("a:title", "", atom_ns), 195),
                        sanitize_issue_text(el.findtext("a:summary", "", atom_ns), 240),
                        link_href,
                    )
                )

        for pub, title, desc, link in entries:
            if not pub or pub < since or not link:
                continue
            combined = f"{title} {desc}"
            if prefiltered:
                if context_re and not context_re.search(combined) and primary_re and not primary_re.search(combined):
                    continue
            else:
                if primary_re and not primary_re.search(combined):
                    continue
                if context_re and not context_re.search(combined):
                    continue
            norm_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            dedupe_key = norm_title if len(norm_title) >= 24 else link
            items.setdefault(
                dedupe_key,
                {
                    "id": item_fingerprint(desk, "rss", dedupe_key),
                    "tier": "lead",
                    "wire_group": "journalism",
                    "channel_kind": "rss",
                    "urgency": "lead",
                    "source": f"{feed_name} (Lead — verify in primary .gov source)",
                    "date": pub.isoformat(),
                    "title": title,
                    "url": link,
                    "summary": desc,
                },
            )

    if errors and len(errors) == len(feeds):
        return [], f"error ({'; '.join(errors)})"
    if errors:
        return (
            list(items.values()),
            f"ok ({len(items)} match(es) across {len(feeds) - len(errors)}/{len(feeds)} feed(s); partial warning: {'; '.join(errors)})",
        )
    return list(items.values()), f"ok ({len(items)} match(es) across {len(feeds)} feed(s))"


_GEMINI_VER_RE = re.compile(r"gemini-(\d+)(?:\.(\d+))?")


def _gemini_model_sort_key(name: str) -> tuple[int, int, int, str]:
    """Rank stable GA Gemini models ahead of `-preview`/`-exp`, then by numeric `(major, minor)`."""
    low = name.lower()
    is_stable = 1 if not any(tag in low for tag in ("preview", "exp", "experimental", "alpha", "beta")) else 0
    m = _GEMINI_VER_RE.search(low)
    major = int(m.group(1)) if m else 0
    minor = int(m.group(2) or 0) if m else 0
    return (is_stable, major, minor, low)


def discover_gemini_flash_models(api_key: str, fetcher: Callable[..., bytes]) -> list[str]:
    raw = fetcher(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": api_key.strip()},
    )
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    excluded = (
        "tts",
        "live",
        "image",
        "omni",
        "transcribe",
        "embedding",
        "aqa",
        "robotics",
        "banana",
        "2.0-flash-lite",
    )
    lite_models: list[str] = []
    flash_models: list[str] = []
    for entry in payload.get("models") or []:
        if not isinstance(entry, dict):
            continue
        methods = entry.get("supportedGenerationMethods") or []
        name = str(entry.get("name") or "").removeprefix("models/")
        low = name.lower()
        if "generateContent" not in methods or "flash" not in low:
            continue
        if any(bad in low for bad in excluded):
            continue
        if "lite" in low:
            lite_models.append(name)
        else:
            flash_models.append(name)

    # Prefer stable lite -> stable flash -> preview lite -> preview flash.
    stable_lite = sorted([m for m in lite_models if _gemini_model_sort_key(m)[0] == 1], key=_gemini_model_sort_key, reverse=True)
    stable_flash = sorted([m for m in flash_models if _gemini_model_sort_key(m)[0] == 1], key=_gemini_model_sort_key, reverse=True)
    preview_lite = sorted([m for m in lite_models if _gemini_model_sort_key(m)[0] == 0], key=_gemini_model_sort_key, reverse=True)
    preview_flash = sorted([m for m in flash_models if _gemini_model_sort_key(m)[0] == 0], key=_gemini_model_sort_key, reverse=True)
    return stable_lite + stable_flash + preview_lite + preview_flash


def _call_gemini_models(
    api_key: str,
    req_body: bytes,
    fetcher: Callable[..., bytes],
    *,
    preferred_model: str = "",
    default_models: tuple[str, ...] = GEMINI_TRIAGE_MODELS,
) -> tuple[bytes | None, str, str, Exception | None]:
    candidates = [m for m in (preferred_model.strip(), *default_models) if m]
    seen_tried: set[str] = set()
    raw: bytes | None = None
    used_model = ""
    last_err: Exception | None = None
    quota_model = ""

    def _try_model(model: str) -> bytes | None:
        nonlocal quota_model, last_err
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            return fetcher(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key.strip(),
                },
                method="POST",
                body=req_body,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and not quota_model:
                quota_model = model
            if not quota_model or exc.code == 429:
                last_err = exc
            if exc.code in (400, 404, 429, 503):
                return None
            raise

    for model in candidates:
        if model in seen_tried:
            continue
        seen_tried.add(model)
        raw = _try_model(model)
        if raw is not None:
            used_model = model
            break

    if raw is None:
        try:
            discovered = discover_gemini_flash_models(api_key, fetcher)
        except Exception as exc:  # noqa: BLE001
            discovered = []
            if last_err is None:
                last_err = exc
        for model in discovered:
            if model in seen_tried:
                continue
            seen_tried.add(model)
            raw = _try_model(model)
            if raw is not None:
                used_model = model
                break

    return raw, used_model, quota_model, last_err


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    for cand in payload.get("candidates") or []:
        parts = ((cand.get("content") or {}).get("parts")) or []
        for part in parts:
            if isinstance(part, dict) and part.get("text") and not part.get("thought"):
                return str(part["text"]).strip()
    return ""


def poll_tavily_search(
    desk: str,
    since: dt.date,
    tavily_key: str,
    fetcher: Callable[..., bytes],
    *,
    today: dt.date | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    cfg = _get_desk_cfg(desk, profiles)
    dated_queries = cfg["tavily_queries"]
    undated_queries = cfg.get("tavily_undated_queries") or ()
    domains = list(cfg["tavily_domains"])
    reg_domains = list(cfg.get("tavily_registry_domains") or domains)
    key_clean = tavily_key.strip()
    now_date = today or dt.date.today()

    by_url: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    credits_used = 0

    query_plan: list[tuple[str, bool, list[str]]] = [
        *((q, True, domains) for q in dated_queries),
        *((q, False, reg_domains) for q in undated_queries),
    ]

    for idx, (query, use_start_date, q_domains) in enumerate(query_plan):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        payload_dict: dict[str, Any] = {
            "query": query,
            "search_depth": cfg["tavily_search_depth"],
            "topic": "general",
            "max_results": cfg["tavily_max_results"],
            "include_published_date": True,
        }
        if use_start_date:
            payload_dict["start_date"] = since.isoformat()
            payload_dict["filter_by_published_date"] = False
        if q_domains:
            payload_dict["include_domains"] = q_domains
            payload_dict["include_domains_mode"] = "restrict"

        req_body = json.dumps(payload_dict).encode("utf-8")
        try:
            raw = fetcher(
                "https://api.tavily.com/search",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key_clean}",
                },
                method="POST",
                body=req_body,
            )
            credits_used += 2 if cfg["tavily_search_depth"] == "advanced" else 1
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            label = f"q{idx + 1}" if use_start_date else f"undated_q{idx + 1 - len(dated_queries)}"
            errors.append(f"{label}: {type(exc).__name__}: {redact_secrets(str(exc), (key_clean,))}")
            continue

        for row in payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            url = safe_url(row.get("url"))
            if not url:
                continue
            pub_raw = str(row.get("published_date") or "").strip()
            pub_date = parse_any_date(pub_raw) if pub_raw else None
            if pub_date is not None and pub_date < since:
                continue
            try:
                score = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            title = sanitize_link_label(row.get("title") or url, 190)
            snippet = sanitize_issue_text(row.get("content") or "", 340)
            content_fp = _content_snippet_hash(title, snippet)
            existing = by_url.get(url)
            if existing is None or score > existing["score"]:
                by_url[url] = {
                    "title": title,
                    "url": url,
                    "date": (pub_date or now_date).isoformat(),
                    "published_date": pub_raw,
                    "score": score,
                    "snippet": snippet,
                    "content_fp": content_fp,
                    "from_undated_sweep": not use_start_date,
                }

    ranked = sorted(by_url.values(), key=lambda c: c["score"], reverse=True)
    return ranked, credits_used, errors


def _build_tavily_item(
    desk: str,
    candidate: dict[str, Any],
    *,
    source_label: str,
    summary: str,
    urgency: str = "watch",
    jurisdiction: str = "",
    allowed_jurs: set[str] | None = None,
    prior_url_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    norm_u = normalize_url_key(candidate["url"]) or candidate["url"]
    content_fp = candidate.get("content_fp") or _content_snippet_hash(candidate["title"], candidate["snippet"])
    prev_hash = (prior_url_hashes or {}).get(norm_u, "")
    final_summary = summary
    if prev_hash and prev_hash != content_fp:
        final_summary = sanitize_issue_text(
            f"{summary} (Updated text on previously seen .gov/court URL.)",
            300,
        )
    entry: dict[str, Any] = {
        "id": item_fingerprint(desk, "tavily", f"{norm_u}:{content_fp}"),
        "tier": "lead",
        "wire_group": "gov_and_legislative",
        "channel_kind": "tavily",
        "urgency": urgency,
        "source": source_label,
        "date": candidate["date"],
        "title": candidate["title"],
        "url": candidate["url"],
        "summary": final_summary or "Official .gov/court web match; open primary page to verify date and text.",
        "_norm_url": norm_u,
        "_content_fp": content_fp,
    }
    if jurisdiction and (allowed_jurs is None or jurisdiction in allowed_jurs):
        entry["jurisdiction"] = jurisdiction
    return entry


def triage_tavily_with_gemini(
    desk: str,
    since: dt.date,
    candidates: list[dict[str, Any]],
    gemini_key: str,
    fetcher: Callable[..., bytes],
    *,
    today: dt.date | None = None,
    preferred_model: str = "",
    profiles: dict[str, dict[str, Any]] | None = None,
    registry_norm_urls: set[str] | None = None,
    prior_url_hashes: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    cfg = _get_desk_cfg(desk, profiles)
    now_date = today or dt.date.today()
    reg_urls = registry_norm_urls if registry_norm_urls is not None else set(build_desk_impact_index(cfg)["by_norm_url"])
    batch = candidates[:15]
    compact = [
        {
            "index": idx,
            "title": c["title"],
            "url": c["url"],
            "published_date": c["published_date"],
            "snippet": c["snippet"],
            "cited_on_civicdesk": normalize_url_key(c["url"]) in reg_urls,
        }
        for idx, c in enumerate(batch)
    ]
    jur_codes = ", ".join((cfg.get("jurisdictions") or {"US": {}}).keys())
    prompt = (
        f"You are the editorial wire desk triaging official government (.gov, state legislature, state court) and "
        f"federal court search results for CivicDesk's '{cfg['label']}' desk.\n"
        f"Desk scope: {cfg['editorial_brief']}\n"
        f"Covered jurisdictions: {jur_codes}. Today is {now_date.isoformat()}; lookback starts {since.isoformat()}.\n\n"
        f"Keep ONLY candidates that announce or reflect a concrete statute/rule update, agency bulletin or guidance, "
        f"form or packet revision, filing fee schedule change, implementation timeline update, or court filing/order "
        f"published or updated on or after {since.isoformat()} (or clearly active {now_date.year}/{now_date.year + 1} "
        f"updates when published_date is blank), PLUS any candidate with cited_on_civicdesk=true.\n"
        f"Drop generic evergreen landing pages, agency homepages, directory listings, or older archived documents "
        f"with no recent change.\n\n"
        f"Set urgency='urgent' only for direct updates to cited sources, enacted rules, fee/form revisions, "
        f"effective dates, or court orders; otherwise set urgency='watch'.\n\n"
        f"Candidates:\n{json.dumps(compact, ensure_ascii=False)}"
    )
    req_body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": GEMINI_TRIAGE_SCHEMA,
            },
        }
    ).encode("utf-8")

    raw, used_model, quota_model, last_err = _call_gemini_models(
        gemini_key,
        req_body,
        fetcher,
        preferred_model=preferred_model,
        default_models=GEMINI_TRIAGE_MODELS,
    )
    if raw is None:
        if quota_model:
            raise RuntimeError(f"HTTP 429 quota exhausted on {quota_model}")
        if last_err is not None:
            raise last_err
        raise RuntimeError("No compatible Gemini Flash model available")

    payload = json.loads(raw.decode("utf-8", errors="replace"))
    raw_text = _extract_gemini_text(payload)
    cleaned_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.IGNORECASE)
    parsed = json.loads(cleaned_json or '{"kept": []}')
    if not isinstance(parsed, dict) or not isinstance(parsed.get("kept"), list):
        raise ValueError("Gemini triage response did not match required {'kept': [...]} schema")
    kept_rows = parsed["kept"]

    items: dict[str, dict[str, Any]] = {}
    kept_indices: set[int] = set()
    allowed_jurs = set((cfg.get("jurisdictions") or {}).keys())
    for row in kept_rows:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(batch):
            continue
        kept_indices.add(idx)
        src = batch[idx]
        summary = sanitize_issue_text(str(row.get("summary") or ""), 220) or src["snippet"]
        hint = sanitize_issue_text(str(row.get("check_hint") or ""), 140)
        if hint:
            summary = sanitize_issue_text(f"{summary} (Verify: {hint})", 300)
        urg = "urgent" if str(row.get("urgency") or "").lower() in {"urgent", "high"} else "watch"
        jur = str(row.get("jurisdiction") or "").upper()
        items[src["url"]] = _build_tavily_item(
            desk,
            src,
            source_label="Tavily + Gemini (.gov / court wire)",
            summary=summary,
            urgency=urg,
            jurisdiction=jur,
            allowed_jurs=allowed_jurs,
            prior_url_hashes=prior_url_hashes,
        )

    # Guardrail: NEVER allow the model to drop a candidate whose URL matches a cited registry source.
    dropped: list[dict[str, Any]] = []
    for idx, src in enumerate(batch):
        if idx in kept_indices:
            continue
        if normalize_url_key(src["url"]) in reg_urls:
            items[src["url"]] = _build_tavily_item(
                desk,
                src,
                source_label="Tavily + Gemini (.gov / court wire — registry match retained)",
                summary=sanitize_issue_text(
                    f"{src['snippet']} (Retained automatically: URL is cited in CivicDesk source registry.)",
                    280,
                ),
                urgency="urgent",
                prior_url_hashes=prior_url_hashes,
            )
            continue
        dropped.append(
            {
                "title": src["title"],
                "url": src["url"],
                "published_date": src["published_date"],
                "score": round(float(src["score"]), 3),
                "reason": f"filtered by {used_model}",
            }
        )
    for src in candidates[15:]:
        if normalize_url_key(src["url"]) in reg_urls and src["url"] not in items:
            items[src["url"]] = _build_tavily_item(
                desk,
                src,
                source_label="Tavily + Gemini (.gov / court wire — registry match retained)",
                summary=sanitize_issue_text(
                    f"{src['snippet']} (Retained automatically: URL is cited in CivicDesk source registry.)",
                    280,
                ),
                urgency="urgent",
                prior_url_hashes=prior_url_hashes,
            )
        else:
            dropped.append(
                {
                    "title": src["title"],
                    "url": src["url"],
                    "published_date": src["published_date"],
                    "score": round(float(src["score"]), 3),
                    "reason": "outside top-15 triage batch",
                }
            )

    return list(items.values()), used_model, dropped


def _fallback_tavily_items(
    desk: str,
    cfg: dict[str, Any],
    candidates: list[dict[str, Any]],
    source_label: str,
    *,
    registry_norm_urls: set[str] | None = None,
    prior_url_hashes: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reg_urls = registry_norm_urls or set()
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for c in candidates:
        is_reg = normalize_url_key(c["url"]) in reg_urls
        threshold = cfg["tavily_min_score_dated"] if c["published_date"] else cfg["tavily_min_score_undated"]
        if not is_reg and c["score"] < threshold:
            dropped.append(
                {
                    "title": c["title"],
                    "url": c["url"],
                    "published_date": c["published_date"],
                    "score": round(float(c["score"]), 3),
                    "reason": f"score {c['score']:.2f} < {threshold:.2f}",
                }
            )
            continue
        kept.append(
            _build_tavily_item(
                desk,
                c,
                source_label=source_label,
                summary=c["snippet"] or "Official .gov/court web match; open primary page to verify date and text.",
                urgency="urgent" if is_reg else "watch",
                prior_url_hashes=prior_url_hashes,
            )
        )
    return kept[:12], dropped


def poll_gemini_grounding(
    desk: str,
    since: dt.date,
    api_key: str,
    fetcher: Callable[..., bytes],
    *,
    today: dt.date | None = None,
    preferred_model: str = "",
    profiles: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    if not api_key.strip():
        return [], "skipped (GEMINI_API_KEY not set)"
    cfg = _get_desk_cfg(desk, profiles)
    now_date = today or dt.date.today()
    prompt = (
        f"Search official government (.gov, state legislature, state court) and federal court sources for any new "
        f"rules, fee schedules, agency forms, bulletins, or court orders published on or after {since.isoformat()} "
        f"for CivicDesk's '{cfg['label']}' desk ({cfg['editorial_brief']}). "
        f"Summarize only concrete updates since {since.isoformat()}."
    )
    req_body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        }
    ).encode("utf-8")

    raw, used_model, quota_model, last_err = _call_gemini_models(
        api_key,
        req_body,
        fetcher,
        preferred_model=preferred_model,
        default_models=GEMINI_GROUNDING_MODELS,
    )
    if raw is None:
        if quota_model:
            return (
                [],
                f"skipped (HTTP 429 quota exhausted on {quota_model}; "
                "Google Search Grounding requires AI Studio Paid Tier 1, while free-tier Gemini JSON triage works with TAVILY_API_KEY)",
            )
        if last_err is not None:
            raise last_err
        return [], "error (no compatible Gemini Flash model available)"

    payload = json.loads(raw.decode("utf-8", errors="replace"))
    note_text = sanitize_issue_text(_extract_gemini_text(payload), 220)
    items: dict[str, dict[str, Any]] = {}
    for cand in payload.get("candidates") or []:
        meta = cand.get("groundingMetadata") or {}
        for chunk in meta.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            uri = safe_url(web.get("uri"))
            if not uri:
                continue
            title = sanitize_link_label(web.get("title") or uri, 180)
            items[uri] = {
                "id": item_fingerprint(desk, "gemini-grounding", uri),
                "tier": "lead",
                "wire_group": "gov_and_legislative",
                "channel_kind": "gemini_grounding",
                "urgency": "watch",
                "source": "Gemini Search Grounding",
                "date": now_date.isoformat(),
                "title": title,
                "url": uri,
                "summary": note_text or "Grounded search citation; open primary .gov source to verify date and text.",
            }
    return list(items.values()), f"ok ({used_model}, {len(items)} grounded citation(s))"


def poll_tavily_and_gemini(
    desk: str,
    since: dt.date,
    tavily_key: str,
    gemini_key: str,
    fetcher: Callable[..., bytes],
    *,
    today: dt.date | None = None,
    preferred_model: str = "",
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
    registry_norm_urls: set[str] | None = None,
    prior_url_hashes: dict[str, str] | None = None,
    out_dropped: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    cfg = _get_desk_cfg(desk, profiles)
    total_queries = len(cfg["tavily_queries"]) + len(cfg.get("tavily_undated_queries") or ())
    if total_queries == 0:
        return [], 0, "skipped (no Tavily queries configured)"

    if not tavily_key.strip():
        if gemini_key.strip():
            items, gem_status = poll_gemini_grounding(
                desk,
                since,
                gemini_key,
                fetcher,
                today=today,
                preferred_model=preferred_model,
                profiles=profiles,
            )
            if gem_status.startswith("skipped (HTTP 429"):
                return (
                    [],
                    0,
                    "skipped (TAVILY_API_KEY not set; free-tier GEMINI_API_KEY ready for JSON triage once TAVILY_API_KEY is set)",
                )
            return items, 0, gem_status
        return [], 0, "skipped (TAVILY_API_KEY and GEMINI_API_KEY not set)"

    reg_urls = registry_norm_urls if registry_norm_urls is not None else set(build_desk_impact_index(cfg)["by_norm_url"])

    candidates, credits_used, errors = poll_tavily_search(
        desk,
        since,
        tavily_key,
        fetcher,
        today=today,
        profiles=profiles,
        pause=pause,
    )
    if errors and not candidates and len(errors) >= total_queries:
        return [], credits_used, f"error ({'; '.join(errors)})"

    warn_suffix = f"; partial warning: {'; '.join(errors)}" if errors else ""
    if not candidates:
        return (
            [],
            credits_used,
            f"ok (0 .gov/court match(es) across {total_queries} Tavily queries, {credits_used} credit(s){warn_suffix})",
        )

    if gemini_key.strip():
        try:
            kept, used_model, dropped = triage_tavily_with_gemini(
                desk,
                since,
                candidates,
                gemini_key,
                fetcher,
                today=today,
                preferred_model=preferred_model,
                profiles=profiles,
                registry_norm_urls=reg_urls,
                prior_url_hashes=prior_url_hashes,
            )
            if out_dropped is not None:
                out_dropped.extend(dropped)
            return (
                kept,
                credits_used,
                f"ok ({len(kept)} .gov/court lead(s) from {len(candidates)} Tavily candidate(s), {credits_used} credit(s), triaged via {used_model}{warn_suffix})",
            )
        except Exception as exc:  # noqa: BLE001
            fallback, dropped = _fallback_tavily_items(
                desk,
                cfg,
                candidates,
                "Tavily Search (.gov / court wire)",
                registry_norm_urls=reg_urls,
                prior_url_hashes=prior_url_hashes,
            )
            if out_dropped is not None:
                out_dropped.extend(dropped)
            err_note = redact_secrets(f"{type(exc).__name__}: {exc}", (gemini_key, tavily_key))
            return (
                fallback,
                credits_used,
                f"ok ({len(fallback)} .gov/court lead(s) from {len(candidates)} Tavily candidate(s), {credits_used} credit(s); Gemini triage fallback: {err_note}{warn_suffix})",
            )

    fallback, dropped = _fallback_tavily_items(
        desk,
        cfg,
        candidates,
        "Tavily Search (.gov / court wire)",
        registry_norm_urls=reg_urls,
        prior_url_hashes=prior_url_hashes,
    )
    if out_dropped is not None:
        out_dropped.extend(dropped)
    return (
        fallback,
        credits_used,
        f"ok ({len(fallback)} .gov/court lead(s) from {len(candidates)} Tavily candidate(s), {credits_used} credit(s); GEMINI_API_KEY not set{warn_suffix})",
    )


def generate_ai_wire_brief(
    cfg: dict[str, Any],
    items: list[dict[str, Any]],
    gemini_key: str,
    fetcher: Callable[..., bytes],
    *,
    preferred_model: str = "",
) -> list[str]:
    if not items or not gemini_key.strip():
        return []
    compact = [
        {
            "date": i["date"],
            "urgency": i.get("urgency", "watch"),
            "tier": i["tier"],
            "jurisdictions": i.get("jurisdictions", ["US"]),
            "source": i["source"],
            "title": i["title"],
            "summary": i.get("summary", ""),
            "matched_sources": i.get("matched_sources", []),
            "matched_routes": i.get("matched_routes", []),
        }
        for i in items[:15]
    ]
    prompt = (
        f"You are preparing a calm, plain-language executive wire summary for the maintainer of CivicDesk's "
        f"'{cfg['label']}' desk. Summarize the most important items to review in 2 to 4 short literal bullets "
        f"(active voice, no idioms, no legal advice, clearly distinguishing primary government/court filings from "
        f"unconfirmed news or bill leads).\n\n"
        f"Dispatches:\n{json.dumps(compact, ensure_ascii=False)}"
    )
    req_body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": GEMINI_BRIEF_SCHEMA,
            },
        }
    ).encode("utf-8")
    raw, _, _, _ = _call_gemini_models(
        gemini_key,
        req_body,
        fetcher,
        preferred_model=preferred_model,
        default_models=GEMINI_TRIAGE_MODELS,
    )
    if raw is None:
        return []
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    raw_text = _extract_gemini_text(payload)
    cleaned_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.IGNORECASE)
    parsed = json.loads(cleaned_json or '{"bullets": []}')
    bullets = parsed.get("bullets") if isinstance(parsed, dict) else []
    if not isinstance(bullets, list):
        return []
    return [sanitize_issue_text(str(b), 220) for b in bullets[:4] if str(b).strip()]


def _classify_status_kind(status_text: str) -> str:
    low = status_text.strip().lower()
    if low.startswith("skipped"):
        return "skipped"
    if low.startswith("error"):
        return "error"
    if "partial warning:" in low or "gemini triage fallback:" in low:
        return "warning"
    return "ok"


def _channel_since_date(
    state: dict[str, Any],
    channel_key: str,
    default_since: dt.date,
    today: dt.date,
    explicit_since: dt.date | None,
) -> dt.date:
    if explicit_since is not None:
        return explicit_since
    ch_meta = (state.get("channels") or {}).get(channel_key) or {}
    last_ok = parse_any_date(ch_meta.get("last_success"))
    if not last_ok:
        return default_since
    overlap_start = last_ok - dt.timedelta(days=OVERLAP_DAYS)
    floor_date = today - dt.timedelta(days=MAX_LOOKBACK_DAYS)
    return max(floor_date, min(default_since, overlap_start))


def evaluate_wire_health(
    statuses: list[dict[str, Any]],
    channel_state: dict[str, dict[str, Any]],
    *,
    empty_streak_threshold: int = DEFAULT_EMPTY_STREAK_THRESHOLD,
) -> tuple[list[str], bool]:
    """Identify channel errors, keyed degradations, empty streaks, and total sweep failure."""
    alerts: list[str] = []
    active = [st for st in statuses if st.get("kind") != "skipped"]
    all_failed = bool(active) and all(st.get("kind") == "error" for st in active)

    for st in statuses:
        name = st["source"]
        ch_key = st.get("channel_key", name)
        kind = st.get("kind", "ok")
        status_str = st["status"]
        if kind == "error":
            alerts.append(f"{name}: {status_str}")
            continue
        if kind == "warning" and st.get("keyed"):
            alerts.append(f"{name}: degraded keyed channel ({status_str})")
            continue
        if kind in {"ok", "warning"}:
            ch_hist = channel_state.get(ch_key) or {}
            streak = int(ch_hist.get("consecutive_empty_runs") or 0)
            expect_nonempty = bool(st.get("expect_nonempty"))
            ever_matched = bool(ch_hist.get("ever_matched"))
            threshold = empty_streak_threshold if expect_nonempty else (empty_streak_threshold * 2)
            if (expect_nonempty or ever_matched) and streak >= threshold:
                alerts.append(
                    f"{name}: returned 0 raw items for {streak} consecutive run(s) (threshold {threshold})"
                )

    return alerts, all_failed


def send_heartbeat(
    heartbeat_url: str,
    *,
    healthy: bool,
    summary: str,
    fetcher: Callable[..., bytes] = default_fetcher,
) -> str:
    """Send an external dead-man's-switch ping (`WIRE_HEARTBEAT_URL` or `.../fail`)."""
    clean = heartbeat_url.strip()
    if not clean:
        return "skipped"
    target = clean if healthy else f"{clean.rstrip('/')}/fail"
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "error (invalid WIRE_HEARTBEAT_URL)"
    try:
        fetcher(
            target,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
            body=summary.encode("utf-8")[:1000],
        )
        return "ok" if healthy else "ok (sent /fail signal)"
    except Exception as exc:  # noqa: BLE001
        return f"error ({type(exc).__name__})"


def _format_wire_bullet(item: dict[str, Any]) -> list[str]:
    urg_label = {"urgent": "URGENT", "watch": "WATCH", "lead": "LEAD"}.get(
        str(item.get("urgency") or "lead"),
        "LEAD",
    )
    jurs = "/".join(item.get("jurisdictions") or ["US"])
    title_safe = sanitize_link_label(item.get("title"), 195)
    url_safe = safe_url(item.get("url"))
    src_safe = sanitize_issue_text(item.get("source"), 120)
    lines = [
        f"- **[{urg_label} · {jurs}]** **{item['date']}** · [{title_safe}]({url_safe}) — *{src_safe}* <!-- radar-item:{item['id']} -->"
    ]
    if item.get("summary"):
        lines.append(f"  - {sanitize_issue_text(item['summary'], 300)}")
    impact_parts: list[str] = []
    if item.get("matched_sources"):
        impact_parts.append("Sources: " + ", ".join(f"`{s}`" for s in item["matched_sources"]))
    if item.get("matched_facts"):
        impact_parts.append("Facts: " + ", ".join(f"`{f}`" for f in item["matched_facts"]))
    if item.get("matched_routes"):
        impact_parts.append("Routes: " + ", ".join(f"`{r}`" for r in item["matched_routes"]))
    if impact_parts:
        lines.append(f"  - **CivicDesk impact map:** {' · '.join(impact_parts)}")
    return lines


def _render_report_internal(
    result: dict[str, Any],
    *,
    max_chars: int = MAX_ISSUE_BODY_CHARS,
) -> tuple[str, list[str], int]:
    """Render Markdown wire report while guaranteeing channel telemetry and marker integrity."""
    desk = result["desk"]
    head_lines = [
        f"## CivicDesk Policy Wire — `{desk}` ({result['desk_label']})",
        "",
        (
            f"**Window:** `{result['since']}` to `{result['today']}` · "
            f"**Unseen dispatches:** **{result['new_count']}** "
            f"(`{result.get('urgent_count', 0)}` urgent · "
            f"`{result.get('watch_count', 0)}` watch · "
            f"`{result.get('lead_count', 0)}` journalism)"
        ),
        "",
        (
            "> Read-only wire report (`User-Agent: CivicDesk-Verify/1.0`). Nothing in this workflow modifies "
            "repository files. Items under `not_official` are leads only and must be verified directly in a "
            "primary `.gov` statute, administrative rule, agency publication, or court docket before citing."
        ),
        "",
    ]

    if result.get("health_alerts"):
        head_lines.append("### Wire health alerts (`wire:health`)")
        head_lines.append("")
        for alert in result["health_alerts"]:
            head_lines.append(f"- {sanitize_issue_text(alert, 260)}")
        head_lines.append("")

    if result.get("touched_sources") or result.get("touched_routes"):
        parts: list[str] = []
        if result.get("touched_sources"):
            parts.append("Registry IDs: " + ", ".join(f"`{s}`" for s in result["touched_sources"][:8]))
        if result.get("touched_facts"):
            parts.append("Tracked facts: " + ", ".join(f"`{f}`" for f in result["touched_facts"][:6]))
        if result.get("touched_routes"):
            parts.append("Guide routes: " + ", ".join(f"`{r}`" for r in result["touched_routes"][:8]))
        head_lines.append("**Potentially affected on CivicDesk:** " + " · ".join(parts))
        head_lines.append("")

    if result.get("ai_brief"):
        head_lines.append("### Editorial triage summary *(machine-generated — verify primary sources)*")
        head_lines.append("")
        for bullet in result["ai_brief"]:
            head_lines.append(f"- {sanitize_issue_text(bullet, 240)}")
        head_lines.append("")

    # Build the mandatory footer FIRST so truncation can NEVER cut off telemetry or dropped-candidate audit trails.
    footer_lines: list[str] = []
    if result.get("already_reported_count"):
        footer_lines.append(
            f"*{result['already_reported_count']} matching item(s) from this window were already reported earlier and skipped.*"
        )
        footer_lines.append("")

    dropped = result.get("dropped_candidates") or []
    if dropped:
        footer_lines.extend(
            [
                f"<details><summary>Dropped `.gov` / court search candidates ({len(dropped)})</summary>",
                "",
            ]
        )
        for dc in dropped[:20]:
            d_title = sanitize_link_label(dc.get("title") or dc.get("url"), 140)
            d_url = safe_url(dc.get("url"))
            d_pub = dc.get("published_date") or "undated"
            d_reason = sanitize_issue_text(dc.get("reason") or "filtered", 90)
            footer_lines.append(f"- [{d_title}]({d_url}) (`{d_pub}`, score `{dc.get('score', 0)}`, {d_reason})")
        if len(dropped) > 20:
            footer_lines.append(f"- *(+{len(dropped) - 20} additional low-score candidate(s) omitted)*")
        footer_lines.extend(["", "</details>", ""])

    footer_lines.extend(
        [
            "### Wire channel telemetry",
            "",
            "| Channel | Status |",
            "|---|---|",
        ]
    )
    for st in result["statuses"]:
        src = str(st["source"]).replace("|", "/")
        val = str(st["status"]).replace("|", "/").replace("\n", " ")
        footer_lines.append(f"| `{src}` | {val} |")
    footer_lines.append("")

    footer_lines.extend(
        [
            "### Next step (Antigravity verification workflow)",
            "",
            "```text",
            f"Triage open radar:{desk} issues in radiofreelove/civicdesk against primary .gov and court sources per AGENTS.md and docs/RADAR-RUNBOOK.md, run build.py, check_brand_purge.py, and pytest, and open a Draft PR.",
            "```",
            "",
        ]
    )

    head_block = "\n".join(head_lines)
    footer_block = "\n".join(footer_lines)
    reserve_notice_budget = 220
    available_item_chars = max(600, max_chars - len(head_block) - len(footer_block) - reserve_notice_budget)

    items: list[dict[str, Any]] = result["items"]
    if not items:
        mid = "No new unseen dispatches matched during this lookback window.\n\n"
        return f"{head_block}\n{mid}{footer_block}", [], 0

    sections_spec = [
        (
            "### 1. Primary official dispatches (`official` — Page Watch, Federal Register, eCFR, State Registers, CourtListener)",
            [i for i in items if i["tier"] == "primary"],
        ),
        (
            "### 2. Official-domain & legislative wire (`not_official` leads — Tavily + Gemini, LegiScan)",
            [i for i in items if i["tier"] != "primary" and i.get("wire_group") == "gov_and_legislative"],
        ),
        (
            "### 3. Policy & state journalism wire (`not_official` context — verify in `.gov` before citing)",
            [i for i in items if i["tier"] != "primary" and i.get("wire_group") != "gov_and_legislative"],
        ),
    ]

    mid_lines: list[str] = []
    used_chars = 0
    included_ids: list[str] = []
    omitted_count = 0

    for sec_heading, sec_items in sections_spec:
        if not sec_items:
            continue
        sec_rendered: list[str] = []
        for item in sec_items:
            bullet_block = "\n".join(_format_wire_bullet(item))
            extra_cost = len(bullet_block) + 1 + (len(sec_heading) + 3 if not sec_rendered else 0)
            if used_chars + extra_cost > available_item_chars and included_ids:
                omitted_count += 1
                continue
            if not sec_rendered:
                sec_rendered.extend([sec_heading, ""])
                used_chars += len(sec_heading) + 2
            sec_rendered.append(bullet_block)
            used_chars += len(bullet_block) + 1
            included_ids.append(item["id"])
        if sec_rendered:
            mid_lines.extend(sec_rendered)
            mid_lines.append("")

    if omitted_count > 0:
        mid_lines.append(
            f"*({omitted_count} additional lower-priority dispatch(es) deferred to the next sweep to stay under "
            "GitHub's 60,000-character issue limit while keeping full channel telemetry intact.)*"
        )
        mid_lines.append("")

    mid_block = "\n".join(mid_lines)
    return f"{head_block}\n{mid_block}\n{footer_block}", included_ids, omitted_count


def render_markdown_report(result: dict[str, Any], *, max_chars: int = MAX_ISSUE_BODY_CHARS) -> str:
    body, _included_ids, _omitted = _render_report_internal(result, max_chars=max_chars)
    return body


def run_sweep(
    desk: str,
    *,
    days: int = 7,
    since: dt.date | None = None,
    today: dt.date | None = None,
    seen_markers: set[str] | None = None,
    wire_state: dict[str, Any] | None = None,
    fetcher: Callable[..., bytes] = default_fetcher,
    env: dict[str, str] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
    ai_brief: bool = False,
    max_report_chars: int = MAX_ISSUE_BODY_CHARS,
) -> dict[str, Any]:
    cfg = _get_desk_cfg(desk, profiles)
    now_date = today or dt.datetime.now(dt.timezone.utc).date()
    default_since = since or (now_date - dt.timedelta(days=max(1, min(days, MAX_LOOKBACK_DAYS))))
    state_in = dict(empty_wire_state(desk) if wire_state is None else wire_state)

    seen = set(seen_markers or set())
    seen.update(str(x) for x in (state_in.get("reported_ids") or []) if x)

    cfg_env = env if env is not None else dict(os.environ)
    legiscan_key = (cfg_env.get("LEGISCAN_API_KEY") or "").strip()
    cl_token = (cfg_env.get("COURTLISTENER_TOKEN") or "").strip()
    tavily_key = (cfg_env.get("TAVILY_API_KEY") or "").strip()
    gemini_key = (cfg_env.get("GEMINI_API_KEY") or "").strip()
    preferred_gemini = (cfg_env.get("GEMINI_MODEL") or "").strip()
    heartbeat_url = (cfg_env.get("WIRE_HEARTBEAT_URL") or "").strip()
    secrets = (legiscan_key, cl_token, tavily_key, gemini_key, heartbeat_url)

    impact_index = build_desk_impact_index(cfg)
    reg_norm_urls = set(impact_index["by_norm_url"].keys())
    prior_tavily_hashes: dict[str, str] = dict(state_in.get("tavily_url_hashes") or {})

    statuses: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    dropped_candidates: list[dict[str, Any]] = []
    tavily_credits_used = 0
    channel_state_out: dict[str, dict[str, Any]] = {
        str(k): dict(v) for k, v in (state_in.get("channels") or {}).items() if isinstance(v, dict)
    }
    effective_sinces: list[dt.date] = [default_since]

    def _record_status(
        *,
        source_name: str,
        channel_key: str,
        status_text: str,
        raw_count: int,
        keyed: bool = False,
        expect_nonempty: bool = False,
    ) -> None:
        redacted = redact_secrets(status_text, secrets)
        kind = _classify_status_kind(redacted)
        prev = dict(channel_state_out.get(channel_key) or {})
        if kind in {"ok", "warning"}:
            prev["last_success"] = now_date.isoformat()
            if raw_count > 0:
                prev["consecutive_empty_runs"] = 0
                prev["ever_matched"] = True
            else:
                prev["consecutive_empty_runs"] = int(prev.get("consecutive_empty_runs") or 0) + 1
            channel_state_out[channel_key] = prev
        statuses.append(
            {
                "source": source_name,
                "channel_key": channel_key,
                "kind": kind,
                "keyed": keyed,
                "expect_nonempty": expect_nonempty,
                "raw_count": raw_count,
                "status": redacted,
            }
        )

    def _run_channel(
        source_name: str,
        channel_key: str,
        fn: Callable[[dt.date], tuple[list[dict[str, Any]], str]],
        *,
        keyed: bool = False,
        expect_nonempty: bool = False,
    ) -> None:
        ch_since = _channel_since_date(state_in, channel_key, default_since, now_date, since)
        effective_sinces.append(ch_since)
        try:
            found, status = fn(ch_since)
            collected.extend(found)
            _record_status(
                source_name=source_name,
                channel_key=channel_key,
                status_text=status,
                raw_count=len(found),
                keyed=keyed,
                expect_nonempty=expect_nonempty,
            )
        except Exception as exc:  # noqa: BLE001
            err_msg = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
            _record_status(
                source_name=source_name,
                channel_key=channel_key,
                status_text=f"error: {err_msg}",
                raw_count=0,
                keyed=keyed,
                expect_nonempty=expect_nonempty,
            )

    # 1. Keyless Registry Page Watch
    updated_page_fps: dict[str, dict[str, Any]] = dict(state_in.get("page_fingerprints") or {})
    try:
        pw_items, updated_page_fps, pw_probed, pw_status = poll_page_watch(
            desk,
            fetcher,
            today=now_date,
            wire_state=state_in,
            profiles=profiles,
            pause=pause,
        )
        collected.extend(pw_items)
        _record_status(
            source_name="Registry Page Watch",
            channel_key="page_watch",
            status_text=pw_status,
            raw_count=pw_probed,
            keyed=False,
            expect_nonempty=True,
        )
    except Exception as exc:  # noqa: BLE001
        err_msg = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
        _record_status(
            source_name="Registry Page Watch",
            channel_key="page_watch",
            status_text=f"error: {err_msg}",
            raw_count=0,
            keyed=False,
            expect_nonempty=True,
        )

    # 2. Federal Register API v1 + Public Inspection
    _run_channel(
        "Federal Register API",
        "federal_register",
        lambda ch_since: poll_federal_register(desk, ch_since, fetcher, profiles=profiles, pause=pause),
    )

    # 3. eCFR Versioner API v1
    _run_channel(
        "eCFR Versioner API",
        "ecfr",
        lambda ch_since: poll_ecfr(desk, ch_since, fetcher, profiles=profiles, pause=pause),
    )

    # 4. State Registers & Official State Legislative Feeds
    updated_wsr_checked: list[str] = list(state_in.get("wsr_checked_filings") or [])
    sr_since = _channel_since_date(state_in, "state_registers", default_since, now_date, since)
    effective_sinces.append(sr_since)
    try:
        sr_items, updated_wsr_checked, sr_status = poll_state_registers(
            desk,
            sr_since,
            fetcher,
            wire_state=state_in,
            profiles=profiles,
            pause=pause,
        )
        collected.extend(sr_items)
        _record_status(
            source_name="State Registers & Legislatures",
            channel_key="state_registers",
            status_text=sr_status,
            raw_count=len(sr_items),
        )
    except Exception as exc:  # noqa: BLE001
        err_msg = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
        _record_status(
            source_name="State Registers & Legislatures",
            channel_key="state_registers",
            status_text=f"error: {err_msg}",
            raw_count=0,
        )

    # 5. CourtListener RECAP Atom Feeds
    _run_channel(
        "CourtListener RECAP Feeds",
        "courtlistener_recap",
        lambda ch_since: poll_courtlistener_feeds(desk, ch_since, fetcher, profiles=profiles, pause=pause),
    )

    # 6. CourtListener REST API v4
    _run_channel(
        "CourtListener REST v4",
        "courtlistener_v4",
        lambda ch_since: poll_courtlistener_v4(desk, ch_since, cl_token, fetcher, profiles=profiles, pause=pause),
        keyed=bool(cl_token),
    )

    # 7. LegiScan API + `getBill`
    updated_legiscan_hashes: dict[str, str] = dict(state_in.get("legiscan_change_hashes") or {})
    leg_since = _channel_since_date(state_in, "legiscan", default_since, now_date, since)
    effective_sinces.append(leg_since)
    try:
        leg_items, updated_legiscan_hashes, leg_status = poll_legiscan(
            desk,
            leg_since,
            legiscan_key,
            fetcher,
            today=now_date,
            wire_state=state_in,
            profiles=profiles,
            pause=pause,
        )
        collected.extend(leg_items)
        _record_status(
            source_name="LegiScan API",
            channel_key="legiscan",
            status_text=leg_status,
            raw_count=len(leg_items),
            keyed=bool(legiscan_key),
        )
    except Exception as exc:  # noqa: BLE001
        err_msg = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
        _record_status(
            source_name="LegiScan API",
            channel_key="legiscan",
            status_text=f"error: {err_msg}",
            raw_count=0,
            keyed=bool(legiscan_key),
        )

    # 8. Tavily + Gemini Web Wire
    tav_since = _channel_since_date(state_in, "tavily_gemini", default_since, now_date, since)
    effective_sinces.append(tav_since)
    updated_tavily_hashes = dict(prior_tavily_hashes)
    try:
        tav_items, credits, tav_status = poll_tavily_and_gemini(
            desk,
            tav_since,
            tavily_key,
            gemini_key,
            fetcher,
            today=now_date,
            preferred_model=preferred_gemini,
            profiles=profiles,
            pause=pause,
            registry_norm_urls=reg_norm_urls,
            prior_url_hashes=prior_tavily_hashes,
            out_dropped=dropped_candidates,
        )
        tavily_credits_used += credits
        for ti in tav_items:
            nu = ti.pop("_norm_url", None)
            cfp = ti.pop("_content_fp", None)
            if nu and cfp:
                updated_tavily_hashes[str(nu)] = str(cfp)
        collected.extend(tav_items)
        _record_status(
            source_name="Tavily + Gemini Web Wire",
            channel_key="tavily_gemini",
            status_text=tav_status,
            raw_count=len(tav_items) + len(dropped_candidates),
            keyed=bool(tavily_key or gemini_key),
            expect_nonempty=bool(tavily_key),
        )
    except Exception as exc:  # noqa: BLE001
        err_msg = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
        _record_status(
            source_name="Tavily + Gemini Web Wire",
            channel_key="tavily_gemini",
            status_text=f"error: {err_msg}",
            raw_count=0,
            keyed=bool(tavily_key or gemini_key),
            expect_nonempty=bool(tavily_key),
        )

    # 9. Policy & State News RSS
    _run_channel(
        "Policy & State News RSS",
        "rss",
        lambda ch_since: poll_rss(desk, ch_since, fetcher, profiles=profiles, pause=pause),
        expect_nonempty=bool(cfg["rss_feeds"]),
    )

    # Deduplicate within this sweep by fingerprint AND by canonical URL (preferring primary tier over leads).
    urgency_rank = {"urgent": 0, "watch": 1, "lead": 2}
    tier_rank = {"primary": 0, "lead": 1}

    annotated = [annotate_item_impact(raw_item, cfg, impact_index, today=now_date) for raw_item in collected]
    annotated.sort(
        key=lambda x: (
            0 if x.get("channel_kind") == "page_watch" else 1,
            tier_rank.get(x["tier"], 1),
            urgency_rank.get(x.get("urgency", "lead"), 2),
            x["date"],
        )
    )

    unique_items: list[dict[str, Any]] = []
    seen_ids_this_run: set[str] = set()
    seen_norm_urls: set[str] = set()
    for item in annotated:
        norm_u = normalize_url_key(item.get("url"))
        if item["id"] in seen_ids_this_run:
            continue
        if norm_u and norm_u in seen_norm_urls:
            continue
        seen_ids_this_run.add(item["id"])
        if norm_u:
            seen_norm_urls.add(norm_u)
        unique_items.append(item)

    already_reported = [i for i in unique_items if i["id"] in seen]
    unseen = [i for i in unique_items if i["id"] not in seen]

    earliest_since = min(effective_sinces) if effective_sinces else default_since

    def _display_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
        grp_order = {"primary": 0, "gov_and_legislative": 1, "journalism": 2}.get(
            str(item.get("wire_group") or ""),
            0 if item["tier"] == "primary" else 2,
        )
        urg_order = urgency_rank.get(str(item.get("urgency") or "lead"), 2)
        day_ord = -(parse_any_date(item.get("date")) or earliest_since).toordinal()
        return (grp_order, urg_order, day_ord, item.get("title", ""))

    unseen.sort(key=_display_sort_key)

    urgent_count = sum(1 for i in unseen if i.get("urgency") == "urgent")
    watch_count = sum(1 for i in unseen if i.get("urgency") == "watch")
    lead_count = sum(1 for i in unseen if i.get("urgency") == "lead")

    touched_sources = sorted({sid for i in unseen for sid in (i.get("matched_sources") or [])})
    touched_facts = sorted({f for i in unseen for f in (i.get("matched_facts") or [])})
    touched_routes = sorted({r for i in unseen for r in (i.get("matched_routes") or [])})

    ai_bullets: list[str] = []
    if ai_brief and unseen and gemini_key:
        try:
            ai_bullets = [
                redact_secrets(b, secrets)
                for b in generate_ai_wire_brief(
                    cfg,
                    unseen,
                    gemini_key,
                    fetcher,
                    preferred_model=preferred_gemini,
                )
            ]
        except Exception:  # noqa: BLE001
            ai_bullets = []

    health_alerts, all_channels_failed = evaluate_wire_health(statuses, channel_state_out)

    result: dict[str, Any] = {
        "desk": desk,
        "desk_label": cfg["label"],
        "route_prefix": cfg["route_prefix"],
        "source_prefix": cfg["source_prefix"],
        "since": earliest_since.isoformat(),
        "today": now_date.isoformat(),
        "total_matched": len(unique_items),
        "already_reported_count": len(already_reported),
        "new_count": len(unseen),
        "urgent_count": urgent_count,
        "watch_count": watch_count,
        "lead_count": lead_count,
        "tavily_credits_used": tavily_credits_used,
        "touched_sources": touched_sources,
        "touched_facts": touched_facts,
        "touched_routes": touched_routes,
        "ai_brief": ai_bullets,
        "dropped_candidates": dropped_candidates,
        "health_alerts": health_alerts,
        "all_channels_failed": all_channels_failed,
        "items": unseen,
        "statuses": statuses,
    }

    report_md, included_ids, omitted_count = _render_report_internal(result, max_chars=max_report_chars)
    result["omitted_count"] = omitted_count
    result["rendered_report"] = report_md

    # Persist ONLY rendered item IDs so any overflow items deferred by `max_report_chars` resurface next sweep.
    merged_reported = list(dict.fromkeys([*(state_in.get("reported_ids") or []), *included_ids]))
    state_out: dict[str, Any] = {
        "version": 1,
        "desk": desk,
        "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "channels": channel_state_out,
        "reported_ids": merged_reported[-MAX_STATE_REPORTED_IDS:],
        "legiscan_change_hashes": updated_legiscan_hashes,
        "tavily_url_hashes": dict(list(updated_tavily_hashes.items())[-500:]),
        "page_fingerprints": updated_page_fps,
        "wsr_checked_filings": updated_wsr_checked[-400:],
    }
    result["wire_state"] = state_out

    if heartbeat_url:
        hb_ok = not all_channels_failed and not health_alerts
        hb_summary = (
            f"CivicDesk Wire ({desk}): new={len(unseen)}, urgent={urgent_count}, "
            f"alerts={len(health_alerts)}, all_failed={all_channels_failed}"
        )
        result["heartbeat_status"] = redact_secrets(
            send_heartbeat(heartbeat_url, healthy=hb_ok, summary=hb_summary, fetcher=fetcher),
            secrets,
        )

    return result


def render_multi_desk_report(results: list[dict[str, Any]]) -> str:
    total_new = sum(r["new_count"] for r in results)
    total_urgent = sum(r.get("urgent_count", 0) for r in results)
    total_credits = sum(r.get("tavily_credits_used", 0) for r in results)
    header = [
        "# CivicDesk Policy Wire — Multi-Desk Edition",
        "",
        (
            f"**Desks swept:** `{len(results)}` · "
            f"**Total unseen dispatches:** **{total_new}** (`{total_urgent}` urgent) · "
            f"**Tavily credits used:** `{total_credits}`"
        ),
        "",
        "| Desk | Window | Unseen | Urgent | Watch | Journalism | Health |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for r in results:
        h_label = f"{len(r.get('health_alerts', []))} alert(s)" if r.get("health_alerts") else "ok"
        header.append(
            f"| `{r['desk']}` ({r['desk_label']}) | `{r['since']}` → `{r['today']}` | "
            f"**{r['new_count']}** | {r.get('urgent_count', 0)} | {r.get('watch_count', 0)} | "
            f"{r.get('lead_count', 0)} | {h_label} |"
        )
    header.append("")
    sections = [r.get("rendered_report") or render_markdown_report(r) for r in results]
    return "\n---\n\n".join(["\n".join(header), *sections])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desk",
        default="all",
        help="Desk slug from data/wire/*.yaml, or 'all' to sweep every enabled desk (default: all)",
    )
    parser.add_argument("--list-desks", action="store_true", help="Print JSON array of active desk slugs and exit")
    parser.add_argument("--include-disabled", action="store_true", help="Include disabled desk profiles with --list-desks")
    parser.add_argument("--wire-dir", type=Path, default=WIRE_DIR, help="Directory containing desk YAML beat profiles")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (1 to 60, default: 7)")
    parser.add_argument("--since", type=str, default=None, help="Explicit YYYY-MM-DD start date (overrides --days)")
    parser.add_argument("--seen-issues", type=Path, default=None, help="JSON from `gh issue list` for deduplication")
    parser.add_argument("--state-in", type=Path, default=None, help="Input JSON or `wire:state` issue body file")
    parser.add_argument("--state-out", type=Path, default=None, help="Write updated per-desk JSON state file")
    parser.add_argument("--state-issue-body", type=Path, default=None, help="Write formatted Markdown body for `wire:state` issue")
    parser.add_argument("--heartbeat-url", type=str, default=None, help="Optional dead-man's-switch URL (overrides WIRE_HEARTBEAT_URL)")
    parser.add_argument("--ai-brief", action="store_true", help="Generate optional free-tier Gemini executive bullets when unseen items arrive")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS, help="Polite pause between sequential requests")
    parser.add_argument("--report", type=Path, default=None, help="Write Markdown wire report to path")
    parser.add_argument("--results", type=Path, default=None, help="Write structured JSON results to path")
    args = parser.parse_args(argv)

    if args.list_desks:
        desks = list_active_desks(args.wire_dir, include_disabled=args.include_disabled)
        print(json.dumps(desks))
        return 0

    profiles = load_wire_profiles(args.wire_dir, include_disabled=True)
    active_slugs = [slug for slug, cfg in profiles.items() if cfg.get("enabled", True)]

    since_date = dt.date.fromisoformat(args.since) if args.since else None
    seen = load_seen_markers(args.seen_issues)
    use_ai_brief = args.ai_brief or os.environ.get("CIVICDESK_AI_BRIEF", "").strip() == "1"

    run_env = dict(os.environ)
    if args.heartbeat_url:
        run_env["WIRE_HEARTBEAT_URL"] = args.heartbeat_url

    if args.desk == "all":
        results = [
            run_sweep(
                slug,
                days=args.days,
                since=since_date,
                seen_markers=seen,
                fetcher=default_fetcher,
                env=run_env,
                profiles=profiles,
                pause=args.pause,
                ai_brief=use_ai_brief,
            )
            for slug in active_slugs
        ]
        md = render_multi_desk_report(results)
        all_failed = bool(results) and any(r.get("all_channels_failed") for r in results)
        payload_out: dict[str, Any] = {
            "desk": "all",
            "desks": [{k: v for k, v in r.items() if k not in {"rendered_report", "wire_state"}} for r in results],
            "new_count": sum(r["new_count"] for r in results),
            "urgent_count": sum(r.get("urgent_count", 0) for r in results),
            "tavily_credits_used": sum(r.get("tavily_credits_used", 0) for r in results),
            "health_alerts": [f"[{r['desk']}] {a}" for r in results for a in r.get("health_alerts", [])],
            "all_channels_failed": all_failed,
        }
    else:
        if args.desk not in profiles:
            parser.error(f"Unknown desk '{args.desk}'. Available desks: {', '.join(sorted(profiles))}")
        loaded_state = load_wire_state(args.state_in, desk=args.desk)
        res = run_sweep(
            args.desk,
            days=args.days,
            since=since_date,
            seen_markers=seen,
            wire_state=loaded_state,
            fetcher=default_fetcher,
            env=run_env,
            profiles=profiles,
            pause=args.pause,
            ai_brief=use_ai_brief,
        )
        md = res.get("rendered_report") or render_markdown_report(res)
        state_out = res.get("wire_state") or empty_wire_state(args.desk)
        if args.state_out:
            args.state_out.write_text(json.dumps(state_out, indent=2) + "\n", encoding="utf-8")
        if args.state_issue_body:
            args.state_issue_body.write_text(format_state_issue_body(state_out), encoding="utf-8")
        all_failed = bool(res.get("all_channels_failed"))
        payload_out = {k: v for k, v in res.items() if k not in {"rendered_report", "wire_state"}}

    if args.report:
        args.report.write_text(md, encoding="utf-8")
    else:
        print(md)
    if args.results:
        args.results.write_text(json.dumps(payload_out, indent=2) + "\n", encoding="utf-8")
    return 2 if all_failed else 0


if __name__ == "__main__":
    sys.exit(main())
