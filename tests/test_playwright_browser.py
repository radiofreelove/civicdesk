from __future__ import annotations

from pathlib import Path
import time
from playwright.sync_api import Error as PlaywrightError
import yaml

ROOT = Path(__file__).resolve().parent.parent

SPACING_CSS = (
    "* { line-height: 1.5 !important; letter-spacing: 0.12em !important; "
    "word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }"
)


def goto_local(page, url: str):
    try:
        return page.goto(url, wait_until="load")
    except PlaywrightError as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
        time.sleep(0.05)
        return page.goto(url, wait_until="load")


def test_all_pages_clean_load_and_reflow(site_server: str, browser_instance) -> None:
    pages_yaml = yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))
    errors: list[str] = []
    outside_requests: list[str] = []
    csp_violations: list[str] = []

    def open_ctx_and_page(width: int = 1280, height: int = 900):
        c = browser_instance.new_context(viewport={"width": width, "height": height})
        c.add_init_script(
            "window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));"
        )
        p = c.new_page()
        p.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        p.on("pageerror", lambda exc: errors.append(str(exc)))
        p.on("request", lambda req: outside_requests.append(req.url) if not req.url.startswith(site_server) else None)
        return c, p

    ctx, page = open_ctx_and_page()

    for idx, entry in enumerate(pages_yaml):
        if idx and idx % 15 == 0:
            ctx.close()
            ctx, page = open_ctx_and_page()
        route = entry["route"]
        errors.clear()
        outside_requests.clear()
        resp = goto_local(page, site_server + route)
        assert resp is not None and resp.status == 200, f"{route}: HTTP {resp and resp.status}"
        assert not errors, f"{route}: console/page errors: {errors}"
        assert not outside_requests, f"{route}: unexpected external requests: {outside_requests}"
        csp_violations = page.evaluate("() => window.__csp || []")
        assert not csp_violations, f"{route}: CSP violations: {csp_violations}"

    ctx.close()

    # Check 200% zoom (640px) and 400% zoom (320px) reflow + >=44px touch targets on key pages across all desks
    key_routes = [
        "/",
        "/start/",
        "/medicaid/",
        "/medicaid/oregon/",
        "/medicaid/oregon/check/",
        "/medicaid/washington/",
        "/medicaid/utah/",
        "/medicaid/utah/check/",
        "/id-records/",
        "/id-records/path/",
        "/id-records/oregon/",
        "/id-records/oregon/court/",
        "/id-records/compare/",
        "/sources/",
    ]
    ctx, page = open_ctx_and_page(640, 800)
    for route in key_routes:
        goto_local(page, site_server + route)
        for width in (640, 320):
            page.set_viewport_size({"width": width, "height": 800})
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 1, f"{route}: horizontal overflow of {overflow}px at {width}px viewport"

        # Check touch target sizes (>= 44px height on standalone interactive controls, >= 24x24px everywhere)
        small = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('button, summary, .site-nav a, .footer-links a, .breadcrumb a, .build-line a, .btn, .btn-secondary, .exit-link, .safety-exit-link, .choice').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return;
                if (getComputedStyle(el).visibility === 'hidden') return;
                if (el.closest('dialog:not([open])') || el.closest('[hidden]')) return;
                if (r.height < 43 || r.width < 43) {
                    out.push(el.tagName + ':' + (el.textContent || '').trim().slice(0, 25) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
                }
            });
            return out;
        }""")
        assert not small, f"{route}: interactive controls under 44x44px: {small}"
    ctx.close()

    spacing_ctx = browser_instance.new_context(viewport={"width": 320, "height": 800}, bypass_csp=True)
    spacing_page = spacing_ctx.new_page()
    for route in key_routes:
        goto_local(spacing_page, site_server + route)
        spacing_page.add_style_tag(content=SPACING_CSS)
        sp_overflow = spacing_page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        assert sp_overflow <= 1, f"{route}: horizontal overflow of {sp_overflow}px with WCAG text spacing at 320px"
    spacing_ctx.close()


def test_start_router_and_medicaid_checks(site_server: str, browser_instance) -> None:
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    # 1. Test /start/ 2-question router
    goto_local(page, site_server + "/start/")
    page.click("#start-form button[type=submit]")
    assert page.is_visible("#start-error")
    page.check('input[name="state"][value="oregon"]')
    page.check('input[name="topic"][value="medicaid"]')
    page.click("#start-form button[type=submit]")
    assert page.is_visible("#start-result")
    assert 'href="/medicaid/oregon/"' in page.inner_html("#start-result-body")
    assert 'href="/medicaid/oregon/check/"' in page.inner_html("#start-result-body")

    # 2. Test Washington 1-minute check (including the fixed 6th 'work' option!)
    goto_local(page, site_server + "/medicaid/washington/check/")
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="none"]')
    page.click('[data-step="2"] button[data-next]')
    assert page.locator('input[name="meet"][value="work"]').count() == 1
    page.check('input[name="meet"][value="work"]')
    page.click('#checker button[type="submit"]')
    assert page.is_visible("#checker-result")
    assert "Washington's rules list this as a way to meet the requirement." in page.inner_text("#checker-result")
    assert "What HCA asks people in this group to do" in page.inner_text("#checker-result")
    assert "You work at least 80 hours a month." in page.inner_text("#checker-result")

    # 3. Test Oregon 1-minute check under-19 short circuit
    goto_local(page, site_server + "/medicaid/oregon/check/")
    page.check('input[name="age"][value="under19"]')
    page.click('[data-step="1"] button[data-next]')
    assert page.is_visible("#checker-result")
    assert "do not apply to your age group" in page.inner_text("#checker-result")

    # 4. Test Utah 1-minute check categories (MED-U05) and non-table treatment tag
    goto_local(page, site_server + "/medicaid/utah/check/")
    assert "Exemptions" not in page.inner_text("#checker-steps-track")
    assert "does not ask about your county" not in page.inner_text("main")
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="pregnant"]')
    page.click('[data-step="2"] button[data-next]')
    page.check('input[name="meet"][value="none"]')
    page.click('#checker button[type="submit"]')
    assert page.is_visible("#checker-result")
    utah_result = page.inner_text("#checker-result")
    # Rule-indexed wording in Utah's own categories (MED-U05), never "applies to you".
    assert "Utah's rules list this reason as an exclusion." in utah_result
    assert "Utah's table: excluded individuals." in utah_result
    assert "What DWS asks people in this group to do" in utah_result
    assert "apply to you" not in utah_result and "What to do" not in utah_result
    assert "exemption" not in utah_result.lower()

    # Mixed categories: a mandatory exception and a requested short-term hardship.
    page.click("#checker-result button:has-text('Change my answers')")
    page.click('[data-step="3"] button[data-back]')
    page.uncheck('input[name="reason"][value="pregnant"]')
    page.check('input[name="reason"][value="medicare"]')
    page.check('input[name="reason"][value="hospital"]')
    page.click('[data-step="2"] button[data-next]')
    page.click('#checker button[type="submit"]')
    mixed = page.inner_text("#checker-result")
    assert "Utah's rules list these reasons as mandatory exceptions or optional short-term hardships." in mixed
    assert "Utah's table: mandatory exceptions." in mixed and "Utah's table: optional short-term hardships." in mixed
    assert "You must ask the state for it." in mixed

    # Treatment is not on MED-U05, so its tag must cite Utah Medicaid pages/federal rule, not "Utah's table".
    page.click("#checker-result button:has-text('Change my answers')")
    page.click('[data-step="3"] button[data-back]')
    page.uncheck('input[name="reason"][value="medicare"]')
    page.uncheck('input[name="reason"][value="hospital"]')
    page.check('input[name="reason"][value="treatment"]')
    page.click('[data-step="2"] button[data-next]')
    page.click('#checker button[type="submit"]')
    treat = page.inner_text("#checker-result")
    assert "Utah's rules list this reason as an exclusion." in treat
    assert "Utah Medicaid pages and federal rule: excluded individuals." in treat
    assert "Utah's table:" not in treat

    ctx.close()


def test_utah_check_reasons_match_utah_table(site_server: str, browser_instance) -> None:
    # Every Utah checkbox has a reason entry with one of Utah's three table categories (MED-U05)
    # and registry sources that are also listed in the page's Sources panel; jail covers people
    # still in jail or prison; county hardships are listed.
    registry = {s["id"] for s in yaml.safe_load((ROOT / "data" / "sources" / "medicaid.yaml").read_text(encoding="utf-8"))}
    pages = {p["slug"]: p for p in yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))}
    page_sources = set(pages["medicaid-utah-check"].get("sources") or [])
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    goto_local(page, site_server + "/medicaid/utah/check/")
    reasons = page.evaluate("""() => Object.fromEntries(Object.entries(window.CivicDeskCheck.reasons)
        .map(([k, v]) => [k, { kind: v.kind, basis: v.basis || [], label: v.label, tag: v.tag || "" }]))""")
    boxes = set(page.eval_on_selector_all('input[name="reason"]', "els => els.map(e => e.value)")) - {"none"}
    assert boxes == set(reasons), f"checkboxes {sorted(boxes ^ set(reasons))} do not match the reason table"
    expected = {
        "exclusion": {"pregnant", "care", "health", "treatment", "snap", "aian", "foster", "veteran"},
        "exception": {"medicare", "ssi", "jail"},
        "hardship": {"hospital", "travel", "county"},
    }
    for kind, keys in expected.items():
        assert {k for k, v in reasons.items() if v["kind"] == kind} == keys, kind
    for key, v in reasons.items():
        assert v["basis"], f"{key}: no basis sources"
        for sid in v["basis"]:
            assert sid in registry, f"{key}: basis {sid} is not in data/sources/medicaid.yaml"
            assert sid in page_sources, f"{key}: basis {sid} is missing from medicaid-utah-check sources in data/pages.yaml"
    # Treatment is not on Utah's table, so it must not rest on MED-U05 or claim "Utah's table".
    assert "MED-U05" not in reasons["treatment"]["basis"]
    assert reasons["treatment"]["tag"] and "Utah's table" not in reasons["treatment"]["tag"]
    assert reasons["jail"]["label"].startswith("You are in jail or prison")
    ctx.close()


def test_washington_and_oregon_check_reasons_match_state_lists(site_server: str, browser_instance) -> None:
    # Every Washington and Oregon checkbox has a reason entry with its state's published category
    # and MED-* basis IDs declared in that page's Sources panel; mixed results render rule-indexed
    # category titles and per-item tags.
    registry = {s["id"] for s in yaml.safe_load((ROOT / "data" / "sources" / "medicaid.yaml").read_text(encoding="utf-8"))}
    pages = {p["slug"]: p for p in yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))}
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    # Washington (HCA categories: excluded, exemption, hardship)
    goto_local(page, site_server + "/medicaid/washington/check/")
    assert "Exemptions & hardships" in page.inner_text("#checker-steps-track")
    assert "does not ask about your county" not in page.inner_text("main")
    wa_reasons = page.evaluate("""() => Object.fromEntries(Object.entries(window.CivicDeskCheck.reasons)
        .map(([k, v]) => [k, { kind: v.kind, basis: v.basis || [], label: v.label }]))""")
    wa_boxes = set(page.eval_on_selector_all('input[name="reason"]', "els => els.map(e => e.value)")) - {"none"}
    assert wa_boxes == set(wa_reasons), f"WA checkboxes {sorted(wa_boxes ^ set(wa_reasons))} mismatch"
    wa_expected = {
        "excluded": {"medicare"},
        "exemption": {"pregnant", "care", "health", "treatment", "snap", "aian", "foster", "veteran", "jail"},
        "hardship": {"hospital", "travel", "county"},
    }
    for kind, keys in wa_expected.items():
        assert {k for k, v in wa_reasons.items() if v["kind"] == kind} == keys, f"WA {kind}"
    wa_sources = set(pages["medicaid-washington-check"].get("sources") or [])
    for key, v in wa_reasons.items():
        assert v["basis"], f"WA {key}: no basis"
        for sid in v["basis"]:
            assert sid in registry and sid in wa_sources, f"WA {key}: basis {sid} missing from registry or page sources"
    assert wa_reasons["jail"]["label"].startswith("You are in jail or prison")

    # Exercise WA mixed selection: exemption + short-term hardship
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="pregnant"]')
    page.check('input[name="reason"][value="hospital"]')
    page.click('[data-step="2"] button[data-next]')
    page.check('input[name="meet"][value="none"]')
    page.click('#checker button[type="submit"]')
    wa_mixed = page.inner_text("#checker-result")
    assert "Washington's rules list these reasons as exemptions or short-term hardships." in wa_mixed
    assert "HCA's list: exemption." in wa_mixed and "HCA's list: short-term hardship." in wa_mixed
    assert "You must ask HCA to grant this short-term hardship." in wa_mixed

    # Oregon (OHA categories: excluded, auto, self_report)
    goto_local(page, site_server + "/medicaid/oregon/check/")
    assert "Exemptions & exclusions" in page.inner_text("#checker-steps-track")
    or_reasons = page.evaluate("""() => Object.fromEntries(Object.entries(window.CivicDeskCheck.reasons)
        .map(([k, v]) => [k, { kind: v.kind, basis: v.basis || [], label: v.label }]))""")
    or_boxes = set(page.eval_on_selector_all('input[name="reason"]', "els => els.map(e => e.value)")) - {"none"}
    assert or_boxes == set(or_reasons), f"OR checkboxes {sorted(or_boxes ^ set(or_reasons))} mismatch"
    or_expected = {
        "excluded": {"medicare", "disability_ohp"},
        "auto": {"snap", "tanf", "foster", "county"},
        "self_report": {"pregnant", "child", "caregiver", "health", "ssi", "treatment", "aian", "veteran", "jail", "hospital", "travel"},
    }
    for kind, keys in or_expected.items():
        assert {k for k, v in or_reasons.items() if v["kind"] == kind} == keys, f"OR {kind}"
    or_sources = set(pages["medicaid-oregon-check"].get("sources") or [])
    for key, v in or_reasons.items():
        assert v["basis"], f"OR {key}: no basis"
        for sid in v["basis"]:
            assert sid in registry and sid in or_sources, f"OR {key}: basis {sid} missing from registry or page sources"

    # Exercise OR mixed selection: auto + self_report
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="county"]')
    page.check('input[name="reason"][value="health"]')
    page.click('[data-step="2"] button[data-next]')
    page.check('input[name="meet"][value="none"]')
    page.click('#checker button[type="submit"]')
    or_mixed = page.inner_text("#checker-result")
    assert "Oregon's rules list these reasons as exemptions OHP checks automatically or exemptions you report to OHP." in or_mixed
    assert "OHP list: exemption checked automatically by OHP." in or_mixed
    assert "OHP list: exemption you report to OHP." in or_mixed
    ctx.close()


def test_broadened_id_records_path_finder(site_server: str, browser_instance) -> None:
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    goto_local(page, site_server + "/id-records/path/")

    # 1. Marriage name change in Oregon WITHOUT answering optional birth-certificate question
    page.check('input[name="live"][value="or"]')
    page.check('input[name="change"][value="marriage"]')
    assert page.is_hidden("#q-who")
    page.click('#path-form button[type="submit"]')
    assert page.is_visible("#path-results")
    marriage_text = page.inner_text("#results-body")
    assert "Marriage or domestic partnership certificate in Oregon" in marriage_text
    assert "Social Security" in marriage_text
    assert "Birth certificate:" not in marriage_text

    # 2. Divorce name restoration in Washington
    page.click('#path-form button[type="reset"]')
    page.check('input[name="live"][value="wa"]')
    page.check('input[name="change"][value="divorce"]')
    page.click('#path-form button[type="submit"]')
    divorce_text = page.inner_text("#results-body")
    assert "Divorce or dissolution decree in Washington" in divorce_text
    assert "Form DS-5504" in divorce_text

    # 3. Sex/gender marker update in Washington with optional birth certificate skipped -> no court step
    page.click('#path-form button[type="reset"]')
    page.check('input[name="live"][value="wa"]')
    page.check('input[name="change"][value="marker"]')
    page.check('input[name="who"][value="adult"]')
    page.check('input[name="born"][value="skip"]')
    page.click('#path-form button[type="submit"]')
    marker_text = page.inner_text("#results-body")
    assert "Court in Washington" not in marker_text
    assert "do not require a court order for state ID or in-state birth-certificate sex-marker updates" in page.inner_text("#results-summary")
    assert "This path finder is general legal information, not legal advice." in page.inner_text("#path-results")
    # Sort, not hide: each sorted step names its rule, and the court route the answers did not
    # select stays on the page in the closed Other published pathways drawer.
    basis_lines = page.locator("#results-body .basis-line").all_inner_texts()
    assert basis_lines and all(line.startswith("Sorted by: ") for line in basis_lines), basis_lines
    assert page.is_visible("#other-pathways")
    assert page.inner_text("#other-pathways > summary") == "Other published pathways in Washington"
    drawer = page.evaluate("""() => {
        const d = document.getElementById('other-pathways');
        const s = d.querySelector(':scope > summary');
        return {
            open: d.open,
            text: document.getElementById('other-pathways-body').textContent,
            leftGap: Math.abs(s.getBoundingClientRect().left - d.getBoundingClientRect().left),
        };
    }""")
    assert drawer["open"] is False
    assert drawer["leftGap"] <= 2, f"drawer summary indented away from its container: {drawer['leftGap']}px"
    assert "Court in Washington" in drawer["text"] and "RCW 4.24.130" in drawer["text"]
    assert "After your core IDs" not in drawer["text"], "t-after wording swap must not duplicate into Other pathways"
    # Sorted-by names the rule behind the lines shown: a marker-only update cites SSA's sex-field
    # rule for the Social Security step, not the name-change page.
    assert "POMS RM 10212.200" in basis_lines[0] and "change your name" not in basis_lines[0], basis_lines[0]
    # Printing keeps a closed drawer closed and out of the printout; site.js must not reopen it.
    page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
    assert page.evaluate("() => document.getElementById('other-pathways').open") is False
    page.emulate_media(media="print")
    assert page.evaluate("() => getComputedStyle(document.getElementById('other-pathways')).display") == "none"
    assert page.evaluate("() => getComputedStyle(document.querySelector('.tool-notice')).display") != "none"
    page.emulate_media(media="screen")
    page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")
    # A drawer the reader opened prints as shown.
    page.click("#other-pathways > summary")
    page.emulate_media(media="print")
    assert page.evaluate("() => getComputedStyle(document.getElementById('other-pathways')).display") != "none"
    page.emulate_media(media="screen")

    # 4. Both court name + sex change in Oregon with Washington birth certificate
    page.click('#path-form button[type="reset"]')
    page.check('input[name="live"][value="or"]')
    page.check('input[name="change"][value="both"]')
    page.check('input[name="who"][value="adult"]')
    page.check('input[name="born"][value="wa"]')
    page.click('#path-form button[type="submit"]')
    both_text = page.inner_text("#results-body")
    assert "any Oregon circuit court" in both_text
    assert "DOH 422-143" in both_text
    or_basis = page.locator("#results-body .basis-line").first.inner_text()
    assert "2026 Oregon Laws ch. 52" in or_basis, or_basis
    # Lines for other situations (a parent or a minor filing) move to the drawer, not away.
    other_text = page.evaluate("() => document.getElementById('other-pathways-body').textContent")
    assert "Marriage or domestic partnership certificate in Oregon" in other_text
    assert "As a parent or guardian, you file the minor forms." in other_text
    assert "As a parent or guardian, you file the minor forms." not in both_text
    assert "After your core IDs" not in other_text

    ctx.close()


def test_scoped_leave_quickly_and_reading_settings(site_server: str, browser_instance) -> None:
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    ctx.route(
        "https://www.weather.gov/**",
        lambda r: r.fulfill(status=200, body="<!doctype html><title>Weather</title><p>weather</p>", content_type="text/html"),
    )
    page = ctx.new_page()

    # 1. Triple-Shift MUST NOT trigger exit on / or /medicaid/
    for non_exit_route in ("/", "/medicaid/"):
        goto_local(page, site_server + non_exit_route)
        assert page.locator("[data-quick-exit]").count() == 0
        for _ in range(3):
            page.keyboard.press("Shift")
        page.wait_for_timeout(150)
        assert "weather.gov" not in page.url

    # 2. Leave quickly click AND triple-Shift work on /id-records/* (and Shift+letter does NOT false-trigger)
    goto_local(page, site_server + "/id-records/oregon/")
    page.locator("body").click(position={"x": 10, "y": 250})
    for ch in ("A", "B", "C"):
        page.keyboard.down("Shift")
        page.keyboard.press(ch)
        page.keyboard.up("Shift")
    assert "weather.gov" not in page.url

    for _ in range(3):
        page.keyboard.press("Shift")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    goto_local(page, site_server + "/id-records/")
    page.click("#quick-exit")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    # 3. Scoped domestic-safety callout Leave quickly works on /medicaid/oregon/reasons/
    goto_local(page, site_server + "/medicaid/oregon/reasons/")
    assert page.locator(".exit-bar").count() == 0
    page.click(".safety-exit-link[data-quick-exit]")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    # 3b. Leave quickly clears saved civicdesk:checks from localStorage while keeping reading prefs
    goto_local(page, site_server + "/id-records/oregon/court/")
    page.click("#settings-open")
    page.check('#settings input[name="theme"][value="dark"]')
    page.check('#settings input[name="remember"]')
    page.click("#settings-close")
    page.locator("input[data-check]").first.check()
    assert page.evaluate("() => localStorage.getItem('civicdesk:checks')") is not None
    page.click("#quick-exit")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)
    goto_local(page, site_server + "/id-records/oregon/court/")
    assert page.evaluate("() => localStorage.getItem('civicdesk:checks')") is None
    assert page.locator("input[data-check]").first.is_checked() is False
    assert page.evaluate("() => document.documentElement.dataset.theme") == "dark"

    # 3c. Optional Reading settings checkbox turns off triple-Shift while keeping Leave quickly button active
    page.click("#settings-open")
    page.check('#settings input[name="shiftexit"]')
    page.click("#settings-close")
    assert page.evaluate("() => document.documentElement.dataset.shiftexit") == "off"
    goto_local(page, site_server + "/id-records/oregon/")
    assert page.evaluate("() => document.documentElement.dataset.shiftexit") == "off"
    page.locator("body").click(position={"x": 10, "y": 250})
    for _ in range(3):
        page.keyboard.press("Shift")
    page.wait_for_timeout(150)
    assert "weather.gov" not in page.url
    # Button still exits
    page.click("#quick-exit")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    # Resetting reading settings restores triple-Shift
    goto_local(page, site_server + "/id-records/oregon/")
    page.click("#settings-open")
    page.click("#reset-settings")
    page.click("#settings-close")
    assert page.evaluate("() => document.documentElement.dataset.shiftexit") == "on"
    page.locator("body").click(position={"x": 10, "y": 250})
    for _ in range(3):
        page.keyboard.press("Shift")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    # 4. Reading settings modal, Dark ink theme, and Forget everything
    goto_local(page, site_server + "/")
    page.click("#settings-open")
    assert page.evaluate("() => document.getElementById('settings').open")
    page.check('#settings input[name="theme"][value="dark"]')
    assert page.evaluate("() => document.documentElement.dataset.theme") == "dark"
    page.click("#settings-close")
    assert page.evaluate("() => document.activeElement.id") == "settings-open"

    page.click("#settings-open")
    page.click("#forget-all")
    page.click("#settings-close")
    left = page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('civicdesk:'))")
    assert left == []

    # 5. Medicaid checker Back-navigation / pageshow bfcache privacy reset
    goto_local(page, site_server + "/medicaid/oregon/check/")
    assert page.get_attribute("#checker", "autocomplete") == "off"
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="pregnant"]')
    page.click('[data-step="2"] button[data-next]')
    page.check('input[name="meet"][value="none"]')
    page.click('#checker button[type="submit"]')
    assert page.is_visible("#checker-result")
    # Navigate away and return via Back (and also dispatch a synthetic persisted pageshow event)
    goto_local(page, site_server + "/medicaid/oregon/")
    page.go_back(wait_until="load")
    assert page.is_hidden("#checker-result")
    assert page.is_visible("#checker")
    assert page.locator('#checker input:checked').count() == 0
    # Synthetic bfcache pageshow after filling results
    page.check('input[name="age"][value="under19"]')
    page.click('[data-step="1"] button[data-next]')
    assert page.is_visible("#checker-result")
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
    assert page.is_hidden("#checker-result")
    assert page.locator('#checker input:checked').count() == 0

    ctx.close()


def test_hardening_ux_controls_timezone_and_storage(site_server: str, browser_instance) -> None:
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()

    # 1. Dark ink sets computed color-scheme: dark on native radio controls; light/warm/contrast set light
    goto_local(page, site_server + "/medicaid/oregon/check/")
    assert page.evaluate("""() => {
        document.documentElement.setAttribute('data-theme', 'dark');
        return getComputedStyle(document.querySelector('input[type="radio"]')).colorScheme;
    }""") == "dark"
    for light_theme in ("light", "warm", "contrast"):
        assert page.evaluate(f"""() => {{
            document.documentElement.setAttribute('data-theme', '{light_theme}');
            return getComputedStyle(document.querySelector('input[type="radio"]')).colorScheme;
        }}""") == "light"

    # 2. Checker validation error sits above Step 1 fieldset inside viewport, and multi-select recap uses ', '
    goto_local(page, site_server + "/medicaid/oregon/check/")
    page.click('[data-step="1"] button[data-next]')
    assert page.is_visible("#step-1-error")
    checker_vis = page.evaluate("""() => {
        const err = document.getElementById('step-1-error').getBoundingClientRect();
        const fs = document.querySelector('[data-step="1"] fieldset').getBoundingClientRect();
        return {
            activeId: document.activeElement && document.activeElement.id,
            errInView: err.top >= 0 && err.bottom <= window.innerHeight,
            fsVisibleBelowErr: fs.top >= err.bottom - 2 && fs.top < window.innerHeight
        };
    }""")
    assert checker_vis["activeId"] == "step-1-error"
    assert checker_vis["errInView"]
    assert checker_vis["fsVisibleBelowErr"]

    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="pregnant"]')
    page.check('input[name="reason"][value="caregiver"]')
    page.click('[data-step="2"] button[data-next]')
    recap_text = page.inner_text("#checker-recap-preview")
    assert "pregnant, " in recap_text.lower() or ", " in recap_text

    # 3. Path Finder empty submit keeps both #path-error and #q-live visible below sticky .exit-bar (desktop & mobile)
    for vw in ({"width": 1280, "height": 800}, {"width": 360, "height": 740}):
        page.set_viewport_size(vw)
        goto_local(page, site_server + "/id-records/path/")
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.click('#path-form button[type="submit"]')
        assert page.is_visible("#path-error")
        path_vis = page.evaluate("""() => {
            const bar = document.querySelector('.exit-bar').getBoundingClientRect();
            const err = document.getElementById('path-error').getBoundingClientRect();
            const q1 = document.getElementById('q-live').getBoundingClientRect();
            return {
                activeId: document.activeElement && document.activeElement.id,
                describedBy: document.getElementById('q-live').getAttribute('aria-describedby'),
                errBelowBar: err.top >= bar.bottom - 1,
                errInView: err.bottom <= window.innerHeight,
                q1Visible: q1.top >= err.bottom - 2 && q1.top < window.innerHeight
            };
        }""")
        assert path_vis["activeId"] == "path-error", f"viewport {vw}: {path_vis}"
        assert path_vis["describedBy"] == "path-error", f"viewport {vw}: {path_vis}"
        assert path_vis["errBelowBar"], f"viewport {vw}: #path-error tucked behind .exit-bar: {path_vis}"
        assert path_vis["errInView"], f"viewport {vw}: #path-error outside viewport: {path_vis}"
        assert path_vis["q1Visible"], f"viewport {vw}: #q-live not visible alongside #path-error: {path_vis}"

    # 4. /start/ searchable guide directory filters client-side with zero storage/network side effects
    page.set_viewport_size({"width": 1280, "height": 800})
    goto_local(page, site_server + "/medicaid/")
    card_align = page.evaluate("""() => {
        const cards = Array.from(document.querySelectorAll('.state-cards > .card'));
        const rowCards = cards.filter(c => Math.abs(c.getBoundingClientRect().top - cards[0].getBoundingClientRect().top) < 2);
        const heights = rowCards.map(c => Math.round(c.getBoundingClientRect().height));
        const btnTops = rowCards.map(c => Math.round(c.querySelector('.btn-row').getBoundingClientRect().top));
        return { count: rowCards.length, heights, btnTops };
    }""")
    assert card_align["count"] >= 2
    assert len(set(card_align["heights"])) == 1, f"Unequal state card heights on same row: {card_align}"
    assert max(card_align["btnTops"]) - min(card_align["btnTops"]) <= 2, f"Misaligned .btn-row tops: {card_align}"

    goto_local(page, site_server + "/start/")
    page.fill("#guide-filter", "passport")
    assert "matching guide" in page.inner_text("#guide-filter-status")
    assert page.locator('#all-guides a[href="/id-records/federal/"]').is_visible()
    page.fill("#guide-filter", "")
    assert page.inner_text("#guide-filter-status") == ""

    # 5. Checklist non-persistence hint + state switcher focusout closing
    goto_local(page, site_server + "/id-records/oregon/court/")
    first_check = page.locator("input[data-check]").first
    first_check.check()
    assert page.is_visible(".checklist-persistence-hint")
    assert page.evaluate("() => localStorage.getItem('civicdesk:checks')") is None

    switcher_summary = page.locator("[data-state-switcher] > summary").first
    switcher_summary.click()
    assert page.evaluate("() => document.querySelector('[data-state-switcher]').open") is True
    page.focus("#settings-open")
    page.wait_for_timeout(50)
    assert page.evaluate("() => document.querySelector('[data-state-switcher]').open") is False
    ctx.close()

    # 6. Local-timezone [data-show-until] check at 8:30 PM Pacific on Dec 31, 2026 (which is Jan 1, 2027 04:30 UTC!)
    tz_ctx = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        timezone_id="America/Los_Angeles",
    )
    tz_ctx.add_init_script("""(() => {
        const fixed = new Date('2026-12-31T20:30:00-08:00').getTime();
        const OrigDate = Date;
        class MockDate extends OrigDate {
            constructor(...args) {
                super(...(args.length ? args : [fixed]));
            }
            static now() { return fixed; }
        }
        window.Date = MockDate;
    })();""")
    tz_page = tz_ctx.new_page()
    goto_local(tz_page, site_server + "/medicaid/oregon/")
    # Confirm UTC date is already 2027-01-01 while local date in LA is still 2026-12-31
    assert tz_page.evaluate("() => new Date().toISOString().slice(0, 10)") == "2027-01-01"
    assert tz_page.evaluate("""() => {
        const items = Array.from(document.querySelectorAll('[data-show-until="2026-12-31"]'));
        return items.length > 0 && items.every(el => !el.hidden);
    }""") is True
    tz_ctx.close()


def test_visual_system_xl_wide_reflow_brief_and_progress(site_server: str, browser_instance) -> None:
    visual_routes = [
        "/",
        "/start/",
        "/privacy/",
        "/accessibility/",
        "/404.html",
        "/medicaid/",
        "/medicaid/oregon/",
        "/medicaid/oregon/rules/",
        "/medicaid/oregon/reasons/",
        "/medicaid/oregon/letter/",
        "/medicaid/oregon/denied/",
        "/medicaid/oregon/words/",
        "/medicaid/oregon/log/",
        "/medicaid/oregon/check/",
        "/id-records/",
        "/id-records/path/",
        "/id-records/oregon/",
        "/id-records/oregon/court/",
        "/id-records/oregon/birth/",
        "/id-records/oregon/id/",
        "/id-records/federal/",
        "/id-records/compare/",
        "/id-records/after/",
    ]
    errors: list[str] = []

    def open_viz_ctx():
        c = browser_instance.new_context(viewport={"width": 390, "height": 844})
        c.add_init_script(
            "window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));"
        )
        p = c.new_page()
        p.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        p.on("pageerror", lambda exc: errors.append(str(exc)))
        return c, p

    ctx, page = open_viz_ctx()

    # 1. Verify zero horizontal overflow and zero CSP errors at 320px and 390px with XL + Wide spacing active
    for idx, route in enumerate(visual_routes):
        if idx and idx % 12 == 0:
            ctx.close()
            ctx, page = open_viz_ctx()
        errors.clear()
        goto_local(page, site_server + route)
        page.evaluate(
            "() => { document.documentElement.dataset.size = 'xl'; document.documentElement.dataset.spacing = 'wide'; }"
        )
        for width in (320, 390):
            page.set_viewport_size({"width": width, "height": 844})
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 1, f"{route}: horizontal overflow of {overflow}px at {width}px with XL + Wide"
        assert not errors, f"{route}: console/page errors: {errors}"
        assert page.evaluate("() => window.__csp || []") == []

    # 2. Verify Short version mode (both data-brief="on" and data-brief="true") hides [data-deeper] & details.deeper cleanly
    page.set_viewport_size({"width": 1280, "height": 800})
    goto_local(page, site_server + "/medicaid/oregon/rules/")
    assert page.locator(".viz-calendar[data-deeper]").is_visible()
    for brief_val in ("true", "on"):
        page.evaluate(f"() => {{ document.documentElement.dataset.brief = '{brief_val}'; }}")
        assert page.locator(".viz-calendar[data-deeper]").is_hidden()
        assert page.locator("details.deeper").first.is_hidden()
    page.evaluate("() => { document.documentElement.dataset.brief = 'off'; }")
    assert page.locator(".viz-calendar[data-deeper]").is_visible()

    goto_local(page, site_server + "/medicaid/oregon/reasons/")
    for brief_val in ("true", "on"):
        page.evaluate(f"() => {{ document.documentElement.dataset.brief = '{brief_val}'; }}")
        assert page.locator("details.deeper").first.is_hidden()
        assert page.locator(".safety-exit-link[data-quick-exit]").is_visible()
    page.evaluate("() => { document.documentElement.dataset.brief = 'off'; }")

    # 3. Verify 3-step visual progress track aria-current="step", completion states, and outcome emblem on check wizard
    goto_local(page, site_server + "/medicaid/oregon/check/")
    assert page.is_visible("#checker-steps-track")
    assert page.get_attribute('[data-progress-step="1"]', "aria-current") == "step"
    assert page.get_attribute('[data-progress-step="2"]', "aria-current") is None

    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    assert "is-complete" in (page.get_attribute('[data-progress-step="1"]', "class") or "")
    assert page.get_attribute('[data-progress-step="2"]', "aria-current") == "step"

    page.check('input[name="reason"][value="none"]')
    page.click('[data-step="2"] button[data-next]')
    assert "is-complete" in (page.get_attribute('[data-progress-step="2"]', "class") or "")
    assert page.get_attribute('[data-progress-step="3"]', "aria-current") == "step"

    page.check('input[name="meet"][value="hours"]')
    page.click('#checker button[type="submit"]')
    assert page.is_hidden("#checker-steps-track")
    assert page.is_visible("#checker-result .result-emblem")
    assert "Activity check" in page.inner_text("#checker-result .result-emblem")

    ctx.close()


def test_state_switcher_summary_and_menu_never_clip_or_overflow_and_editorial_art_loads(
    site_server: str, browser_instance
) -> None:
    ctx = browser_instance.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()

    for route in ("/", "/id-records/", "/medicaid/oregon/rules/"):
        for size_mode in ("m", "xl"):
            for width in (320, 375, 390, 430, 640, 820):
                page.set_viewport_size({"width": width, "height": 844})
                goto_local(page, site_server + route)
                page.evaluate(f"() => {{ document.documentElement.dataset.size = '{size_mode}'; }}")

                closed = page.evaluate("""() => {
                    const s = document.querySelector('[data-state-switcher] > summary');
                    const r = s.getBoundingClientRect();
                    return {
                        left: r.left,
                        right: r.right,
                        innerWidth: window.innerWidth,
                        scrollWidth: s.scrollWidth,
                        clientWidth: s.clientWidth,
                    };
                }""")
                assert closed["left"] >= 0, f"{route}@{width}px ({size_mode}): summary clipped left: {closed}"
                assert closed["right"] <= closed["innerWidth"] + 0.5, (
                    f"{route}@{width}px ({size_mode}): summary overflows right: {closed}"
                )
                assert closed["scrollWidth"] <= closed["clientWidth"] + 2, (
                    f"{route}@{width}px ({size_mode}): summary text clipped: {closed}"
                )

                page.click("[data-state-switcher] > summary")
                opened = page.evaluate("""() => {
                    const sw = document.querySelector('[data-state-switcher]');
                    const m = sw.querySelector('.state-switcher-menu');
                    const r = m.getBoundingClientRect();
                    return {
                        open: sw.open,
                        left: r.left,
                        right: r.right,
                        innerWidth: window.innerWidth,
                    };
                }""")
                assert opened["open"] is True
                assert opened["left"] >= 0, f"{route}@{width}px ({size_mode}): menu clipped off left edge: {opened}"
                assert opened["right"] <= opened["innerWidth"] + 0.5, (
                    f"{route}@{width}px ({size_mode}): menu overflows right edge: {opened}"
                )

    # Verify self-hosted editorial WebP illustrations load cleanly with non-zero natural dimensions
    for art_route in ("/", "/medicaid/", "/medicaid/oregon/reasons/", "/medicaid/oregon/letter/", "/medicaid/oregon/apply/", "/id-records/"):
        page.set_viewport_size({"width": 1280, "height": 900})
        goto_local(page, site_server + art_route)
        art_stats = page.evaluate("""async () => {
            const imgs = Array.from(document.querySelectorAll('.editorial-art img'));
            for (const img of imgs) {
                img.loading = 'eager';
                if (!img.complete) {
                    await new Promise((resolve) => {
                        img.addEventListener('load', resolve, { once: true });
                        img.addEventListener('error', resolve, { once: true });
                    });
                }
            }
            return imgs.map(i => ({ src: i.getAttribute('src'), complete: i.complete, w: i.naturalWidth, h: i.naturalHeight }));
        }""")
        assert art_stats, f"{art_route}: expected .editorial-art img elements"
        for item in art_stats:
            assert item["complete"] and item["w"] > 0 and item["h"] > 0, f"{art_route}: broken editorial art {item}"

    ctx.close()


def test_accessibility_navigation_and_broadsheet_polish(site_server: str, browser_instance) -> None:
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    # 1. Dark Ink :focus-visible contrast (>= 7:1 on #EDBB00) on links and nested child spans
    def _rgb_contrast(fg_css: str, bg_css: str) -> float:
        def parse(s: str) -> tuple[int, int, int]:
            nums = [int(x) for x in s.replace("rgba(", "").replace("rgb(", "").replace(")", "").split(",")[:3]]
            return nums[0], nums[1], nums[2]

        def lum(rgb: tuple[int, int, int]) -> float:
            vals = [c / 255.0 for c in rgb]
            lin = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        la, lb = lum(parse(fg_css)), lum(parse(bg_css))
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    for route, target_sel, child_sel in (
        ("/", ".desk-tile-link", ".desk-tile-go"),
        ("/medicaid/oregon/", "#tab-sources", ".page-tab-count"),
        ("/id-records/oregon/", ".site-nav a", None),
    ):
        goto_local(page, site_server + route)
        page.evaluate("() => { document.documentElement.dataset.theme = 'dark'; }")
        loc = page.locator(target_sel).first
        if target_sel == "#tab-sources":
            page.locator("#tab-guide").focus()
            page.keyboard.press("Tab")
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("ArrowRight")
        else:
            loc.focus()
            page.keyboard.press("Tab")
            page.keyboard.press("Shift+Tab")
        styles = loc.evaluate(
            """(el, cSel) => {
                const cs = getComputedStyle(el);
                const child = cSel ? el.querySelector(cSel) : null;
                const ccs = child ? getComputedStyle(child) : null;
                return {
                    matchesFocusVisible: el.matches(':focus-visible'),
                    fg: cs.color,
                    bg: cs.backgroundColor,
                    childFg: ccs ? ccs.color : null,
                    childBg: ccs ? ccs.backgroundColor : null,
                };
            }""",
            child_sel,
        )
        assert styles["matchesFocusVisible"], f"{route} {target_sel}: did not enter :focus-visible"
        ratio = _rgb_contrast(styles["fg"], styles["bg"])
        assert ratio >= 7.0, f"{route} {target_sel}: Dark Ink focus contrast {ratio:.2f}:1 ({styles})"
        if styles["childFg"]:
            eff_bg = styles["bg"] if styles["childBg"] in ("rgba(0, 0, 0, 0)", "transparent") else styles["childBg"]
            child_ratio = _rgb_contrast(styles["childFg"], eff_bg)
            assert child_ratio >= 7.0, f"{route} {target_sel} child {child_sel}: contrast {child_ratio:.2f}:1 ({styles})"

    # 2. Reading settings in-dialog live region, #brief-toggle aria-pressed, and non-<nav> .exit-bar
    goto_local(page, site_server + "/id-records/oregon/")
    assert page.evaluate("() => document.querySelector('.exit-bar').tagName") == "DIV"
    assert page.get_attribute(".exit-bar", "role") == "region"
    assert page.get_attribute("#brief-toggle", "aria-pressed") == "false"
    page.click("#brief-toggle")
    assert page.get_attribute("#brief-toggle", "aria-pressed") == "true"
    page.click("#brief-toggle")
    assert page.get_attribute("#brief-toggle", "aria-pressed") == "false"

    page.click("#settings-open")
    page.check('#settings input[name="size"][value="l"]')
    page.wait_for_function("() => (document.getElementById('dialog-announcer').textContent || '').includes('Large')")
    page.click("#settings-close")

    # 3. Medicaid checker aria-describedby starts unset, toggles on validation error, focuses <h2> on step change
    goto_local(page, site_server + "/medicaid/oregon/check/")
    assert page.get_attribute('[data-step="1"] fieldset', "aria-describedby") is None
    page.click('[data-step="1"] button[data-next]')
    assert page.get_attribute('[data-step="1"] fieldset', "aria-describedby") == "step-1-error"
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    assert page.get_attribute('[data-step="1"] fieldset', "aria-describedby") is None
    active_info = page.evaluate(
        "() => ({ tag: document.activeElement.tagName, step: document.activeElement.closest('[data-step]')?.dataset.step })"
    )
    assert active_info == {"tag": "H2", "step": "2"}

    # 4. Guide/Sources ARIA tabs: Space activation, ArrowDown ignored, tabindex="0", history Back, Back to guide
    goto_local(page, site_server + "/medicaid/oregon/letter/")
    assert page.get_attribute("#guide", "tabindex") == "0"
    assert page.get_attribute(".page-sources", "tabindex") is None
    page.focus("#tab-guide")
    page.keyboard.press("ArrowDown")
    assert page.get_attribute("#tab-guide", "aria-selected") == "true"
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Space")
    assert page.get_attribute("#tab-sources", "aria-selected") == "true"
    assert page.get_attribute(".page-sources", "tabindex") == "0"
    assert page.get_attribute("#guide", "tabindex") is None
    page.click("[data-back-to-guide]")
    assert page.get_attribute("#tab-guide", "aria-selected") == "true"

    # Section-sources click pushes history state; browser Back returns cleanly to Guide tab
    page.locator(".section-sources").first.click()
    assert page.get_attribute("#tab-sources", "aria-selected") == "true"
    assert "#src-group-" in page.url
    page.go_back()
    assert page.get_attribute("#tab-guide", "aria-selected") == "true"

    # 5. Breadcrumbs on level-2+ pages, absent on / and /medicaid/, plus persistent Find a guide nav link
    for crumb_route, expected_links in (
        ("/medicaid/oregon/letter/", ["/", "/medicaid/", "/medicaid/oregon/"]),
        ("/id-records/oregon/court/", ["/", "/id-records/", "/id-records/oregon/"]),
    ):
        goto_local(page, site_server + crumb_route)
        hrefs = page.eval_on_selector_all("nav.breadcrumb a", "els => els.map(a => a.getAttribute('href'))")
        assert hrefs == expected_links, f"{crumb_route}: unexpected breadcrumbs {hrefs}"
        assert page.locator("nav.breadcrumb [aria-current='page']").count() == 1
        assert page.locator('.site-nav a[href="/start/#all-guides"]').count() == 1

    for no_crumb_route in ("/", "/medicaid/"):
        goto_local(page, site_server + no_crumb_route)
        assert page.locator("nav.breadcrumb").count() == 0

    # 6. Desktop 1280px XL + Wide reflow: .how-grid single column, no inline-start borders, zero overflow
    for route in ("/", "/medicaid/oregon/"):
        goto_local(page, site_server + route)
        page.evaluate(
            "() => { document.documentElement.dataset.size = 'xl'; document.documentElement.dataset.spacing = 'wide'; }"
        )
        assert page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth") <= 1
        if route == "/":
            how_cols = page.evaluate(
                "() => getComputedStyle(document.querySelector('.how-grid')).gridTemplateColumns.split(' ').length"
            )
            assert how_cols == 1, f"Expected 1-column .how-grid under XL+Wide desktop, got {how_cols}"
            assert page.evaluate("() => getComputedStyle(document.querySelector('.how-grid > li + li')).borderLeftWidth") == "0px"
        else:
            assert (
                page.evaluate("() => getComputedStyle(document.querySelector('.viz-stat-card + .viz-stat-card')).borderLeftWidth")
                == "0px"
            )

    # 7. /start/ guide filter is NOT wiped by pageshow while form answers reset
    goto_local(page, site_server + "/start/")
    page.fill("#guide-filter", "passport")
    assert page.locator('#all-guides a[href="/id-records/federal/"]').is_visible()
    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
    assert page.input_value("#guide-filter") == "passport"
    assert page.locator('#all-guides a[href="/id-records/federal/"]').is_visible()

    # 8. forced-colors: active and dark + prefers-contrast: more render cleanly with visible borders
    page.emulate_media(forced_colors="active")
    goto_local(page, site_server + "/")
    assert page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('.desk-tile')).borderTopWidth)") >= 2.0
    page.emulate_media(forced_colors="none", color_scheme="dark", contrast="more")
    goto_local(page, site_server + "/medicaid/oregon/rules/")
    assert page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth") <= 1

    ctx.close()




