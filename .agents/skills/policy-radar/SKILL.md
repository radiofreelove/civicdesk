---
name: policy-radar
description: Triage open policy-radar GitHub issues (radar:medicaid and radar:id-records) or run ad-hoc authoritative policy sweeps on radiofreelove/civicdesk, verify leads against primary .gov and court sources, run full-repo checks, and open Draft PRs.
---
# CivicDesk Policy Radar Skill

Use this skill when triaging `policy-radar` issues (`radar:medicaid` or `radar:id-records`) or running ad-hoc primary-source verification sweeps in `radiofreelove/civicdesk`.

## 1. Read the Runbook & Rules First

Read [`AGENTS.md`](../../../AGENTS.md), [`docs/RADAR-RUNBOOK.md`](../../../docs/RADAR-RUNBOOK.md), and [`docs/CONTENT-STYLE.md`](../../../docs/CONTENT-STYLE.md) before making any change.

## 2. Hard Guardrails

1. **Protected `main` branch:** Never push directly to `main`. Create a `radar/*` branch and open a Draft PR (`gh pr create --draft`).
2. **Two-Account Cloudflare Boundary:** Never query Cloudflare CLI or MCP for retired legacy projects. CivicDesk is hosted solely on Cloudflare Pages (`civicdesk.net`).
3. **Leads are never authority:** News RSS, LegiScan, and search summaries are leads (`not_official`). Confirm changes directly in primary `.gov` statutes, administrative rules, court dockets, or official agency forms, sending `User-Agent: CivicDesk-Verify/1.0`. Never bypass CAPTCHAs or anti-bot gates.
4. **Desk-partitioned registries & 4-tier labels:**
   - Update `data/sources/medicaid.yaml` (`MED-*`) or `data/sources/id_records.yaml` (`ID-*`).
   - Allowed `status` values: `official`, `not_official`, `not_confirmed`, `our_reading`.
5. **Pre-PR required checks:**
   ```bash
   python3 scripts/build.py
   python3 scripts/check_brand_purge.py
   pytest
   ```
