---
name: policy-radar
description: Triage open CivicDesk Policy Wire GitHub issues (radar:medicaid, radar:id-records, wire:urgent, and future desks in data/wire/*.yaml) or run ad-hoc authoritative policy sweeps on radiofreelove/civicdesk, verify leads against primary .gov and court sources, run full-repo checks, and open Draft PRs.
---
# CivicDesk Policy Wire & Verification Skill

Use this skill when triaging `policy-radar` issues (`radar:medicaid`, `radar:id-records`, or future desks configured in `data/wire/*.yaml`) or running ad-hoc primary-source sweeps in `radiofreelove/civicdesk`.

## 1. Read the Runbook & Rules First

Read [`AGENTS.md`](../../../AGENTS.md), [`docs/RADAR-RUNBOOK.md`](../../../docs/RADAR-RUNBOOK.md), and [`docs/CONTENT-STYLE.md`](../../../docs/CONTENT-STYLE.md) before making any change.

## 2. Hard Guardrails

1. **Protected `main` branch:** Never push directly to `main`. Create a `radar/*` or `feature/*` branch and open a Draft PR (`gh pr create --draft`).
2. **Two-Account Cloudflare Boundary:** Never query Cloudflare CLI or MCP for retired legacy projects. CivicDesk is hosted solely on Cloudflare Workers Static Assets (`civicdesk.net`, status check `Workers Builds: civicdesk`).
3. **Leads are never authority:** Journalism RSS, LegiScan, and Tavily + Gemini search summaries are leads (`not_official`). Confirm every factual update directly in primary `.gov` statutes, administrative rules, court dockets, or official agency forms (`User-Agent: CivicDesk-Verify/1.0`). Never bypass CAPTCHAs or anti-bot gates.
4. **Desk-partitioned registries & 4-tier labels:**
   - Update `data/sources/medicaid.yaml` (`MED-*`) or `data/sources/id_records.yaml` (`ID-*`) — plus `data/facts.yaml` when tracked fees or dates change.
   - Allowed `status` values: `official`, `not_official`, `not_confirmed`, `our_reading`.
5. **Ad-hoc local wire sweeps:**
   ```bash
   python3 scripts/policy_radar.py --list-desks
   python3 scripts/policy_radar.py --desk all --days 7
   ```
6. **Pre-PR required checks:**
   ```bash
   python3 scripts/build.py
   python3 scripts/check_brand_purge.py
   pytest
   ```
