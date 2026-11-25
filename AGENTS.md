# Agent Guidelines for CivicDesk (`radiofreelove/civicdesk`)

Rules for browser-based Google Antigravity and automated contributors working on `radiofreelove/civicdesk`.

## 1. Protected `main` Branch Workflow

- Never push directly to `main`. All changes must go through `feature/*` or `radar/*` branches and Pull Requests.
- Branch protection settings on `main`: `enforce_admins: true`, `0` required approvals, and required status checks: `Checks` and `Cloudflare Pages`.

## 2. Strict Two-Account Cloudflare Boundary

- The connected Cloudflare account **ONLY** hosts `civicdesk.net` and its Cloudflare Pages project.
- The two retired legacy `pages.dev` projects live in a separate Cloudflare account that agents must **never** query or modify via Cloudflare CLI or MCP.
- Legacy source material may **only** be read from public GitHub repositories (`radiofreelove/projectstasis` and `radiofreelove/projectstrand`).
- **Cloudflare Pages is the sole host.** Do not create or maintain secondary mirror folders or deployment workflows.

## 3. Universal Civic Mission & Quiet Inclusion

- `/` is the neutral CivicDesk front door, and `/start/` is a 2-question state-and-topic router.
- Use the AAA Civic Horizon palette (`#1D70B8` sky, `#FFFFFF` paper, `#C85A70` muted rose on `#111827` slate ink) and the 5-stripe service-desk mark.
- Frame `/id-records/` broadly around marriage, divorce, court name changes, and sex/gender designation updates.
- Scope the calm **Leave quickly** control (replacing `location` with `weather.gov` + triple-Shift) strictly to `/id-records/*` and domestic-safety exemption callouts — **never** display it as a red banner on `/` or `/medicaid/`.

## 4. Accessibility, Source Registries & Pre-PR Checks

- **UK Home Office / GOV.UK accessibility rules:** Left-aligned sentence case, semantic SVG lists for 200% zoom reflow, >=7:1 contrast ratio, and >=44x44 px touch targets.
- **Desk-partitioned source registries:**
  - `data/sources/medicaid.yaml` using `MED-*` IDs
  - `data/sources/id_records.yaml` using `ID-*` IDs
  - Allowed status labels: `official`, `not_official`, `not_confirmed`, `our_reading`
- **Required full-repo checks before any PR:**
  - `python3 scripts/build.py`
  - `python3 scripts/check_brand_purge.py`
  - `pytest`
