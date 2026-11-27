# CivicDesk Policy Wire Runbook

## 1. Architecture, State Ledger & Twice-Weekly Wire Schedule

CivicDesk operates a read-only, multi-desk **Policy Wire** (`scripts/policy_radar.py` + `data/wire/*.yaml`) that monitors normalized fingerprints of cited registry pages, primary federal and state administrative rules, federal and appellate court dockets, state and federal legislation, `.gov`/court web updates, and state policy journalism without modifying repository files during scheduled runs.

| Workflow | Schedule | Scope & Boundary |
|---|---|---|
| **Policy Radar / Wire** ([`radar.yml`](../.github/workflows/radar.yml)) | **Tuesday & Friday evenings Pacific (`23 2 * * 3,6`, 02:23 UTC Wednesday & Saturday)** + manual `workflow_dispatch` | Dynamically discovers active desks from `data/wire/*.yaml`, sweeps all 9 wire channels from each channel's `last_success - 3d` overlap cursor, persists per-desk JSON state in a single `wire:state` ledger issue, opens `wire:health` issues on channel errors or empty streaks, and opens/updates `policy-radar` + `radar:<desk>` issues (`wire:urgent` reserved strictly for direct registry edits, near-term final rules, court orders/opinions, and enacted statutes). |
| **Weekly Source Verification** ([`weekly.yml`](../.github/workflows/weekly.yml)) | **Monday evenings Pacific (`17 2 * * 2`, 02:17 UTC Tuesday)** + manual `workflow_dispatch` | Runs full build, brand-purge, `kind`-tiered source freshness (`60`–`180` days), `review_by` date checks on `data/facts.yaml`, stored `fingerprint` checks, live URL verification (`429` + `Retry-After` backoff), scheduled-workflow keepalive, and live API contract checks (`scripts/check_wire_contracts.py`). |
| **Verification & Draft PRs** | On demand in Google Antigravity (`antigravity.google.com`) | Triages open `policy-radar`, `wire:health`, and `automated-source-check` issues against primary `.gov` and court sources, updates registries/templates + source fingerprints, runs full-repo checks, and opens a **Draft PR** on a `radar/*` branch. Never pushes directly to `main`. |

All automated requests identify themselves with `User-Agent: CivicDesk-Verify/1.0` via shared retry/backoff (`scripts/http_retry.py`).

---

## 2. Wire Channels & Optional Repository Secrets

The wire runs out of the box with zero secrets on public primary endpoints and activates optional `$0`/free-tier API and heartbeat secrets configured in **Settings -> Secrets and variables -> Actions**:

| Secret / Channel | Authority Tier | Role in CivicDesk Policy Wire | If Unset |
|---|---|---|---|
| *(Keyless)* **Registry Page Watch** | `official` (`primary`) | Watches normalized HTML/PDF text and byte fingerprints (plus `ETag` / `Last-Modified` HTTP `304`) across every cited source in `data/sources/*.yaml`, firing `"Changed since you last read it on YYYY-MM-DD"` dispatches when an agency edits a form, fee schedule, or undated PDF in place. | Always runs |
| *(Keyless)* **Federal Register API v1 + Public Inspection** | `official` (`primary`) | Paginates (`per_page=100` + `next_page_url`) across desk search terms, watched CFR title/part pairs, and pre-publication Public Inspection filings, displaying `effective_on` and `comments_close_on`. | Always runs |
| *(Keyless)* **eCFR Versioner API v1** | `official` (`primary`) | Section-level CFR codification detection (`42 CFR 435`, `8 CFR 212`, `22 CFR 51`, `20 CFR 422`, `6 CFR 37`), filtering on `issue_date` (plus `amendment_date`) so newly codified rules carrying earlier amendment dates are never skipped. | Always runs |
| *(Keyless)* **State Registers & Legislatures** | `official` (`primary`) | Polls Utah Office of Administrative Rules RSS (`rules.utah.gov/feed/`), Washington State Register agency filings (`lawfilesext.leg.wa.gov/law/wsr/`), and Oregon Legislative Information System OData (`api.oregonlegislature.gov`). | Always runs |
| *(Keyless)* **CourtListener RECAP Atom Feeds** | `official` (`primary`) | New docket filings (checking both `<published>` and `<updated>` to catch late RECAP PDF uploads) on watched federal cases plus any CourtListener docket cited in `data/sources/*.yaml`. | Always runs |
| `COURTLISTENER_TOKEN` | `official` (`primary`) | CourtListener REST API v4 newest-first (`order_by=dateFiled desc`) discovery across federal RECAP dockets (`type=r`) and judicial opinions (`type=o`). | Skipped cleanly |
| `LEGISCAN_API_KEY` | `not_official` (`lead`) | State and federal bill search (`year=3`, `action:YYYYMMDD..YYYYMMDD`) plus `tracked_bills` `change_hash` polling and `getBill` enrichment for official `.gov` `state_link` URLs and upcoming `calendar` committee hearings. | Skipped cleanly |
| `TAVILY_API_KEY` | `not_official` (`lead`) | `POST /search` (`include_domains_mode: "restrict"`, `include_published_date: true`, `filter_by_published_date: false`) across dated queries plus an undated PDF/form sweep on registry domains, tracked by URL + content snippet fingerprint so edited pages resurface. | Skipped cleanly |
| `GEMINI_API_KEY` | Editorial triage (free tier) | Enforced `responseSchema` JSON triage (`gemini-3.5-flash-lite` / `gemini-3.6-flash` with stable-before-preview auto-discovery), force-retaining any direct registry URL match and listing dropped candidates in a `<details>` audit block. | Preserves score-filtered Tavily `.gov` leads |
| *(Keyless)* **States Newsroom & KFF RSS** | `not_official` (`lead`) | Deduplicated journalism wire across KFF Health News, Oregon Capital Chronicle, Washington State Standard, Utah News Dispatch, and Stateline. | Always runs |
| `WIRE_HEARTBEAT_URL` *(optional)* | Operations health | Dead-man's-switch webhook URL (e.g., Healthchecks.io, Better Stack, Uptime Kuma) pinged on healthy runs (and `<url>/fail` on channel/contract alerts) so external monitoring notices if GitHub scheduled Actions ever stop running. | Skipped cleanly |

