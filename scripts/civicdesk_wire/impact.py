"""Cross-referencing wire dispatches against CivicDesk registries, facts, and built routes."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import yaml

from .common import (
    COURTLISTENER_DOCKET_RE,
    COURT_ORDER_OPINION_RE,
    DATA_DIR,
    FACT_CALL_RE,
    NON_ORDER_FILING_RE,
    STATUTE_TOKEN_RE,
    TEMPLATES_DIR,
    URGENT_EFFECTIVE_DAYS,
    WS_RE,
    extract_host,
    host_matches_domain,
    normalize_url_key,
    parse_any_date,
    sanitize_issue_text,
    sanitize_link_label,
    strip_html,
)


def _extract_statute_tokens(text: str, pattern: re.Pattern[str] | None = None) -> set[str]:
    rx = pattern or STATUTE_TOKEN_RE
    return {WS_RE.sub(" ", m.group(0).lower()).strip() for m in rx.finditer(text or "")}


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
    statute_re = cfg.get("statute_re") or STATUTE_TOKEN_RE

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
            for tok in _extract_statute_tokens(src_text, statute_re):
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
                note_tokens = _extract_statute_tokens(str(fmeta.get("note") or ""), statute_re)
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
            if not route or bool(page.get("source_registry")) or page.get("slug") in {"sources", "404"}:
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

    statute_re = cfg.get("statute_re") or STATUTE_TOKEN_RE
    text_blob = f"{enriched.get('title', '')} {enriched.get('summary', '')} {enriched.get('url', '')}"
    for tok in _extract_statute_tokens(text_blob, statute_re):
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


