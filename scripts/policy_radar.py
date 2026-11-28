#!/usr/bin/env python3
"""CivicDesk Policy Wire — thin CLI and compatibility facade over `scripts/civicdesk_wire/`.

Monitors authoritative primary government/court feeds and official `.gov` / court
domain web dispatches for every enabled desk profile in `data/wire/*.yaml`.

Strictly read-only: never modifies repository files; writes Markdown and JSON
reports for human/Antigravity review.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from civicdesk_wire import (  # noqa: E402, F401
    BASE_STATUTE_PATTERNS,
    COURTLISTENER_DOCKET_RE,
    DATA_DIR,
    DEFAULT_EMPTY_STREAK_THRESHOLD,
    FEDERAL_REGISTER_FIELDS,
    GEMINI_GROUNDING_MODELS,
    GEMINI_TRIAGE_MODELS,
    MAX_ISSUE_BODY_CHARS,
    MAX_LOOKBACK_DAYS,
    MAX_STATE_REPORTED_IDS,
    MINISTERIAL_DOCKET_RE,
    OVERLAP_DAYS,
    PAUSE_SECONDS,
    PREFERRED_DESK_ORDER,
    RADAR_MARKER_RE,
    REQUEST_TIMEOUT,
    ROOT,
    STATUTE_TOKEN_RE,
    SUPPORTING_FILING_RE,
    TEMPLATES_DIR,
    URGENT_EFFECTIVE_DAYS,
    USER_AGENT,
    VALID_STATE_REGISTER_KINDS,
    WIRE_DIR,
    WIRE_STATE_JSON_RE,
    _WA_WSR_ROW_RE,
    annotate_item_impact,
    build_desk_impact_index,
    compile_statute_regex,
    default_fetcher,
    detect_item_jurisdictions,
    discover_dockets,
    discover_gemini_flash_models,
    empty_wire_state,
    evaluate_wire_health,
    extract_host,
    format_state_issue_body,
    generate_ai_wire_brief,
    host_matches_domain,
    item_fingerprint,
    list_active_desks,
    load_seen_markers,
    load_wire_profiles,
    load_wire_state,
    main as _report_main,
    normalize_url_key,
    parse_any_date,
    poll_courtlistener_feeds,
    poll_courtlistener_v4,
    poll_ecfr,
    poll_federal_register,
    poll_gemini_grounding,
    poll_legiscan,
    poll_page_watch,
    poll_rss,
    poll_state_registers,
    poll_tavily_and_gemini,
    poll_tavily_search,
    redact_secrets,
    render_markdown_report,
    render_multi_desk_report,
    run_sweep,
    send_heartbeat,
    strip_html,
    sanitize_issue_text,
    sanitize_link_label,
    safe_url,
    triage_tavily_with_gemini,
)


def main(argv: list[str] | None = None) -> int:
    """Invoke the Policy Wire CLI, respecting monkeypatched `policy_radar.default_fetcher`."""
    return _report_main(argv, fetcher=default_fetcher)


if __name__ == "__main__":
    sys.exit(main())
