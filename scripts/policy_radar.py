#!/usr/bin/env python3
"""CivicDesk Policy Wire (`scripts/policy_radar.py`).

Declarative, multi-desk civic news wire and primary-source radar driven by
`data/wire/*.yaml`. Polls primary official endpoints (Federal Register API v1,
eCFR Versioner API v1, CourtListener RECAP Atom feeds + REST v4 dockets/opinions)
and triaged policy/legislative/web leads (LegiScan API, Tavily `.gov`/court search
paired with free-tier Gemini JSON editorial triage, and States Newsroom / KFF RSS),
cross-referencing every dispatch against CivicDesk's live source registries,
tracked facts (`data/facts.yaml`), and built routes (`data/pages.yaml`).

Read-only: never modifies repository data, templates, or built assets.
Always sends `User-Agent: CivicDesk-Verify/1.0`.
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
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WIRE_DIR = DATA_DIR / "wire"
TEMPLATES_DIR = ROOT / "templates"

USER_AGENT = "CivicDesk-Verify/1.0"
REQUEST_TIMEOUT = 35
PAUSE_SECONDS = 0.30
MAX_ISSUE_BODY_CHARS = 60000

RADAR_MARKER_RE = re.compile(r"<!--\s*radar-item:([a-f0-9]{12})\s*-->")
COURTLISTENER_DOCKET_RE = re.compile(r"https?://(?:www\.)?courtlistener\.com/docket/(\d+)(?:/([^/?#]+))?")
TAG_RE = re.compile(r"<[^>]+>")
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
LEGISCAN_URGENT_ACTION_RE = re.compile(
    r"\b(signed|approved by governor|governor signed|enacted|chapter\s+\d+|effective|"
    r"passed\s+(?:house|senate)|third reading.*passed|public hearing|work session)\b",
    re.IGNORECASE,
)

GEMINI_TRIAGE_MODELS = tuple(
    m.strip()
    for m in os.environ.get(
        "GEMINI_TRIAGE_MODELS",
        "gemini-2.5-flash-lite,gemini-2.5-flash",
    ).split(",")
    if m.strip()
)
GEMINI_GROUNDING_MODELS = tuple(
    m.strip()
    for m in os.environ.get(
        "GEMINI_GROUNDING_MODELS",
        "gemini-2.5-flash,gemini-2.5-flash-lite",
    ).split(",")
    if m.strip()
)

PREFERRED_DESK_ORDER = ("medicaid", "id-records", "public-records")


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
    text = html.unescape(TAG_RE.sub(" ", str(raw)))
    text = WS_RE.sub(" ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def safe_url(url: str | None) -> str:
    if not url:
        return ""
    cleaned = str(url).strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return cleaned


def normalize_url_key(url: str | None) -> str:
    cleaned = safe_url(url)
    if not cleaned:
        return ""
    parsed = urllib.parse.urlparse(cleaned)
    host = parsed.netloc.lower().removeprefix("www.")
    path = urllib.parse.unquote(parsed.path or "/").rstrip("/") or "/"
    frag = parsed.fragment.lower()
    if frag and ("entry-" in frag or "minute-entry-" in frag):
        return f"{host}{path.lower()}#{frag}"
    return f"{host}{path.lower()}"


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
        return None


def default_fetcher(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> bytes:
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
    }
    if headers:
        req_headers.update(headers)
    for attempt in range(2):
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if attempt == 0 and exc.code in (502, 503, 504):
                time.sleep(1.2)
                continue
            raise
    raise RuntimeError("Unreachable fetcher state")


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
        cl_cfg = channels.get("courtlistener") or {}
        rss_cfg = channels.get("rss") or {}
        fr_cfg = channels.get("federal_register") or {}
        leg_cfg = channels.get("legiscan") or {}
        tav_cfg = channels.get("tavily") or {}

        jurisdictions = loaded.get("jurisdictions") or {}
        tavily_domains: list[str] = []
        for jur_meta in jurisdictions.values():
            if isinstance(jur_meta, dict):
                for dom in jur_meta.get("domains") or []:
                    d = str(dom).strip().lower()
                    if d and d not in tavily_domains:
                        tavily_domains.append(d)
        for dom in tav_cfg.get("extra_domains") or []:
            d = str(dom).strip().lower()
            if d and d not in tavily_domains:
                tavily_domains.append(d)

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
            "default_dockets": {str(k): str(v) for k, v in (cl_cfg.get("watched_dockets") or {}).items()},
            "federal_register_terms": tuple(str(t) for t in (fr_cfg.get("terms") or ())),
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
            "tavily_queries": tuple(str(q) for q in (tav_cfg.get("queries") or ())),
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
) -> dict[str, Any]:
    enriched = dict(item)
    jurisdictions = detect_item_jurisdictions(enriched, cfg)
    enriched["jurisdictions"] = jurisdictions

    matched_sids: list[str] = []
    direct_match = False

    norm_u = normalize_url_key(enriched.get("url"))
    if norm_u and norm_u in impact_index["by_norm_url"]:
        direct_match = True
        for sid in impact_index["by_norm_url"][norm_u]:
            if sid not in matched_sids:
                matched_sids.append(sid)

    docket_m = COURTLISTENER_DOCKET_RE.search(str(enriched.get("url") or ""))
    if docket_m and docket_m.group(1) in impact_index["by_docket_id"]:
        direct_match = True
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
    enriched["direct_registry_match"] = direct_match

    preset_urgency = str(enriched.get("urgency") or "").lower()
    summary_clean = str(enriched.get("summary") or "").strip()
    is_supporting_docket = bool(
        docket_m
        and (
            MINISTERIAL_DOCKET_RE.search(f"{enriched.get('title', '')} {summary_clean}")
            or SUPPORTING_FILING_RE.match(summary_clean)
        )
    )
    if is_supporting_docket:
        urgency = "watch"
    elif enriched.get("tier") == "primary":
        urgency = "urgent"
    elif preset_urgency in {"urgent", "high"} or direct_match:
        urgency = "urgent"
    elif enriched.get("wire_group") == "gov_and_legislative":
        urgency = "watch"
    else:
        urgency = "lead"
    enriched["urgency"] = urgency
    return enriched


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


def poll_federal_register(
    desk: str,
    since: dt.date,
    fetcher: Callable[..., bytes],
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], str]:
    cfg = _get_desk_cfg(desk, profiles)
    terms = cfg["federal_register_terms"]
    if not terms:
        return [], "skipped (no Federal Register terms configured)"

    items: list[dict[str, Any]] = []
    seen_docs: set[str] = set()
    for idx, term in enumerate(terms):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        params = urllib.parse.urlencode(
            {
                "conditions[term]": term,
                "conditions[publication_date][gte]": since.isoformat(),
                "per_page": "20",
                "order": "newest",
            }
        )
        url = f"https://www.federalregister.gov/api/v1/documents.json?{params}"
        data = json.loads(fetcher(url).decode("utf-8", errors="replace"))
        for doc in data.get("results") or []:
            doc_num = str(doc.get("document_number") or "").strip()
            if not doc_num or doc_num in seen_docs:
                continue
            pub_parsed = parse_any_date(str(doc.get("publication_date") or ""))
            if pub_parsed and pub_parsed < since:
                continue
            html_url = safe_url(doc.get("html_url"))
            if not html_url:
                continue
            agencies = ", ".join(
                str(a.get("name") or "")
                for a in (doc.get("agencies") or [])
                if isinstance(a, dict) and a.get("name")
            )
            title_clean = strip_html(doc.get("title") or doc_num, 200)
            abstract_clean = strip_html(doc.get("abstract") or "", 260)
            if not _matches_federal_register(cfg, title_clean, abstract_clean, agencies):
                continue
            seen_docs.add(doc_num)
            doc_type = str(doc.get("type") or "").strip()
            prefix_note = f"{doc_type} ({doc_num})" if doc_type else ""
            if agencies:
                prefix_note = f"{prefix_note} — {agencies}" if prefix_note else agencies
            summary_text = (
                strip_html(f"{prefix_note}: {abstract_clean}", 250)
                if prefix_note and abstract_clean
                else (abstract_clean or prefix_note)
            )
            items.append(
                {
                    "id": item_fingerprint(desk, "fedreg", doc_num),
                    "tier": "primary",
                    "wire_group": "primary",
                    "jurisdiction": "US",
                    "source": "Federal Register",
                    "date": (pub_parsed or since).isoformat(),
                    "title": title_clean,
                    "url": html_url,
                    "summary": summary_text,
                }
            )
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
            effective = amend_date or issue_date
            if not effective or effective < since:
                continue
            ident = str(v.get("identifier") or "").strip()
            if prefixes and not any(ident.startswith(p) for p in prefixes):
                continue
            key = f"{title_num}:{part_num}:{ident}:{effective.isoformat()}"
            sec_url = (
                f"https://www.ecfr.gov/current/title-{title_num}/section-{ident}"
                if ident
                else f"https://www.ecfr.gov/current/title-{title_num}/part-{part_num}"
            )
            items[key] = {
                "id": item_fingerprint(desk, "ecfr", key),
                "tier": "primary",
                "wire_group": "primary",
                "jurisdiction": "US",
                "source": f"eCFR ({label})",
                "date": effective.isoformat(),
                "title": f"{label} section {ident or part_num} amended",
                "url": safe_url(sec_url),
                "summary": strip_html(v.get("name"), 200),
            }
    return list(items.values()), f"ok ({len(items)} match(es))"


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
            raw_dt = (
                (pub_el.text if pub_el is not None and pub_el.text else None)
                or (upd_el.text if upd_el is not None and upd_el.text else None)
            )
            entry_date = parse_any_date(raw_dt)
            if not entry_date or entry_date < since:
                continue
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
            title_clean = strip_html(title_el.text if title_el is not None else case_label, 200)
            summary_clean = strip_html(summary_el.text if summary_el is not None else "", 240)
            if MINISTERIAL_DOCKET_RE.search(f"{title_clean} {summary_clean}"):
                filtered_ministerial += 1
                continue
            items[href] = {
                "id": item_fingerprint(desk, "recap", href),
                "tier": "primary",
                "wire_group": "primary",
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
            case_name = strip_html(
                row.get("caseName") or row.get("case_name") or row.get("docketNumber") or "Federal court filing",
                180,
            )
            court = strip_html(row.get("court") or row.get("court_id") or "", 80)
            date_str = row.get("dateFiled") or row.get("date_filed") or row.get("date_created") or since.isoformat()
            parsed_dt = parse_any_date(str(date_str)) or since
            kind_badge = "Opinion" if search_type == "o" else "Docket"
            snippet = ""
            opinions = row.get("opinions") or []
            if opinions and isinstance(opinions[0], dict):
                snippet = strip_html(opinions[0].get("snippet") or "", 200)
            if not snippet:
                snippet = strip_html(row.get("snippet") or row.get("docketNumber") or qspec["label"], 220)
            items[full_url] = {
                "id": item_fingerprint(desk, f"cl-v4-{search_type}", full_url),
                "tier": "primary",
                "wire_group": "primary",
                "jurisdiction": "US",
                "source": f"CourtListener REST v4 ({kind_badge})",
                "date": parsed_dt.isoformat(),
                "title": f"{case_name} ({court})" if court else case_name,
                "url": full_url,
                "summary": snippet,
            }

    if errors and not items and len(errors) == len(queries):
        return [], f"error ({'; '.join(errors)})"
    if errors:
        return list(items.values()), f"ok ({len(items)} match(es); partial warning: {'; '.join(errors)})"
    return list(items.values()), f"ok ({len(items)} match(es) across {len(queries)} v4 search(es))"


def poll_legiscan(
    desk: str,
    since: dt.date,
    api_key: str,
    fetcher: Callable[..., bytes],
    *,
    today: dt.date | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], str]:
    key_clean = api_key.strip()
    if not key_clean:
        return [], "skipped (LEGISCAN_API_KEY not set)"
    cfg = _get_desk_cfg(desk, profiles)
    queries = cfg["legiscan_queries"]
    if not queries:
        return [], "skipped (no LegiScan queries configured)"

    anchor = max(today or dt.date.today(), since)
    until = anchor + dt.timedelta(days=90)
    action_window = f"action:{since:%Y%m%d}..{until:%Y%m%d}"
    min_rel = cfg["legiscan_min_relevance"]
    year_param = cfg["legiscan_year"]

    items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for idx, (state_code, query) in enumerate(queries):
        if idx > 0 and pause > 0:
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
            bill_url = safe_url(row.get("url") or row.get("text_url") or row.get("research_url"))
            bill_num = str(row.get("bill_number") or "").strip()
            bill_id = str(row.get("bill_id") or bill_url or bill_num)
            if not bill_url or not bill_id:
                continue
            last_action = strip_html(str(row.get("last_action") or ""), 200)
            rel_note = f", {relevance}% match" if "relevance" in row else ""
            is_urgent_bill = bool(LEGISCAN_URGENT_ACTION_RE.search(last_action))
            fp_key = f"{state_code}:{bill_id}:{action_date.isoformat()}"
            items[bill_id] = {
                "id": item_fingerprint(desk, "legiscan", fp_key),
                "tier": "lead",
                "wire_group": "gov_and_legislative",
                "urgency": "urgent" if is_urgent_bill else "watch",
                "jurisdiction": state_code,
                "source": f"LegiScan ({state_code} — verify in official legislature)",
                "date": action_date.isoformat(),
                "title": strip_html(f"{state_code} {bill_num}: {row.get('title') or ''}", 190),
                "url": bill_url,
                "summary": strip_html(f"Last action ({action_date.isoformat()}{rel_note}): {last_action}", 240),
            }

    if errors and not items and len(errors) == len(queries):
        return [], f"error ({'; '.join(errors)})"
    states_label = ", ".join(s for s, _ in queries)
    if errors:
        return list(items.values()), f"ok ({len(items)} bill action(s); partial warning: {'; '.join(errors)})"
    return list(items.values()), f"ok ({len(items)} bill action(s) across {states_label})"


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
                        strip_html(el.findtext("title"), 200),
                        strip_html(el.findtext("description"), 240),
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
                        strip_html(el.findtext("a:title", "", atom_ns), 200),
                        strip_html(el.findtext("a:summary", "", atom_ns), 240),
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


def discover_gemini_flash_models(api_key: str, fetcher: Callable[..., bytes]) -> list[str]:
    raw = fetcher(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": api_key.strip()},
    )
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    excluded = ("tts", "live", "image", "omni", "transcribe", "embedding", "aqa", "2.0-flash-lite")
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
    lite_models.sort(reverse=True)
    flash_models.sort(reverse=True)
    return lite_models + flash_models


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
    queries = cfg["tavily_queries"]
    domains = list(cfg["tavily_domains"])
    key_clean = tavily_key.strip()
    now_date = today or dt.date.today()

    by_url: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    credits_used = 0

    for idx, query in enumerate(queries):
        if idx > 0 and pause > 0:
            time.sleep(pause)
        payload_dict: dict[str, Any] = {
            "query": query,
            "search_depth": cfg["tavily_search_depth"],
            "topic": "general",
            "max_results": cfg["tavily_max_results"],
            "start_date": since.isoformat(),
            "include_published_date": True,
            "filter_by_published_date": False,
        }
        if domains:
            payload_dict["include_domains"] = domains
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
            errors.append(f"q{idx + 1}: {type(exc).__name__}: {redact_secrets(str(exc), (key_clean,))}")
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
            title = strip_html(row.get("title") or url, 190)
            snippet = strip_html(row.get("content") or "", 360)
            existing = by_url.get(url)
            if existing is None or score > existing["score"]:
                by_url[url] = {
                    "title": title,
                    "url": url,
                    "date": (pub_date or now_date).isoformat(),
                    "published_date": pub_raw,
                    "score": score,
                    "snippet": snippet,
                }

    ranked = sorted(by_url.values(), key=lambda c: c["score"], reverse=True)
    return ranked, credits_used, errors


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
) -> tuple[list[dict[str, Any]], str]:
    cfg = _get_desk_cfg(desk, profiles)
    now_date = today or dt.date.today()
    batch = candidates[:15]
    compact = [
        {
            "index": idx,
            "title": c["title"],
            "url": c["url"],
            "published_date": c["published_date"],
            "snippet": c["snippet"],
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
        f"updates when published_date is blank). Drop generic evergreen landing pages, agency homepages, directory "
        f"listings, or older archived documents with no recent change.\n\n"
        f"Respond ONLY with valid JSON matching:\n"
        f'{{"kept": [{{"index": 0, "urgency": "urgent|watch", "jurisdiction": "US", '
        f'"summary": "One literal plain-language sentence stating what changed or was announced.", '
        f'"check_hint": "Short note on what to verify on CivicDesk."}}]}}\n'
        f'Use {{"kept": []}} if none qualify. Set urgency="urgent" only for enacted rules, fee/form revisions, '
        f"effective dates, or court orders; otherwise set urgency=\"watch\".\n\n"
        f"Candidates:\n{json.dumps(compact, ensure_ascii=False)}"
    )
    req_body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
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
    kept_rows = parsed.get("kept") if isinstance(parsed, dict) else []
    if not isinstance(kept_rows, list):
        kept_rows = []

    items: dict[str, dict[str, Any]] = {}
    allowed_jurs = set((cfg.get("jurisdictions") or {}).keys())
    for row in kept_rows:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(batch):
            continue
        src = batch[idx]
        summary = strip_html(str(row.get("summary") or ""), 220) or src["snippet"]
        hint = strip_html(str(row.get("check_hint") or ""), 140)
        if hint:
            summary = strip_html(f"{summary} (Verify: {hint})", 300)
        urg = "urgent" if str(row.get("urgency") or "").lower() in {"urgent", "high"} else "watch"
        jur = str(row.get("jurisdiction") or "").upper()
        item_entry: dict[str, Any] = {
            "id": item_fingerprint(desk, "tavily", src["url"]),
            "tier": "lead",
            "wire_group": "gov_and_legislative",
            "urgency": urg,
            "source": "Tavily + Gemini (.gov / court wire)",
            "date": src["date"],
            "title": src["title"],
            "url": src["url"],
            "summary": summary or "Official .gov/court web match; open primary page to verify date and text.",
        }
        if jur in allowed_jurs:
            item_entry["jurisdiction"] = jur
        items[src["url"]] = item_entry
    return list(items.values()), used_model


def _fallback_tavily_items(
    desk: str,
    cfg: dict[str, Any],
    candidates: list[dict[str, Any]],
    source_label: str,
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for c in candidates:
        threshold = cfg["tavily_min_score_dated"] if c["published_date"] else cfg["tavily_min_score_undated"]
        if c["score"] < threshold:
            continue
        kept.append(
            {
                "id": item_fingerprint(desk, "tavily", c["url"]),
                "tier": "lead",
                "wire_group": "gov_and_legislative",
                "urgency": "watch",
                "source": source_label,
                "date": c["date"],
                "title": c["title"],
                "url": c["url"],
                "summary": c["snippet"] or "Official .gov/court web match; open primary page to verify date and text.",
            }
        )
    return kept[:10]


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
    note_text = strip_html(_extract_gemini_text(payload), 220)
    items: dict[str, dict[str, Any]] = {}
    for cand in payload.get("candidates") or []:
        meta = cand.get("groundingMetadata") or {}
        for chunk in meta.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            uri = safe_url(web.get("uri"))
            if not uri:
                continue
            title = strip_html(web.get("title") or uri, 180)
            items[uri] = {
                "id": item_fingerprint(desk, "gemini-grounding", uri),
                "tier": "lead",
                "wire_group": "gov_and_legislative",
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
) -> tuple[list[dict[str, Any]], int, str]:
    cfg = _get_desk_cfg(desk, profiles)
    if not cfg["tavily_queries"]:
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

    candidates, credits_used, errors = poll_tavily_search(
        desk,
        since,
        tavily_key,
        fetcher,
        today=today,
        profiles=profiles,
        pause=pause,
    )
    if errors and not candidates and len(errors) == len(cfg["tavily_queries"]):
        return [], credits_used, f"error ({'; '.join(errors)})"

    warn_suffix = f"; partial warning: {'; '.join(errors)}" if errors else ""
    if not candidates:
        return (
            [],
            credits_used,
            f"ok (0 .gov/court match(es) across {len(cfg['tavily_queries'])} Tavily queries, {credits_used} credit(s){warn_suffix})",
        )

    if gemini_key.strip():
        try:
            kept, used_model = triage_tavily_with_gemini(
                desk,
                since,
                candidates,
                gemini_key,
                fetcher,
                today=today,
                preferred_model=preferred_model,
                profiles=profiles,
            )
            return (
                kept,
                credits_used,
                f"ok ({len(kept)} .gov/court lead(s) from {len(candidates)} Tavily candidate(s), {credits_used} credit(s), triaged via {used_model}{warn_suffix})",
            )
        except Exception as exc:  # noqa: BLE001
            fallback = _fallback_tavily_items(desk, cfg, candidates, "Tavily Search (.gov / court wire)")
            err_note = redact_secrets(f"{type(exc).__name__}: {exc}", (gemini_key, tavily_key))
            return (
                fallback,
                credits_used,
                f"ok ({len(fallback)} .gov/court lead(s) from {len(candidates)} Tavily candidate(s), {credits_used} credit(s); Gemini triage fallback: {err_note}{warn_suffix})",
            )

    fallback = _fallback_tavily_items(desk, cfg, candidates, "Tavily Search (.gov / court wire)")
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
        f'Respond ONLY with JSON: {{"bullets": ["First short bullet.", "Second short bullet."]}}\n\n'
        f"Dispatches:\n{json.dumps(compact, ensure_ascii=False)}"
    )
    req_body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
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
    return [strip_html(str(b), 220) for b in bullets[:4] if str(b).strip()]


def run_sweep(
    desk: str,
    *,
    days: int = 7,
    since: dt.date | None = None,
    today: dt.date | None = None,
    seen_markers: set[str] | None = None,
    fetcher: Callable[..., bytes] = default_fetcher,
    env: dict[str, str] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    pause: float = 0.0,
    ai_brief: bool = False,
) -> dict[str, Any]:
    cfg = _get_desk_cfg(desk, profiles)
    now_date = today or dt.datetime.now(dt.timezone.utc).date()
    since_date = since or (now_date - dt.timedelta(days=max(1, min(days, 60))))
    seen = seen_markers or set()
    cfg_env = env if env is not None else dict(os.environ)

    legiscan_key = (cfg_env.get("LEGISCAN_API_KEY") or "").strip()
    cl_token = (cfg_env.get("COURTLISTENER_TOKEN") or "").strip()
    tavily_key = (cfg_env.get("TAVILY_API_KEY") or "").strip()
    gemini_key = (cfg_env.get("GEMINI_API_KEY") or "").strip()
    preferred_gemini = (cfg_env.get("GEMINI_MODEL") or "").strip()
    secrets = (legiscan_key, cl_token, tavily_key, gemini_key)

    impact_index = build_desk_impact_index(cfg)
    statuses: list[dict[str, str]] = []
    collected: list[dict[str, Any]] = []
    tavily_credits_used = 0

    def _run_channel(name: str, fn: Callable[[], tuple[list[dict[str, Any]], str]]) -> None:
        try:
            found, status = fn()
            collected.extend(found)
            statuses.append({"source": name, "status": redact_secrets(status, secrets)})
        except Exception as exc:  # noqa: BLE001
            err_msg = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
            statuses.append({"source": name, "status": f"error: {err_msg}"})

    _run_channel(
        "Federal Register API",
        lambda: poll_federal_register(desk, since_date, fetcher, profiles=profiles, pause=pause),
    )
    _run_channel(
        "eCFR Versioner API",
        lambda: poll_ecfr(desk, since_date, fetcher, profiles=profiles, pause=pause),
    )
    _run_channel(
        "CourtListener RECAP Feeds",
        lambda: poll_courtlistener_feeds(desk, since_date, fetcher, profiles=profiles, pause=pause),
    )
    _run_channel(
        "CourtListener REST v4",
        lambda: poll_courtlistener_v4(desk, since_date, cl_token, fetcher, profiles=profiles, pause=pause),
    )
    _run_channel(
        "LegiScan API",
        lambda: poll_legiscan(
            desk,
            since_date,
            legiscan_key,
            fetcher,
            today=now_date,
            profiles=profiles,
            pause=pause,
        ),
    )

    try:
        tav_items, credits, tav_status = poll_tavily_and_gemini(
            desk,
            since_date,
            tavily_key,
            gemini_key,
            fetcher,
            today=now_date,
            preferred_model=preferred_gemini,
            profiles=profiles,
            pause=pause,
        )
        tavily_credits_used += credits
        collected.extend(tav_items)
        statuses.append({"source": "Tavily + Gemini Web Wire", "status": redact_secrets(tav_status, secrets)})
    except Exception as exc:  # noqa: BLE001
        err_msg = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
        statuses.append({"source": "Tavily + Gemini Web Wire", "status": f"error: {err_msg}"})

    _run_channel(
        "Policy & State News RSS",
        lambda: poll_rss(desk, since_date, fetcher, profiles=profiles, pause=pause),
    )

    # Deduplicate within this sweep by fingerprint AND by canonical URL (preferring primary tier over leads).
    urgency_rank = {"urgent": 0, "watch": 1, "lead": 2}
    tier_rank = {"primary": 0, "lead": 1}

    annotated = [annotate_item_impact(raw_item, cfg, impact_index) for raw_item in collected]
    annotated.sort(
        key=lambda x: (
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

    # Sort unseen items: primary urgent newest first, then gov/legislative newest first, then journalism newest first.
    def _display_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
        grp_order = {"primary": 0, "gov_and_legislative": 1, "journalism": 2}.get(
            str(item.get("wire_group") or ""),
            0 if item["tier"] == "primary" else 2,
        )
        urg_order = urgency_rank.get(str(item.get("urgency") or "lead"), 2)
        day_ord = -(parse_any_date(item.get("date")) or since_date).toordinal()
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

    return {
        "desk": desk,
        "desk_label": cfg["label"],
        "route_prefix": cfg["route_prefix"],
        "source_prefix": cfg["source_prefix"],
        "since": since_date.isoformat(),
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
        "items": unseen,
        "statuses": statuses,
    }


def _format_wire_bullet(item: dict[str, Any]) -> list[str]:
    urg_label = {"urgent": "URGENT", "watch": "WATCH", "lead": "LEAD"}.get(
        str(item.get("urgency") or "lead"),
        "LEAD",
    )
    jurs = "/".join(item.get("jurisdictions") or ["US"])
    lines = [
        f"- **[{urg_label} · {jurs}]** **{item['date']}** · [{item['title']}]({item['url']}) — *{item['source']}* <!-- radar-item:{item['id']} -->"
    ]
    if item.get("summary"):
        lines.append(f"  - {item['summary']}")
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


def render_markdown_report(result: dict[str, Any]) -> str:
    desk = result["desk"]
    lines = [
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

    if result.get("touched_sources") or result.get("touched_routes"):
        parts: list[str] = []
        if result.get("touched_sources"):
            parts.append("Registry IDs: " + ", ".join(f"`{s}`" for s in result["touched_sources"][:8]))
        if result.get("touched_facts"):
            parts.append("Tracked facts: " + ", ".join(f"`{f}`" for f in result["touched_facts"][:6]))
        if result.get("touched_routes"):
            parts.append("Guide routes: " + ", ".join(f"`{r}`" for r in result["touched_routes"][:8]))
        lines.append("**Potentially affected on CivicDesk:** " + " · ".join(parts))
        lines.append("")

    if result.get("ai_brief"):
        lines.append("### Editorial triage summary *(machine-generated — verify primary sources)*")
        lines.append("")
        for bullet in result["ai_brief"]:
            lines.append(f"- {bullet}")
        lines.append("")

    items: list[dict[str, Any]] = result["items"]
    primary = [i for i in items if i["tier"] == "primary"]
    gov_leg = [i for i in items if i["tier"] != "primary" and i.get("wire_group") == "gov_and_legislative"]
    journalism = [
        i
        for i in items
        if i["tier"] != "primary" and i.get("wire_group") != "gov_and_legislative"
    ]

    if primary:
        lines.append("### 1. Primary official dispatches (`official` — Federal Register, eCFR, CourtListener)")
        lines.append("")
        for item in primary:
            lines.extend(_format_wire_bullet(item))
        lines.append("")

    if gov_leg:
        lines.append("### 2. Official-domain & legislative wire (`not_official` leads — Tavily + Gemini, LegiScan)")
        lines.append("")
        for item in gov_leg:
            lines.extend(_format_wire_bullet(item))
        lines.append("")

    if journalism:
        lines.append("### 3. Policy & state journalism wire (`not_official` context — verify in `.gov` before citing)")
        lines.append("")
        for item in journalism:
            lines.extend(_format_wire_bullet(item))
        lines.append("")

    if not items:
        lines.append("No new unseen dispatches matched during this lookback window.")
        lines.append("")

    if result.get("already_reported_count"):
        lines.append(
            f"*{result['already_reported_count']} matching item(s) from this window were already reported in an earlier `radar:{desk}` issue and skipped.*"
        )
        lines.append("")

    lines.extend(
        [
            "### Wire channel telemetry",
            "",
            "| Channel | Status |",
            "|---|---|",
        ]
    )
    for st in result["statuses"]:
        src = st["source"].replace("|", "/")
        val = st["status"].replace("|", "/").replace("\n", " ")
        lines.append(f"| `{src}` | {val} |")
    lines.append("")

    lines.extend(
        [
            "### Next step (Antigravity verification workflow)",
            "",
            "```text",
            f"Triage open radar:{desk} issues in radiofreelove/civicdesk against primary .gov and court sources per AGENTS.md and docs/RADAR-RUNBOOK.md, run build.py, check_brand_purge.py, and pytest, and open a Draft PR.",
            "```",
            "",
        ]
    )

    body = "\n".join(lines)
    if len(body) > MAX_ISSUE_BODY_CHARS:
        body = (
            body[: MAX_ISSUE_BODY_CHARS - 90].rsplit("\n", 1)[0]
            + "\n\n*(Truncated to fit GitHub issue length limit.)*\n"
        )
    return body


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
        "| Desk | Window | Unseen | Urgent | Watch | Journalism |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in results:
        header.append(
            f"| `{r['desk']}` ({r['desk_label']}) | `{r['since']}` → `{r['today']}` | "
            f"**{r['new_count']}** | {r.get('urgent_count', 0)} | {r.get('watch_count', 0)} | {r.get('lead_count', 0)} |"
        )
    header.append("")
    sections = [render_markdown_report(r) for r in results]
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

    if args.desk == "all":
        results = [
            run_sweep(
                slug,
                days=args.days,
                since=since_date,
                seen_markers=seen,
                profiles=profiles,
                pause=args.pause,
                ai_brief=use_ai_brief,
            )
            for slug in active_slugs
        ]
        md = render_multi_desk_report(results)
        payload_out: dict[str, Any] = {
            "desk": "all",
            "desks": results,
            "new_count": sum(r["new_count"] for r in results),
            "urgent_count": sum(r.get("urgent_count", 0) for r in results),
            "tavily_credits_used": sum(r.get("tavily_credits_used", 0) for r in results),
        }
    else:
        if args.desk not in profiles:
            parser.error(f"Unknown desk '{args.desk}'. Available desks: {', '.join(sorted(profiles))}")
        res = run_sweep(
            args.desk,
            days=args.days,
            since=since_date,
            seen_markers=seen,
            profiles=profiles,
            pause=args.pause,
            ai_brief=use_ai_brief,
        )
        md = render_markdown_report(res)
        payload_out = res

    if args.report:
        args.report.write_text(md, encoding="utf-8")
    else:
        print(md)
    if args.results:
        args.results.write_text(json.dumps(payload_out, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
