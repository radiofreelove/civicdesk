# CivicDesk

Plain-language guides to government rules, benefits, and official records, backed by dated primary sources — [civicdesk.net](https://civicdesk.net).

CivicDesk is an independent public-interest project. It is **not** run by any court or government agency and does **not** provide legal advice.

## Active Desks

- **2027 Medicaid Work and Activity Rules (`/medicaid/`)** — Covers Oregon Health Plan (OHP), Washington Apple Health, and Utah Medicaid.
- **Legal Name, ID, and Vital Records (`/id-records/`)** — Covers Oregon, Washington, Utah, and federal agencies.

## Core Guarantees

- **Dated primary sources:** Every fact carries a dated primary source citation.
- **Private on shared computers:** Zero cookies, zero analytics, and `Referrer-Policy: no-referrer`.
- **Inclusive communication clarity:** Follows UK Home Office and GOV.UK inclusive communication standards.

## Directory Layout

- `.agents/skills/` — Agent skill definitions
- `.github/workflows/` — CI, checks, and Policy Radar workflows
- `data/sources/` — Desk-partitioned primary source registries
- `docs/` — Project documentation and architecture decisions
- `scripts/` — Build and verification scripts
- `site-root/` — Static root configuration (`_headers`, `_redirects`)
- `static/` — Static assets (CSS, SVG, fonts)
- `templates/` — Jinja2 HTML templates
- `tests/` — Automated test suite

## Build and Test Commands (Python 3.13)

```bash
python3 scripts/build.py
python3 scripts/check_brand_purge.py
pytest
```

## Reporting Corrections

To report a factual error or outdated rule, email `corrections at civicdesk.net` or open a public issue. **Never share personal case details.**

## Licensing

- **Writing and guides:** CC BY 4.0
- **Code and scripts:** MIT License