---

## 3. Adding a New Desk, State, or Tracked Bill

Desk beats are configured declaratively in `data/wire/*.yaml`:

- [`data/wire/medicaid.yaml`](../data/wire/medicaid.yaml) — `medicaid` (`MED-*`, `/medicaid/`)
- [`data/wire/id_records.yaml`](../data/wire/id_records.yaml) — `id-records` (`ID-*`, `/id-records/`)
- [`data/wire/public_records.yaml`](../data/wire/public_records.yaml) — `public-records` (`PUB-*`, `/public-records/`, pre-scaffolded with `enabled: false` for Milestone 3)

To launch a new desk, add a state, or follow a specific bill through every legislative action:
1. Add or update `data/wire/<desk>.yaml` (`enabled: true`, `jurisdictions:`, `channels.state_registers:`, or `channels.legiscan.tracked_bills: [{bill_id: 1900123, state: "OR", label: "HB 2050"}]`).
2. Verify discovery and live API contracts locally:
   ```bash
   python3 scripts/policy_radar.py --list-desks
   python3 scripts/check_wire_contracts.py
   python3 scripts/policy_radar.py --desk <desk> --days 7
   ```

---

## 4. Antigravity Verification & Draft PR Protocol

1. **Sync and branch (never push directly to `main`):**
   ```bash
   git checkout main && git pull --ff-only
   git checkout -b radar/verify-YYYY-MM-DD
   ```
2. **Inspect open wire, health, and source-check issues:**
   ```bash
   gh issue list -R radiofreelove/civicdesk --state open --label "policy-radar" --json number,title,labels,body,comments
   gh issue list -R radiofreelove/civicdesk --state open --label "wire:health" --json number,title,labels,body,comments
   gh issue list -R radiofreelove/civicdesk --state open --label "automated-source-check" --json number,title,labels,body,comments
   ```
3. **Verify leads in primary `.gov` or court sources & record fingerprints:**
   - Leads (`not_official` — LegiScan, Tavily + Gemini, RSS) are never cited directly as authority for what an agency rule says.
   - Open the controlling statute, administrative rule, court order, or official agency publication/form (`User-Agent: CivicDesk-Verify/1.0`). Never bypass CAPTCHAs or anti-bot gates; list any blocked URL under **Needs human browser check before merge** in the Draft PR body.
   - Update `data/sources/medicaid.yaml` (`MED-*`), `data/sources/id_records.yaml` (`ID-*`), `data/facts.yaml` (including `review_by: 'YYYY-MM-DD'` when a rule or fee changes on a known future date), and record normalized fingerprints next to `checked` for verified sources:
     ```bash
     python3 scripts/verify_sources.py --record-fingerprints --source-id MED-S01
     ```
4. **Run required full-repo checks before opening a Draft PR:**
   ```bash
   python3 scripts/build.py
   python3 scripts/check_brand_purge.py
   pytest
   ```
5. **Open a Draft PR (`gh pr create --draft`):**
   - Wait for both required status checks (`Checks` and `Workers Builds: civicdesk` on Cloudflare Workers Static Assets) to pass before marking ready for review and merging.
