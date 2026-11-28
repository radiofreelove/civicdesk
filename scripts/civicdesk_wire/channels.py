"""Keyless and keyed wire polling channels: Page Watch, Federal Register, eCFR, CourtListener, LegiScan, RSS."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import yaml

from .common import (
    COURTLISTENER_DOCKET_RE,
    FEDERAL_REGISTER_FIELDS,
    LEGISCAN_ENACTED_RE,
    _fetch_with_meta,
    compute_page_fingerprint,
    item_fingerprint,
    parse_any_date,
    redact_secrets,
    safe_url,
    sanitize_issue_text,
    sanitize_link_label,
)
from .impact import MINISTERIAL_DOCKET_RE, SUPPORTING_FILING_RE, _is_court_order_or_opinion
from .profiles import _get_desk_cfg, discover_dockets

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

