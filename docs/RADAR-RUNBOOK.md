# CivicDesk Policy Radar Runbook

## Overview

CivicDesk uses a two-stage Policy Radar to detect changes in statutes, agency rules, court dockets, and official forms before they break guidance for readers.

## Schedule & Workflows

- **Tuesday & Friday (`radar.yml`):** Runs a matrixed Policy Radar scan across both active desks (`radar:medicaid` and `radar:id-records`).
- **Monday (`weekly.yml`):** Runs full weekly link, registry, and build verification checks across all sources.

## Request Identification

- All automated source checks must send the `User-Agent: CivicDesk-Verify/1.0` header.

## Optional Repository Secrets

- `COURTLISTENER_TOKEN` — CourtListener API access for federal/state docket monitoring.
- `LEGISCAN_API_KEY` — LegiScan API access for state legislative bill tracking.
- `GEMINI_API_KEY` — Optional LLM summarization/triage assistance during Stage 2 analysis.

## Antigravity Draft PR Verification

1. When Policy Radar detects a substantive change, it opens a Draft PR on a `radar/*` branch.
2. Google Antigravity verifies the primary source change, updates the corresponding `MED-*` or `ID-*` entry and guide prose, and runs full-repo checks (`python3 scripts/build.py`, `python3 scripts/check_brand_purge.py`, `pytest`).
3. Once `Checks` and `Cloudflare Pages` status checks pass, the PR is marked ready for review and merged into `main`.
