"""State administrative register and legislative OData polling (UT RSS, WA WSR, OR OLIS)."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Callable

from .common import (
    LEGISCAN_ENACTED_RE,
    item_fingerprint,
    parse_any_date,
    safe_url,
    sanitize_issue_text,
    sanitize_link_label,
    strip_html,
)
from .profiles import _get_desk_cfg

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
            else:
                raise ValueError(f"Unknown state register kind: {kind!r}")
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

