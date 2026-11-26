# CivicDesk Policy Wire Runbook

## 1. Architecture & Twice-Weekly Wire Schedule

CivicDesk operates a read-only, multi-desk **Policy Wire** (`scripts/policy_radar.py` + `data/wire/*.yaml`) that monitors primary government rules, federal and appellate court dockets, state and federal legislation, `.gov`/court web updates, and state policy journalism without ever modifying repository files directly.

| Workflow | Schedule | Scope & Boundary |
|---|---|---|
| **Policy Radar / Wire** ([`radar.yml`](../.github/workflows/radar.yml)) | **Tuesdays & Fridays (`16:23 UTC`)** + manual `workflow_dispatch` | Dynamically discovers active desks from `data/wire/*.yaml`, sweeps all 6 wire channels, maps hits to `MED-*` / `ID-*` sources, `facts.yaml`, and live routes, and opens or updates `policy-radar` + `radar:<desk>` issues (plus `wire:urgent` when urgent dispatches arrive). |
| **Weekly Source Verification** ([`weekly.yml`](../.github/workflows/weekly.yml)) | **Mondays (`16:17 UTC`)** + manual `workflow_dispatch` | Runs full build, brand-purge, source freshness (`120`-day window), and live URL checks across all registered sources. Opens an `automated-source-check` issue if human attention is needed. |
| **Verification & Draft PRs** | On demand in Google Antigravity (`antigravity.google.com`) | Triages open `policy-radar` and `automated-source-check` issues against primary `.gov` and court sources, updates registries/templates, runs full-repo checks, and opens a **Draft PR** on a `radar/*` branch. Never pushes directly to `main`. |

All automated requests identify themselves with `User-Agent: CivicDesk-Verify/1.0`.

---

## 2. Six Wire Channels & Four Repository Secrets

The wire runs out of the box with zero secrets on public endpoints and activates four `$0`/free-tier API channels configured in **Settings -> Secrets and variables -> Actions**:

| Secret / Channel | Authority Tier | Role in CivicDesk Policy Wire | If Unset |
|---|---|---|---|
| *(Keyless)* **Federal Register API v1** | `official` (`primary`) | Rules, proposed rules, and agency notices filtered per desk (`data/wire/*.yaml`). | Always runs |
| *(Keyless)* **eCFR Versioner API v1** | `official` (`primary`) | Section-level CFR amendment detection (`42 CFR 435`, `8 CFR 212`, `22 CFR 51`, `20 CFR 422`, `6 CFR 37`). | Always runs |
| *(Keyless)* **CourtListener RECAP Atom Feeds** | `official` (`primary`) | New docket filings on watched federal cases plus any CourtListener docket cited in `data/sources/*.yaml`. | Always runs |
| `COURTLISTENER_TOKEN` | `official` (`primary`) | CourtListener REST API v4 discovery searches across brand-new federal RECAP dockets (`type=r`) and federal/appellate judicial opinions (`type=o`). | Skipped cleanly |
| `LEGISCAN_API_KEY` | `not_official` (`lead`) | State and federal bill tracking (`OR`, `WA`, `UT`, `US`) with `year=3` and `action:` windowing (including scheduled committee hearings up to 90 days out). | Skipped cleanly |
| `TAVILY_API_KEY` | `not_official` (`lead`) | `POST /search` (`search_depth: "basic"`) restricted strictly to the desk's official `.gov`, state legislature, and court domains (`~6` credits per full biweekly run out of `1,000` free monthly credits). | Skipped cleanly |
| `GEMINI_API_KEY` | Editorial triage (free tier) | Free-tier JSON editorial triage (`responseMimeType: "application/json"`, Flash-Lite / Flash auto-discovery) filtering evergreen pages out of Tavily `.gov` candidates and generating concise plain-language triage notes when unseen dispatches arrive. | Preserves score-filtered Tavily `.gov` leads |
| *(Keyless)* **States Newsroom & KFF RSS** | `not_official` (`lead`) | Deduplicated journalism wire across KFF Health News, Oregon Capital Chronicle, Washington State Standard, Utah News Dispatch, and Stateline. | Always runs |

---

## 3. Adding a New Desk or State (Zero Python or Workflow Edits)

Desk beats are configured declaratively in `data/wire/*.yaml`:

- [`data/wire/medicaid.yaml`](../data/wire/medicaid.yaml) — `medicaid` (`MED-*`, `/medicaid/`)
- [`data/wire/id_records.yaml`](../data/wire/id_records.yaml) — `id-records` (`ID-*`, `/id-records/`)
- [`data/wire/public_records.yaml`](../data/wire/public_records.yaml) — `public-records` (`PUB-*`, `/public-records/`, pre-scaffolded with `enabled: false` for Milestone 3)

To launch a new desk or expand to a new state:
1. Add or update the YAML profile in `data/wire/<desk>.yaml` with `enabled: true` (adding any new state under `jurisdictions:` automatically wires domain scoping, Tavily restrictions, and jurisdiction badges).
2. Verify discovery locally:
   ```bash
   python3 scripts/policy_radar.py --list-desks
   python3 scripts/policy_radar.py --desk <desk> --days 7
   ```
3. On merge, `radar.yml` automatically includes the desk in the Tuesday/Friday matrix.

---

## 4. Antigravity Verification & Draft PR Protocol

1. **Sync and branch (never push directly to `main`):**
   ```bash
   git checkout main && git pull --ff-only
   git checkout -b radar/verify-YYYY-MM-DD
   ```
2. **Inspect open wire issues:**
   ```bash
   gh issue list -R radiofreelove/civicdesk --state open --label "policy-radar" --json number,title,labels,body,comments
   ```
3. **Verify leads in primary `.gov` or court sources:**
   - Leads (`not_official` — LegiScan, Tavily + Gemini, RSS) are never cited directly as authority for what an agency rule says.
   - Open the controlling statute, administrative rule, court order, or official agency publication/form (`User-Agent: CivicDesk-Verify/1.0`). Never bypass CAPTCHAs or anti-bot gates; list any blocked URL under **Needs human browser check before merge** in the Draft PR body.
   - Update `data/sources/medicaid.yaml` (`MED-*`), `data/sources/id_records.yaml` (`ID-*`), `data/facts.yaml`, and affected templates only for sources actually read during the session.
4. **Run required full-repo checks before opening a Draft PR:**
   ```bash
   python3 scripts/build.py
   python3 scripts/check_brand_purge.py
   pytest
   ```
5. **Open a Draft PR (`gh pr create --draft`):**
   - Wait for both required status checks (`Checks` and `Workers Builds: civicdesk` on Cloudflare Workers Static Assets) to pass before marking ready for review and merging.
