#!/usr/bin/env python3
"""Weekly source link and freshness check across MED-* and ID-* registries.

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

import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "CivicDesk-Verify/1.0"


def check_freshness(max_age_days: int = 120) -> list[str]:
    today = dt.date.today()
    stale: list[str] = []
    for rel in ("sources/medicaid.yaml", "sources/id_records.yaml"):
        entries = yaml.safe_load((ROOT / "data" / rel).read_text(encoding="utf-8"))
        for s in entries:
            checked = dt.date.fromisoformat(str(s["checked"]))
            age = (today - checked).days
            if age > max_age_days:
                stale.append(f"{s['id']}: last checked {age} days ago ({checked.isoformat()}, limit {max_age_days})")
    return stale


def check_links(timeout: int = 20) -> list[str]:
    issues: list[str] = []
    seen_urls: set[str] = set()
    for rel in ("sources/medicaid.yaml", "sources/id_records.yaml"):
        entries = yaml.safe_load((ROOT / "data" / rel).read_text(encoding="utf-8"))
        for s in entries:
            if s.get("link_check") == "by hand":
                continue
            url = str(s["url"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status >= 400:
                        issues.append(f"{s['id']} ({url}): HTTP {resp.status}")
            except urllib.error.HTTPError as exc:
                if exc.code in {403, 429}:
                    print(f"note (bot-gated HTTP {exc.code}, check in browser): {s['id']} {url}")
                else:
                    issues.append(f"{s['id']} ({url}): HTTP {exc.code}")
            except Exception as exc:
                issues.append(f"{s['id']} ({url}): {exc}")
            time.sleep(0.25)
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
