# Civic Horizon Visual System (`docs/DESIGN.md`)

CivicDesk's visual system translates public-interest information design (GOV.UK, Stripe Docs, NYT Upshot) into a high-trust, typographic, neurodivergent-friendly, WCAG 2.2 AAA public-service interface. Zero decorative clip-art; visuals are used only to show order, time, proportion, branching choices, or realistic notice structure; every fact is stated once without duplicating graphic content in adjacent prose.

---

## 1. Core Design Principles ("Earn Every Visual, Say It Once")

1. **Purposeful Information Design Over Widget Quotas:** Visual components appear only where spatial layout teaches a timeline, sequential agency dependency, proportional breakdown, multi-option matrix, or realistic state notice layout faster than prose. Trust and utility pages (`/privacy/`, `/about/`, `/accessibility/`, `/404.html`) stay clean, direct, and typographic without filler graphics.
2. **Say It Once (Zero Visual-Prose Duplication):** Whenever a key-facts strip, dependency flow, comparison matrix, or sample notice presents concrete fees, deadlines, or channels, adjacent prose never repeats those same figures or lists.
3. **Concise Node Copy & Sleek Jump Pills:** Dependency cards (`.viz-flow-card`) and timeline milestones (`.viz-timeline-item`) pair numbered typographic counters (`aria-hidden="true"` inside `<ol>`) with a concise title and `<= 120` characters of body text—zero cartoon medallions or distorted state-map blobs. Page tables of contents render as compact horizontal **Jump Pill Strips** (`.toc-pills`).
4. **Quiet Inclusion & Strict Self-Hosted CSP (`default-src 'none'`):** All layout proportions, colors, notice mockups, and proportional meters use external CSS classes in `static/css/site.css` (`style-src 'self'`). Inline `style="..."` attributes and `<style>` blocks are forbidden across templates, SVGs, and JavaScript.

---

## 2. Typography & Scannable Rhythm

- **Maximum 2 Font Families:**
  - Default: system sans-serif stack (`system-ui, -apple-system, "Segoe UI", Roboto, ...`) for zero-latency rendering.
  - Optional reader preference (`html[data-font="hyper"]`): self-hosted upright **Atkinson Hyperlegible Next** (`400` and `700` WOFF2, SHA-256 verified at build time) with `font-synthesis: none` and upright `em`/`i` rules.
- **Left-Aligned Sentence Case & Measure:** Every heading, navigation item, kicker, badge, and visual label uses left-aligned sentence case per UK Home Office and GOV.UK standards, with `<= 65ch` prose measure.
- **Tabular Numerals:** Key-fact figures (`1.6rem–2.1rem`), dates, hour counts (`80 hrs`), dollar thresholds (`$580`, `$124`), and step numbers use `font-variant-numeric: tabular-nums`.

---

## 3. Color & Semantic `--viz-*` Tokens (WCAG 2.2 AAA)

All four reading themes (`light`, `warm`, `dark` + `prefers-color-scheme: dark`, and `contrast` + `prefers-contrast: more`) preserve CivicDesk's locked **Civic Horizon** anchors (`#1D70B8` sky, `#FFFFFF` paper, `#C85A70` muted rose, `#111827` slate ink) and verify every `--viz-*` token in `tests/test_contrast_and_radar.py`:

| Token | Role | Contrast Requirement |
|---|---|---|
| `--viz-surface` / `--viz-surface-2` | Tinted diagram & stage containers | `>= 7.0:1` with `--text`, `--heading`, and `--link` (WCAG 1.4.6 AAA) |
| `--viz-fill-1` / `--viz-fill-2` / `--viz-fill-3` | Subtle accent washes (Sky / Rose / Sage) | `>= 7.0:1` with `--text`, `--heading`, and `--link` |
| `--viz-paper` / `--viz-shadow` | Realistic notice sheet surface & crisp offset shadow | `>= 7.0:1` text contrast on `--viz-paper` |
| `--viz-border` | Structural card, notice, and divider stroke | `>= 3.0:1` against `--bg`, `--surface`, and `--viz-surface` (WCAG 1.4.11) |
| `--viz-accent` / `--viz-accent-2` / `--viz-accent-3` | Primary/secondary/tertiary graphical strokes & meter fills | `>= 3.0:1` non-text contrast |
| `--viz-node-bg` / `--viz-node-text` | Step counter and active progress node badge | `>= 7.0:1` text-on-fill contrast |

