# CivicDesk Roadmap

## Milestone 1: Unified Platform Launch (Completed November 25, 2026)

- Launched the unified CivicDesk platform (`/`, `/start/`, `/medicaid/`, and `/id-records/`) ahead of the **December 31, 2026** Oregon Health Plan (OHP) retroactive coverage cutoff and the **January 1, 2027** Medicaid work and activity rules start date.
- Included the cross-desk ID name-mismatch callout connecting `/medicaid/` eligibility verification with `/id-records/` name and record correction guides.

## Milestone 2: Production Cutover & Policy Wire (Completed November 25, 2026)

- Deployed `civicdesk.net` in production on **Cloudflare Workers Static Assets** (`wrangler.jsonc`).
- Enabled automated twice-weekly Policy Wire dispatches (`scripts/policy_radar.py`) and Monday source link verification (`scripts/verify_sources.py`).

## Milestone 3: Community Review & `/public-records/` Desk (Q1 2027)

- Conduct community and legal-aid review of the `/medicaid/` and `/id-records/` desks.
- Launch the `/public-records/` desk using the same verified primary-source architecture.
