# Civic Horizon Visual System (`docs/DESIGN.md`)

CivicDesk's visual system translates modern frontend craft principles into a high-trust, neurodivergent-friendly, WCAG 2.2 AAA public-service interface. Every visual element exists to lower cognitive load when navigating complex government rules, forms, fees, and agency dependencies.

---

## 1. Core Design Principles

1. **Function-Driven Civic Clarity:** Visuals orient the reader immediately—showing *which office goes first*, *which date is a hard cutoff*, or *how 80 monthly hours add up*—never decorative marketing fluff.
2. **Quiet Inclusion & Calm Authority:** Avoid alarmist red banners, pulsing status dots ("biscuit pills"), gradient headline keywords, purple-on-dark accents, grid-wallpaper backgrounds, or deeply nested card containers.
3. **Strict Self-Hosted CSP (`default-src 'none'`):** All layout proportions, colors, and SVG states are controlled via external CSS classes in `static/css/site.css`. Inline `style="..."` attributes and `<style>` blocks are forbidden across templates, SVGs, and JavaScript.

---

## 2. Typography & Scannable Rhythm

- **Maximum 2 Font Families:**
  - Default: system sans-serif stack (`system-ui, -apple-system, "Segoe UI", Roboto, ...`) for zero-latency rendering.
  - Optional reader preference (`html[data-font="hyper"]`): self-hosted upright **Atkinson Hyperlegible Next** (`400` and `700` WOFF2, SHA-256 verified at build time) with `font-synthesis: none` and upright `em`/`i` rules.
- **Left-Aligned Sentence Case:** Every heading, navigation item, kicker, badge, and visual label uses left-aligned sentence case per UK Home Office and GOV.UK standards.
- **Readable Measure & Floor Size:** Body copy and callout text stay within a comfortable `<= 65ch` measure inside `.wrap` (`max-width: 54rem`). No visible text drops below `13px` (`0.75rem` at the `18px` base root size).
- **Tabular Numerals Without Monospace Noise:** Dates, hour counts (`80 hours`), dollar thresholds (`$580`, `$124`), and step numbers use `font-variant-numeric: tabular-nums` in the primary sans-serif face rather than decorative monospace kickers.

---

## 3. Color & Semantic `--viz-*` Tokens (WCAG 2.2 AAA)

All four reading themes (`light`, `warm`, `dark` + `prefers-color-scheme: dark`, and `contrast` + `prefers-contrast: more`) preserve CivicDesk's locked **Civic Horizon** anchors:
- Sky: `#1D70B8`
- Paper: `#FFFFFF`
- Muted Rose: `#C85A70`
- Slate Ink: `#111827`

Every visual diagram and emblem uses theme-reactive `--viz-*` custom properties verified in `tests/test_contrast_and_radar.py`:

| Token | Role | Contrast Requirement |
|---|---|---|
| `--viz-surface` | Cool sky-tinted diagram / stage container | `>= 7.0:1` with `--text`, `--heading`, and `--link` (WCAG 1.4.6 AAA) |
| `--viz-surface-2` | Warm rose-tinted secondary stage container | `>= 7.0:1` with `--text`, `--heading`, and `--link` (WCAG 1.4.6 AAA) |
| `--viz-border` | Structural card, node, and meter track stroke | `>= 3.0:1` against `--bg`, `--surface`, and `--viz-surface` (WCAG 1.4.11) |
| `--viz-accent` | Primary graphical stroke / meter segment (Sky) | `>= 3.0:1` against `--bg`, `--surface`, and `--viz-surface` |
| `--viz-accent-2` | Secondary graphical stroke / deadline accent (Rose) | `>= 3.0:1` against `--bg`, `--surface`, and `--viz-surface-2` |
| `--viz-accent-3` | Tertiary graphical stroke / community segment (Teal) | `>= 3.0:1` against `--bg`, `--surface`, and `--viz-surface` |
| `--viz-node-bg` / `--viz-node-text` | Step counter and active progress node badge | `>= 7.0:1` text-on-fill contrast |

Inside `@media print`, all `--viz-*` tokens reset to crisp `#000000` ink on `#FFFFFF` paper.

---

## 4. Hybrid Semantic HTML + Inline SVG Diagrams

- **Never Bake Prose Into `<svg><text>`:** Multi-word labels, dates, fees, and explanations live in semantic HTML (`<ol>`, `<ul>`, `<dl>`, `<h3>`, `<p>`) styled with CSS Grid and Flexbox so **Extra large** text (`150%`), **Wide spacing** (WCAG 1.4.12), and `320px` viewports (WCAG 1.4.10) reflow into clean vertical stacks without clipping or overlapping.
- **SVG Accessibility Contract:** Decorative icons, connectors, horizon linework, and proportional bars carry `aria-hidden="true" focusable="false"`. Any standalone informational `<svg>` without adjacent HTML text equivalents must carry `role="img"` and `aria-label` or `aria-labelledby`.
- **2px Geometric Icon Grid (`templates/icons.html`):** Icons use a unified `viewBox="0 0 24 24"` grid (`stroke-width="1.85"`, `stroke-linecap="round"`, `stroke-linejoin="round"`, `fill="none"`, `stroke="currentColor"`), scaling cleanly to `20px`, `24px`, or `32px` emblem badges.
- **Non-Color-Only Differentiation:** Timeline phases, meter segments, and checker outcome headers pair distinct iconography, borders (solid vs. dashed/accented), and explicit text kickers so meaning never relies on color alone (WCAG 1.4.1).

---

## 5. Motion, Progressive Disclosure & Print

- **Restrained Transitions:** Interactive cards, buttons, and choice rows use `160ms cubic-bezier(0.22, 1, 0.36, 1)` scoped strictly to `border-color`, `background-color`, `color`, and `box-shadow`—never `transition: all`. `@media (prefers-reduced-motion: reduce)` disables all transitions and animations.
- **Short Version (`data-brief="on"` / `data-brief="true"`):** Primary orientation strips (`.viz-timeline`, core agency `.viz-flow`) stay compact and visible; secondary explanatory diagrams carry `data-deeper` (or `.more` / `.brief-extra`) so Short Version hides background detail cleanly.
- **Print Readiness (`@media print`):** Visual containers enforce `break-inside: avoid`, hide interactive question forms once a personalized result sheet is visible, and render clean monochrome borders and icons.