Inside `@media print`, all `--viz-*` tokens reset to crisp `#000000` ink on `#FFFFFF` paper.

---

## 4. Purposeful Information Design & Cohesive Editorial Art (`templates/visuals.html`)

- **Readable Labels in Semantic HTML:** All labels, dates, fees, and callouts live in semantic HTML (`<ol>`, `<ul>`, `<dl>`, `<table>`, `<h3>`, `<p>`) inside CSS Grid/Flex layouts so **Extra large** text (`150%`), **Wide spacing** (WCAG 1.4.12), and `320px` viewports (WCAG 1.4.10) reflow cleanly without horizontal scrollbars or clipped words.
- **Cohesive Self-Hosted Editorial Spot Illustrations (`static/img/art/*.webp` & `editorial_art`):**
  - Six cohesive vector-editorial `.webp` spot illustrations (`desk-overview.webp`, `medicaid-calendar.webp`, `id-sequence.webp`, `medicaid-exemptions.webp`, `official-notice.webp`, `community-help.webp`) sit directly on Light and Warm paper surfaces, receive a crisp `#F1F5F9` soft-lit frame in Dark slate and High contrast so `#111827` linework stays high-contrast, and hide automatically in `@media print` (with secondary spot art hidden in `data-brief` Short version mode).
- **Purposeful Macro Suite (`templates/visuals.html` & `templates/icons.html`):**
  1. `stat_hero_strip`: Architectural hairline key-facts strip (`tabular-nums`) for concrete fees, ages, deadlines, and verified counts.
  2. `process_flow`: Sequential numbered dependency cards (`<= 120` chars per step) with directional arrows where order genuinely matters (`Court -> SSA -> DMV -> Passport`, court filing steps, post-application checks).
  3. `timeline_strip` & `swimlane_timeline`: Graphical `4px` colored timeline rails with `36x36` circular icon/step nodes (`.viz-rail-node`, horizontal connected track on desktop, vertical left-spine track on mobile) for 2027 effective dates, appeal & aid-paid-pending windows (`denied.html`), and 90-day retroactive bill windows (`bills.html`).
  4. `hours_breakdown`, `calendar_80hr_grid`, & `.viz-compare-board`: `136x136` SVG Donut Ring Chart (`80h / mo`) paired with a chunky `36px` rounded horizontal stacked bar + color-keyed legend pills and concrete 4-week sample mix (`rules.html`, `log.html`), plus `viewBox="0 0 320 22"` proportional horizontal court-fee comparison bars alongside `$0 with approved fee waiver` badges (`id-records/compare.html`).
  5. `passport_form_matrix` & `yes_no_split_board`: `DS-5504` vs. `DS-82` vs. `DS-11` decision table (`id-records/federal.html`) and 2-column administrative vs. action/court eligibility comparisons (`*-birth.html`, `reasons.html`, `health.html`).
  6. `doc_mockup_letter` & `glossary_list`: Realistic typographic state notice sheet with numbered markers `1`–`4` paired with `<ol class="mockup-legend">` on `medicaid/*/letter.html`, plus 2-column typographic `<dl class="glossary-grid">` tables on glossary and directory pages (`words.html`, `id-records/glossary.html`, `help.html`). Small `20–24px` semantic UI icons in `templates/icons.html` are used sparingly on buttons, timeline nodes, callout kickers, and card links.

---

## 5. Motion, Progressive Disclosure & Print

- **Restrained Transitions:** Interactive cards, pills, and choice rows use `160ms cubic-bezier(0.22, 1, 0.36, 1)` scoped to `border-color`, `background-color`, `color`, and `box-shadow`, disabled under `@media (prefers-reduced-motion: reduce)`.
- **Progressive Disclosure (`<details class="deeper">` & `data-brief`):** Dense edge cases fold into accessible `<details class="deeper">` panels while preserving all heading/section `id`s and primary citations; `@media print` expands `<details>` automatically and keeps visual tables and notice sheets intact (`break-inside: avoid`).

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
