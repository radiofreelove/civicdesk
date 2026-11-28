"""Declarative wire beat profile loading (`data/wire/*.yaml`) and docket discovery."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .common import (
    COURTLISTENER_DOCKET_RE,
    PREFERRED_DESK_ORDER,
    ROOT,
    STATUTE_TOKEN_RE,
    VALID_STATE_REGISTER_KINDS,
    WIRE_DIR,
    WS_RE,
    compile_statute_regex,
)

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
            kind_norm = str(sr["kind"]).strip().lower()
            if kind_norm not in VALID_STATE_REGISTER_KINDS:
                raise ValueError(
                    f"{path.name}: unknown state_registers kind {kind_norm!r} "
                    f"(expected one of {sorted(VALID_STATE_REGISTER_KINDS)})"
                )
            sr_url = str(sr["url"]).strip()
            if not sr_url.startswith("https://") or any(ch in sr_url for ch in (" ", "\t", "\n", "\r", "'")):
                raise ValueError(f"{path.name}: state_registers URL must be percent-encoded https://, got {sr_url!r}")
            pat_str = str(sr.get("match_pattern") or "").strip()
            state_registers.append(
                {
                    "kind": kind_norm,
                    "jurisdiction": str(sr.get("jurisdiction") or "US").strip().upper(),
                    "name": str(sr.get("name") or sr_url).strip(),
                    "url": sr_url,
                    "match_re": re.compile(pat_str, re.IGNORECASE) if pat_str else None,
                }
            )

        statute_patterns = tuple(str(p).strip() for p in (loaded.get("statute_patterns") or ()) if str(p).strip())
        statute_re = compile_statute_regex(statute_patterns) if statute_patterns else STATUTE_TOKEN_RE

        profile: dict[str, Any] = {
            "desk": desk_slug,
            "enabled": enabled,
            "label": str(loaded.get("label") or desk_slug),
            "registry": _resolve_repo_path(str(loaded.get("registry") or f"data/sources/{desk_slug}.yaml"), base_root),
            "source_prefix": str(loaded.get("source_prefix") or f"{desk_slug.upper()}-"),
            "route_prefix": str(loaded.get("route_prefix") or f"/{desk_slug}/"),
            "editorial_brief": WS_RE.sub(" ", str(loaded.get("editorial_brief") or "")).strip(),
            "statute_patterns": statute_patterns,
            "statute_re": statute_re,
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


_DEFAULT_PROFILES_CACHE: dict[str, dict[str, Any]] | None = None


def _default_profiles() -> dict[str, dict[str, Any]]:
    global _DEFAULT_PROFILES_CACHE
    if _DEFAULT_PROFILES_CACHE is None:
        _DEFAULT_PROFILES_CACHE = load_wire_profiles(include_disabled=True)
    return _DEFAULT_PROFILES_CACHE


def list_active_desks(wire_dir: Path = WIRE_DIR, *, include_disabled: bool = False) -> list[str]:
    profiles = load_wire_profiles(wire_dir, include_disabled=include_disabled)
    return list(profiles.keys())


def _get_desk_cfg(desk: str, profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if profiles is not None and desk in profiles:
        return profiles[desk]
    all_profiles = _default_profiles()
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

