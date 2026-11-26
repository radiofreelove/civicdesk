# Civic Horizon Visual System (`docs/DESIGN.md`)

CivicDesk's visual system translates modern frontend craft principles into a high-trust, graphic-led, neurodivergent-friendly, WCAG 2.2 AAA public-service interface. Every visual element exists to break up text fatigue and lower cognitive load when navigating complex government rules, forms, fees, and agency dependencies.

---

## 1. Core Design Principles

1. **Graphic-Led Text Relief Across All 68 Pages:** Every page in `data/pages.yaml` pairs concise plain-language guidance with expressive multi-layer duotone illustrations (`220px–420px`), realistic CSS+SVG **Anatomy of Paperwork** facsimiles, **Big-Number (`tabular-nums`) callout strips**, **State map silhouettes**, **Visual Yes/No & Jargon Flashcard boards**, and `<details class="deeper">` progressive disclosure.
2. **Concise Node Copy & Sleek Jump Pills:** Flowchart cards (`.viz-flow-card`) and timeline milestones (`.viz-timeline-item`) lead with `56x56` duotone artwork medallions, a 2–5 word title, and `<= 12` words (`<= 140` characters) of body text—never cramped multi-sentence columns. Page tables of contents render as compact horizontal **Jump Pill Strips** (`.toc-pills`).
3. **Quiet Inclusion & Strict Self-Hosted CSP (`default-src 'none'`):** All layout proportions, colors, mockups, and SVG fills use external CSS classes in `static/css/site.css` (`style-src 'self'`). Inline `style="..."` attributes and `<style>` blocks are forbidden across templates, SVGs, and JavaScript.

---

## 2. Typography & Scannable Rhythm

- **Maximum 2 Font Families:**
  - Default: system sans-serif stack (`system-ui, -apple-system, "Segoe UI", Roboto, ...`) for zero-latency rendering.
  - Optional reader preference (`html[data-font="hyper"]`): self-hosted upright **Atkinson Hyperlegible Next** (`400` and `700` WOFF2, SHA-256 verified at build time) with `font-synthesis: none` and upright `em`/`i` rules.
- **Left-Aligned Sentence Case & Measure:** Every heading, navigation item, kicker, badge, and visual label uses left-aligned sentence case per UK Home Office and GOV.UK standards, with `<= 65ch` prose measure.
- **Tabular Numerals:** Oversized stat figures (`2.25rem–2.85rem`), dates, hour counts (`80 hrs`), dollar thresholds (`$580`, `$124`), and step numbers use `font-variant-numeric: tabular-nums`.

---

## 3. Color & Semantic `--viz-*` Tokens (WCAG 2.2 AAA)

All four reading themes (`light`, `warm`, `dark` + `prefers-color-scheme: dark`, and `contrast` + `prefers-contrast: more`) preserve CivicDesk's locked **Civic Horizon** anchors (`#1D70B8` sky, `#FFFFFF` paper, `#C85A70` muted rose, `#111827` slate ink) and verify every `--viz-*` token in `tests/test_contrast_and_radar.py`:

| Token | Role | Contrast Requirement |
|---|---|---|
| `--viz-surface` / `--viz-surface-2` | Tinted diagram & stage containers | `>= 7.0:1` with `--text`, `--heading`, and `--link` (WCAG 1.4.6 AAA) |
| `--viz-fill-1` / `--viz-fill-2` / `--viz-fill-3` | Layered duotone SVG & mockup accent washes (Sky / Rose / Sage) | `>= 7.0:1` with `--text`, `--heading`, and `--link` |
| `--viz-paper` / `--viz-shadow` | Realistic document sheet surface & crisp offset shadow | `>= 7.0:1` text contrast on `--viz-paper` |
| `--viz-border` | Structural card, mockup, and illustration stroke | `>= 3.0:1` against `--bg`, `--surface`, and `--viz-surface` (WCAG 1.4.11) |
| `--viz-accent` / `--viz-accent-2` / `--viz-accent-3` | Primary/secondary/tertiary graphical strokes & meter fills | `>= 3.0:1` non-text contrast |
| `--viz-node-bg` / `--viz-node-text` | Step counter and active progress node badge | `>= 7.0:1` text-on-fill contrast |

Inside `@media print`, all `--viz-*` tokens reset to crisp `#000000` ink on `#FFFFFF` paper.

---

## 4. Reflow-Safe Hybrid HTML + Duotone SVG Architecture

- **Readable Labels in HTML, Artwork in `<svg aria-hidden="true">`:** Multi-word labels, dates, fees, and callouts live in semantic HTML (`<ol>`, `<ul>`, `<dl>`, `<h3>`, `<p>`) inside CSS Grid/Flex stages so **Extra large** text (`150%`), **Wide spacing** (WCAG 1.4.12), and `320px` viewports (WCAG 1.4.10) reflow cleanly without horizontal scrollbars or clipped words (only short `<= 4`-character visual stamps like `OR`, `WA`, `UT`, `2027`, `80h` appear inside `aria-hidden="true"` SVGs).
- **Reusable Macro Suites:**
  - `templates/illustrations.html`: `scene_civic_horizon`, `scene_medicaid_desk`, `scene_id_desk`, `state_map_badge`, `medallion`, `scene_channel`, `scene_trust_privacy`, `scene_lost_compass`.
  - `templates/visuals.html`: `stat_hero_strip`, `doc_mockup_letter`, `doc_mockup_id_card`, `doc_mockup_court_order`, `doc_mockup_birth_cert`, `doc_mockup_federal_ids`, `calendar_80hr_grid`, `yes_no_split_board`, `swimlane_timeline`, `jargon_flashcards`, `channel_cards`, `process_flow`, `timeline_strip`, `hours_breakdown`, `icon_feature_grid`, `step_progress_track`, `trust_privacy_diagram`, `reading_theme_tiles`, `lost_page_stage`.

---

## 5. Motion, Progressive Disclosure & Print

- **Restrained Transitions:** Interactive cards, pills, and choice rows use `160ms cubic-bezier(0.22, 1, 0.36, 1)` scoped to `border-color`, `background-color`, `color`, and `box-shadow`, disabled under `@media (prefers-reduced-motion: reduce)`.
- **Progressive Disclosure (`<details class="deeper">` & `data-brief`):** Dense edge cases fold into accessible `<details class="deeper">` panels while preserving all heading/section `id`s and primary citations; `@media print` expands `<details>` automatically and keeps visual mockups intact (`break-inside: avoid`).
