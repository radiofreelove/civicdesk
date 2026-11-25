# ADR-0001: CivicDesk Architecture

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

CivicDesk unifies plain-language guides for Medicaid work and activity rules and legal name, ID, and vital records under `civicdesk.net` without duplicating build tooling, accessibility checks, or deployment pipelines.

## Decisions

1. **Single Jinja2 + Python 3.13 Monorepo:** All templates, static assets, desk-partitioned source registries (`data/sources/medicaid.yaml`, `data/sources/id_records.yaml`), and verification scripts live in `radiofreelove/civicdesk`.
2. **Explicit Desk URL Prefixes:** Each topic desk lives under an explicit URL prefix (`/medicaid/`, `/id-records/`, and future `/public-records/`), routed from a neutral `/` front door and a 2-question `/start/` triage page.
3. **Cloudflare Registrar + Workers Static Assets Exclusivity:** `civicdesk.net` is registered on Cloudflare Registrar and hosted exclusively on Cloudflare Workers Static Assets (`wrangler.jsonc` pointing `assets.directory` at `./public`, built via Workers Builds). No secondary mirror folders or deployment workflows are maintained.
4. **Two-Account Cloudflare Boundary:** The connected Cloudflare account exclusively hosts `civicdesk.net` and its Cloudflare Workers Static Assets service. Legacy `pages.dev` projects reside in a separate Cloudflare account that agents must never query or modify.
5. **Structural Inclusion:** Accessibility (UK Home Office / GOV.UK standards, >=7:1 contrast, 200% zoom reflow, >=44x44 px touch targets), shared-computer privacy (`Referrer-Policy: no-referrer`, zero cookies/analytics), and scoped **Leave quickly** safety controls on `/id-records/*` are built into the core layout.
