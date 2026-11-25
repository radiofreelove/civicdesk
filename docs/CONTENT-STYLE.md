# CivicDesk Content Style Guide

## 1. Multi-Desk Plain-Language Rules

- Follow UK Home Office and GOV.UK inclusive communication standards.
- Use active voice, short sentences, concrete verbs, and everyday words.
- Explain agency acronyms on first use (e.g., Oregon Health Plan (OHP)).
- Never offer legal advice; state clearly what the rule says and link to the primary source.

## 2. Global `/` and `/start/` Triage Rules

- `/` is a neutral front door introducing CivicDesk and its active desks without overwhelming the reader.
- `/start/` is a calm 2-question router (state + topic) that routes readers directly to the exact guide they need.

## 3. Neurodivergent Layout Guardrails

- Left-aligned text only; use sentence case for headings and labels.
- Maintain >=7:1 contrast ratio (AAA Civic Horizon palette: `#1D70B8` sky, `#FFFFFF` paper, `#C85A70` muted rose on `#111827` slate ink).
- Use semantic SVG lists and responsive containers that reflow cleanly at 200% zoom.
- Ensure all interactive controls have >=44x44 px touch targets.

## 4. Four-Tier Source Honesty Labels

Every claim backed by `data/sources/medicaid.yaml` (`MED-*`) or `data/sources/id_records.yaml` (`ID-*`) must carry one of four explicit status labels:
1. `official` — Confirmed in a published government statute, rule, or agency form/guidance.
2. `not_official` — Reported by an external legal aid, advocacy, or news source, not yet published by the agency.
3. `not_confirmed` — Mentioned in draft policy or pending guidance without final confirmation.
4. `our_reading` — CivicDesk's plain-language synthesis across multiple primary sources, clearly labeled as an interpretation.

## 5. Scoped "Leave quickly" Safety Rules

- The calm **Leave quickly** control (replacing `location` with `weather.gov`, plus triple-Shift shortcut) is scoped strictly to `/id-records/*` pages and domestic-safety exemption callouts.
- Never display **Leave quickly** as a red banner or on `/` or `/medicaid/` pages.
