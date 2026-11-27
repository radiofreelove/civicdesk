#!/usr/bin/env python3
"""Weekly source link, fingerprint, `kind`-based freshness, and `review_by` check.

Always sends `User-Agent: CivicDesk-Verify/1.0` per docs/RADAR-RUNBOOK.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable

import yaml

try:
    from http_retry import (
        TRANSIENT_HTTP_CODES,
        USER_AGENT,
        compute_page_fingerprint,
        fetch_with_retry,
        parse_retry_after,
    )
except ImportError:  # pragma: no cover - package-qualified import fallback
    from scripts.http_retry import (  # type: ignore[no-redef]
        TRANSIENT_HTTP_CODES,
        USER_AGENT,
        compute_page_fingerprint,
        fetch_with_retry,
        parse_retry_after,
    )

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "data" / "sources"
FACTS_PATH = ROOT / "data" / "facts.yaml"

DEFAULT_MAX_AGE_DAYS = 120
MAX_AGE_BY_KIND: dict[str, int] = {
    # Short window: forms, fee schedules, and active litigation dockets
    "agency-form": 60,
    "court-form": 60,
    "state-form": 60,
    "fee-schedule": 60,
    "court": 60,
    # Medium window: live agency and court web pages / letters
    "agency-page": 90,
    "court-page": 90,
    "state-page": 90,
    "federal-page": 90,
    "federal-letter": 90,
    # Standard window: explainers, guides, and external reference pages
    "state-explainer": 120,
    "guide": 120,
    "legal-aid": 120,
    "advocacy": 120,
    "helper": 120,
    "legislature": 120,
    "platform": 120,
    "secondary": 120,
    # Longer window: codified statutes, session laws, rules, standards, and fixed datasets
    "statute": 180,
    "session-law": 180,
    "state-rule": 180,
    "federal-rule": 180,
    "executive-order": 180,
    "standard": 180,
    "accessibility-standard": 180,
    "data": 180,
}


def iter_source_files(sources_dir: Path | None = None) -> list[Path]:
    base = sources_dir or SOURCES_DIR
    return sorted(base.glob("*.yaml"))


def max_age_for_source(source: dict[str, Any], override_days: int | None = None) -> int:
    if override_days is not None:
        return int(override_days)
    if source.get("max_age_days") is not None:
        return int(source["max_age_days"])
    kind = str(source.get("kind") or "").strip().lower()
    return MAX_AGE_BY_KIND.get(kind, DEFAULT_MAX_AGE_DAYS)


def _default_fetch_response(url: str, timeout: int) -> tuple[int, dict[str, str], bytes]:
    return fetch_with_retry(url, timeout=timeout, max_retries=0)


def _probe_url(
    url: str,
    *,
    timeout: int,
    max_retries: int,
    fetcher: Callable[[str, int], int | tuple[int, dict[str, str], bytes]],
    sleeper: Callable[[float], None],
) -> tuple[str, int | str, dict[str, str]]:
    attempt = 0
    while True:
        try:
            res = fetcher(url, timeout)
            if isinstance(res, tuple):
                status, hdrs, body = res
            else:
                status, hdrs, body = int(res), {}, b""
            if status in TRANSIENT_HTTP_CODES and attempt < max_retries:
                retry_delay = parse_retry_after(hdrs.get("retry-after"))
                wait_s = retry_delay if retry_delay is not None else (0.5 * (2**attempt))
                attempt += 1
                sleeper(wait_s)
                continue
            if status >= 400:
                return ("http_error", status, {})
            fp_meta = compute_page_fingerprint(body, hdrs, url=url) if body else {}
            return ("ok", status, fp_meta)
        except urllib.error.HTTPError as exc:
            exc_hdrs = {str(k).lower(): str(v).strip() for k, v in (exc.headers.items() if exc.headers else ())}
            if exc.code in TRANSIENT_HTTP_CODES and attempt < max_retries:
                retry_delay = parse_retry_after(exc_hdrs.get("retry-after"))
                wait_s = retry_delay if retry_delay is not None else (0.5 * (2**attempt))
                attempt += 1
                sleeper(wait_s)
                continue
            return ("http_error", int(exc.code), {})
        except Exception as exc:  # noqa: BLE001
            if attempt < max_retries:
                attempt += 1
                sleeper(0.5 * (2 ** (attempt - 1)))
                continue
            return ("exc", str(exc), {})


def check_freshness(
    max_age_days: int | None = None,
    *,
    sources_dir: Path | None = None,
    today: dt.date | None = None,
) -> list[str]:
    now_date = today or dt.date.today()
    stale: list[str] = []
    for yaml_path in iter_source_files(sources_dir):
        entries = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or []
        for s in entries:
            checked = dt.date.fromisoformat(str(s["checked"]))
            age = (now_date - checked).days
            limit = max_age_for_source(s, override_days=max_age_days)
            if age > limit:
                kind_label = str(s.get("kind") or "default")
                stale.append(
                    f"{s['id']}: last checked {age} days ago ({checked.isoformat()}, kind={kind_label}, limit {limit})"
                )
    return stale


def check_review_by_dates(
    *,
    sources_dir: Path | None = None,
    facts_path: Path | None = None,
    today: dt.date | None = None,
) -> list[str]:
    """Flag dated facts or sources whose `review_by` date has arrived or passed."""
    now_date = today or dt.date.today()
    expired: list[str] = []

    fpath = facts_path if facts_path is not None else FACTS_PATH
    if fpath.is_file():
        facts = yaml.safe_load(fpath.read_text(encoding="utf-8")) or {}
        if isinstance(facts, dict):
            for fid, fmeta in facts.items():
                if not isinstance(fmeta, dict) or not fmeta.get("review_by"):
                    continue
                rev_date = dt.date.fromisoformat(str(fmeta["review_by"]))
                if now_date >= rev_date:
                    overdue = (now_date - rev_date).days
                    val_preview = str(fmeta.get("value") or "")[:60]
                    expired.append(
                        f"fact {fid}: review_by {rev_date.isoformat()} reached ({overdue} day(s) past review_by; value={val_preview!r})"
                    )

    for yaml_path in iter_source_files(sources_dir):
        entries = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or []
        for s in entries:
            if not isinstance(s, dict) or not s.get("review_by"):
                continue
            rev_date = dt.date.fromisoformat(str(s["review_by"]))
            if now_date >= rev_date:
                overdue = (now_date - rev_date).days
                expired.append(
                    f"source {s['id']}: review_by {rev_date.isoformat()} reached ({overdue} day(s) past review_by)"
                )

    return expired


def check_links(
    timeout: int = 20,
    *,
    sources_dir: Path | None = None,
    max_retries: int = 2,
    pause_seconds: float = 0.25,
    fetcher: Callable[[str, int], int | tuple[int, dict[str, str], bytes]] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[str]:
    issues: list[str] = []
    outcomes: dict[str, tuple[str, int | str, dict[str, str]]] = {}
    fetch_fn = fetcher or _default_fetch_response

    for yaml_path in iter_source_files(sources_dir):
        entries = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or []
        for s in entries:
            if s.get("link_check") == "by hand":
                continue
            url = str(s["url"])
            if url not in outcomes:
                outcomes[url] = _probe_url(
                    url,
                    timeout=timeout,
                    max_retries=max_retries,
                    fetcher=fetch_fn,
                    sleeper=sleeper,
                )
                if pause_seconds > 0:
                    sleeper(pause_seconds)

            kind, detail, fp_meta = outcomes[url]
            if kind == "ok":
                expected_fp = str(s.get("fingerprint") or "").strip()
                live_fp = str(fp_meta.get("fingerprint") or "").strip()
                if expected_fp and live_fp and expected_fp != live_fp:
                    issues.append(
                        f"{s['id']} ({url}): content fingerprint changed since checked {s.get('checked')} "
                        f"(recorded {expected_fp}, live {live_fp})"
                    )
                continue
            if kind == "http_error" and detail == 403 and s.get("bot_gated") is True:
                print(f"note (bot-gated HTTP 403, check in browser): {s['id']} {url}")
                continue
            if kind == "http_error":
                issues.append(f"{s['id']} ({url}): HTTP {detail}")
            else:
                issues.append(f"{s['id']} ({url}): {detail}")
    return issues


def _upsert_yaml_fingerprint_lines(yaml_text: str, fingerprints_by_id: dict[str, str]) -> tuple[str, int]:
    """Insert or update `fingerprint:` immediately beneath `checked:` for matching source IDs."""
    if not fingerprints_by_id:
        return yaml_text, 0
    lines = yaml_text.splitlines(keepends=True)
    out: list[str] = []
    current_id: str | None = None
    updated = 0
    id_re = re.compile(r"^-\s+id:\s*(\S+)\s*$")
    checked_re = re.compile(r"^(\s+)checked:\s*.*$")
    fp_re = re.compile(r"^(\s+)fingerprint:\s*.*$")

    i = 0
    while i < len(lines):
        line = lines[i]
        m_id = id_re.match(line.rstrip("\r\n"))
        if m_id:
            current_id = m_id.group(1).strip("'\"")
            out.append(line)
            i += 1
            continue

        if current_id and current_id in fingerprints_by_id:
            if fp_re.match(line.rstrip("\r\n")):
                # Skip existing fingerprint line; we rewrite it cleanly right under `checked:`.
                i += 1
                continue
            m_chk = checked_re.match(line.rstrip("\r\n"))
            if m_chk:
                indent = m_chk.group(1)
                nl = "\r\n" if line.endswith("\r\n") else "\n"
                out.append(line)
                # Skip an immediately following old fingerprint line if present.
                if i + 1 < len(lines) and fp_re.match(lines[i + 1].rstrip("\r\n")):
                    i += 1
                out.append(f"{indent}fingerprint: {fingerprints_by_id[current_id]}{nl}")
                updated += 1
                i += 1
                continue

        out.append(line)
        i += 1

    return "".join(out), updated


def record_fingerprints(
    *,
    source_ids: set[str] | None = None,
    sources_dir: Path | None = None,
    timeout: int = 25,
    max_retries: int = 2,
    pause_seconds: float = 0.25,
    fetcher: Callable[[str, int], tuple[int, dict[str, str], bytes]] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[int, list[str]]:
    """Fetch sources and record normalized `fingerprint:` next to `checked:` in registry YAML files."""
    fetch_fn = fetcher or _default_fetch_response
    url_cache: dict[str, tuple[str, int | str, dict[str, str]]] = {}
    total_updated = 0
    warnings: list[str] = []

    for yaml_path in iter_source_files(sources_dir):
        raw_text = yaml_path.read_text(encoding="utf-8")
        entries = yaml.safe_load(raw_text) or []
        fp_updates: dict[str, str] = {}
        for s in entries:
            sid = str(s["id"])
            if source_ids and sid not in source_ids:
                continue
            if s.get("link_check") == "by hand":
                continue
            url = str(s["url"])
            if url not in url_cache:
                url_cache[url] = _probe_url(
                    url,
                    timeout=timeout,
                    max_retries=max_retries,
                    fetcher=fetch_fn,
                    sleeper=sleeper,
                )
                if pause_seconds > 0:
                    sleeper(pause_seconds)
            kind, detail, fp_meta = url_cache[url]
            if kind == "ok" and fp_meta.get("fingerprint"):
                fp_updates[sid] = fp_meta["fingerprint"]
            elif kind == "http_error" and detail == 403 and s.get("bot_gated") is True:
                warnings.append(f"{sid} ({url}): skipped bot-gated HTTP 403")
            else:
                warnings.append(f"{sid} ({url}): {detail}")

        if fp_updates:
            new_text, count = _upsert_yaml_fingerprint_lines(raw_text, fp_updates)
            if count > 0 and new_text != raw_text:
                yaml_path.write_text(new_text, encoding="utf-8")
                total_updated += count

    return total_updated, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freshness-only", action="store_true")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Optional flat override in days (default: per-kind limits from 60 to 180 days)",
    )
    parser.add_argument(
        "--record-fingerprints",
        action="store_true",
        help="Fetch verified sources and record normalized content fingerprints next to `checked`",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Restrict --record-fingerprints to specific source ID(s) (repeatable)",
    )
    args = parser.parse_args(argv)

    if args.record_fingerprints:
        target_ids = {s.strip() for s in args.source_id if s.strip()} or None
        updated, warns = record_fingerprints(source_ids=target_ids)
        print(f"Recorded {updated} source fingerprint(s).")
        for w in warns:
            print("WARN:", w)
        return 0

    stale = check_freshness(max_age_days=args.max_age_days)
    for line in stale:
        print("STALE:", line)

    expired_reviews = check_review_by_dates()
    for line in expired_reviews:
        print("REVIEW_BY:", line)

    link_issues: list[str] = []
    if not args.freshness_only:
        link_issues = check_links()
        for line in link_issues:
            print("LINK:", line)

    return 1 if (stale or expired_reviews or link_issues) else 0


if __name__ == "__main__":
    sys.exit(main())
