"""Tavily web discovery and Gemini Flash structured triage / executive wire briefs."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
from typing import Any, Callable

from .common import (
    GEMINI_BRIEF_SCHEMA,
    GEMINI_GROUNDING_MODELS,
    GEMINI_TRIAGE_MODELS,
    GEMINI_TRIAGE_SCHEMA,
    _content_snippet_hash,
    extract_host,
    host_matches_domain,
    item_fingerprint,
    normalize_url_key,
    parse_any_date,
    redact_secrets,
    safe_url,
    sanitize_issue_text,
    sanitize_link_label,
)
from .impact import build_desk_impact_index
from .profiles import _get_desk_cfg

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


