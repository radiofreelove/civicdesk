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

---

## 6. Custom Artist Logo Spec & 3-Step Swap Guide

CivicDesk includes a declarative `brand:` block in `data/site.yaml` so a custom artist-designed mark or full wordmark lockup can be dropped into `static/brand/` with zero HTML template or CSS edits.

### Artist Technical Deliverable Spec

1. **Clean Self-Contained Vector `.svg` Files (Strict CSP Safe):**
   - Export clean `.svg` artwork with an explicit `viewBox` attribute (for example `viewBox="0 0 40 40"` for an emblem mark or `viewBox="0 0 220 44"` for a horizontal lockup).
   - Convert all custom lettering to outlined vector `<path>` shapes so rendering never depends on external web fonts.
   - Color shapes using standard SVG presentation attributes (`fill="#..."`, `stroke="#..."`, `stroke-width="..."`)—**never** inline `<style>` tags, `style="..."` attributes, `<script>`, or external `http://` / `https://` image or font URLs (`scripts/build.py` validates every configured `.svg` at build time under CivicDesk's `default-src 'none'; style-src 'self'; img-src 'self'` policy).
2. **Light & Dark Theme Variants (`>= 3.0:1` Non-Text / `>= 7.0:1` Lettering Contrast):**
   - **Primary light-paper logo (`static/brand/logo-light.svg`):** Designed for Plain white paper (`#FFFFFF`), Warm paper (`#F7F5F0`), High contrast (`#FFFFFF`), and print.
   - **Optional dark-slate logo (`static/brand/logo-dark.svg`):** Designed for Dark slate (`#111827`) and automatic OS dark mode (`prefers-color-scheme: dark`). CivicDesk toggles between `.brand-logo-light` and `.brand-logo-dark` purely via CSS rules in `static/css/site.css`.
3. **Favicon, Touch Icon & Social Share Card:**
   - `favicon.svg` (clean square vector icon, `viewBox="0 0 32 32"` or `0 0 40 40`, legible at `16x16` and `32x32`).
   - `apple-touch-icon.png` (`180x180` opaque PNG on dark slate or solid brand background).
   - `og-card.png` (`1200x630` PNG social preview card).

### 3-Step Zero-Code Swap Checklist

1. **Drop artwork files into `static/brand/`:** Place the artist's files under `static/brand/` (for example `static/brand/logo-light.svg`, `static/brand/logo-dark.svg`, `static/brand/favicon.svg`, `static/brand/apple-touch-icon.png`, and `static/brand/og-card.png`).
2. **Update `brand:` in `data/site.yaml`:**
   - Set `custom_artwork: true` (tells `scripts/make_mark.py --check` to validate your custom files rather than enforcing checksums on the default 5-stripe mark).
   - Choose `mode: "mark_and_wordmark"` (if the artwork is a standalone emblem beside HTML `"CivicDesk"` text) or `mode: "lockup_only"` (if the artwork already includes `"CivicDesk"` lettering; HTML `"CivicDesk"` moves automatically into `.sr-only` so screen readers still announce the home link cleanly without visual duplication).
   - Point `logo`, `logo_dark`, `favicon_svg`, `apple_touch_icon`, `og_image`, and `og_image_alt` to your paths relative to `static/` (for example `"brand/logo-light.svg"`).
3. **Verify locally and open a PR:** Run `python3 scripts/make_mark.py --check && python3 scripts/build.py && pytest` to verify XML/CSP safety, cache-busted asset URLs, and 4-theme accessibility before opening a pull request.
