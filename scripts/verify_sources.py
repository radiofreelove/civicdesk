#!/usr/bin/env python3
"""Weekly source link and freshness check across all data/sources/*.yaml registries.

Always sends `User-Agent: CivicDesk-Verify/1.0` per docs/RADAR-RUNBOOK.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "data" / "sources"
USER_AGENT = "CivicDesk-Verify/1.0"
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def iter_source_files(sources_dir: Path | None = None) -> list[Path]:
    base = sources_dir or SOURCES_DIR
    return sorted(base.glob("*.yaml"))


def _default_fetch_status(url: str, timeout: int) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status)


def _probe_url(
    url: str,
    *,
    timeout: int,
    max_retries: int,
    fetcher: Callable[[str, int], int],
    sleeper: Callable[[float], None],
) -> tuple[str, int | str]:
    attempt = 0
    while True:
        try:
            status = fetcher(url, timeout)
            if status in TRANSIENT_HTTP_CODES and attempt < max_retries:
                attempt += 1
                sleeper(0.5 * (2 ** (attempt - 1)))
                continue
            if status >= 400:
                return ("http_error", status)
            return ("ok", status)
        except urllib.error.HTTPError as exc:
            if exc.code in TRANSIENT_HTTP_CODES and attempt < max_retries:
                attempt += 1
                sleeper(0.5 * (2 ** (attempt - 1)))
                continue
            return ("http_error", int(exc.code))
        except Exception as exc:  # noqa: BLE001
            if attempt < max_retries:
                attempt += 1
                sleeper(0.5 * (2 ** (attempt - 1)))
                continue
            return ("exc", str(exc))


def check_freshness(
    max_age_days: int = 120,
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
            if age > max_age_days:
                stale.append(f"{s['id']}: last checked {age} days ago ({checked.isoformat()}, limit {max_age_days})")
    return stale


def check_links(
    timeout: int = 20,
    *,
    sources_dir: Path | None = None,
    max_retries: int = 2,
    pause_seconds: float = 0.25,
    fetcher: Callable[[str, int], int] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[str]:
    issues: list[str] = []
    outcomes: dict[str, tuple[str, int | str]] = {}
    fetch_fn = fetcher or _default_fetch_status

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

            kind, detail = outcomes[url]
            if kind == "ok":
                continue
            if kind == "http_error" and detail == 403 and s.get("bot_gated") is True:
                print(f"note (bot-gated HTTP 403, check in browser): {s['id']} {url}")
                continue
            if kind == "http_error":
                issues.append(f"{s['id']} ({url}): HTTP {detail}")
            else:
                issues.append(f"{s['id']} ({url}): {detail}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freshness-only", action="store_true")
    args = parser.parse_args()

    stale = check_freshness()
    for line in stale:
        print("STALE:", line)

    link_issues: list[str] = []
    if not args.freshness_only:
        link_issues = check_links()
        for line in link_issues:
            print("LINK:", line)

    return 1 if (stale or link_issues) else 0


if __name__ == "__main__":
    sys.exit(main())
