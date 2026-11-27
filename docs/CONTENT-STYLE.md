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
- Maintain >=7:1 contrast ratio (Broadsheet visual system: `#0088B0` process cyan, `#D6006C` process magenta, `#F3F2F2` newsprint on `#201E1D` press ink).
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

## 6. Four-beat page fields (`data/pages.yaml`)

Each guide page leads with what a tired reader needs, then the proof. Fields live in `data/pages.yaml` and render through macros in `templates/macros.html`; `scripts/build.py` rejects malformed entries.

| Beat | Field | Macro | Rule |
|---|---|---|---|
| 1 | `answer`, `why` | `m.answer(page)` | One plain sentence answering the real question, then a "Why it matters" line in money, time, or rejected paperwork. |
| 2 | `analogies` | `m.analogy(page)`, `m.analogy(page, 'id')` | Everyday comparison of how the agency system works (see below). |
| 3 | `spec` | `m.spec_strip(page)` | Optional rows: `cost` and `clock` (facts.yaml keys), `paperwork` (source ID of the official blank form), `catch` (`text` plus `basis`). Empty rows do not render, so explainers without fees or forms show no filler. |
| 4 | Page body | `m.deeper` | Statutes, rule pinpoints, and edge cases. |

### Analogy rules

```yaml
analogies:
  - id: judgment-carry        # lowercase-kebab-case, unique on the page
    placement: lead           # one lead per page; inline for optional micro-analogies
    compare: "..."            # how the agency system works, in everyday terms
    boundary: "..."           # exactly one sentence: where the comparison stops or an exception applies
    basis: [ID-..., MED-...]  # at least one source ID from data/sources/*.yaml
```

- The comparison explains how the system behaves (who checks what, in what order, against which record). It never tells the reader which personal legal choice to make.
- The boundary names where the comparison breaks down, such as another state's different rule or an office that uses its own form instead of a court order.
- Every analogy renders with an "Our reading" badge and its basis sources. Treat each one as a factual claim: check it against the primary page behind every basis ID.
- The lead analogy sits outside `m.deeper` so the Short version shows it.

### Which pages need the fields

`data/voice_rollout.yaml` is the rollout ledger. `tests/test_page_fields.py` requires `answer` and `why` on every page except 404, and a lead analogy on the 41 substantive guide and hub pages. The 27 exempt pages (utility pages, source registries, tools, glossaries, hours logs, help directories, and the three Medicaid immigration pages) are listed under `analogy_exempt`. Pages under `pending` have not had their content pass yet; remove a slug from `pending` in the same change that adds its fields.

## 7. Tool voice check (`scripts/check_voice.py`)

Interactive tools (the ID path finder, the 1-minute Medicaid checks, and `/start/`) describe what a state's published rules list and what its agency asks. They never tell one reader what applies to them or what to file. `pytest` runs the check over the tools' built `<main>`, the tool scripts' string literals, and the `pages.yaml` voice fields, and fails on:

`you should`, `we recommend`, `your best`, `personalized`, `my steps`, `apply to you` (including "do not apply to you"), `your steps`, `my results`, `you qualify` / `you are exempt`, `fits your situation`, and a heading that is only "What to do" or "What you can do".

Bare sentence-initial verbs ("File", "Use", "Tell", "Skip"), "you must", and "you need to" are not enforced, because in CivicDesk copy they describe what a rule requires. Write tool results in rule-indexed form, such as "Utah's rules list this reason as an exclusion" and "What DWS asks people in this group to do".

## 8. Sitemap dates

`<lastmod>` follows page content, not source check dates. Running `python3 scripts/build.py` locally updates `data/lastmod.json` when a page's visible text or outside links change; commit that file with the content change. CI and Cloudflare builds never write it. If it is stale there, the build keeps the older date and prints a warning instead of failing.
