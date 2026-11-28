"""Wire health evaluation, sweep orchestration, Markdown report rendering, and CLI runner."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .channels import (
    poll_courtlistener_feeds,
    poll_courtlistener_v4,
    poll_ecfr,
    poll_federal_register,
    poll_legiscan,
    poll_page_watch,
    poll_rss,
)
from .common import (
    DEFAULT_EMPTY_STREAK_THRESHOLD,
    MAX_ISSUE_BODY_CHARS,
    MAX_LOOKBACK_DAYS,
    MAX_STATE_REPORTED_IDS,
    OVERLAP_DAYS,
    PAUSE_SECONDS,
    URGENT_EFFECTIVE_DAYS,
    WIRE_DIR,
    default_fetcher,
    empty_wire_state,
    format_state_issue_body,
    load_seen_markers,
    load_wire_state,
    normalize_url_key,
    parse_any_date,
    redact_secrets,
    safe_url,
    sanitize_issue_text,
    sanitize_link_label,
)
from .impact import annotate_item_impact, build_desk_impact_index
from .profiles import _get_desk_cfg, list_active_desks, load_wire_profiles
from .registers import poll_state_registers
from .triage import generate_ai_wire_brief, poll_tavily_and_gemini

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


def main(
    argv: list[str] | None = None,
    *,
    fetcher: Callable[..., bytes] | None = None,
) -> int:
    active_fetcher = fetcher or default_fetcher
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
                fetcher=active_fetcher,
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
            fetcher=active_fetcher,
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

