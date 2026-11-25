from __future__ import annotations

from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent

SPACING_CSS = (
    "* { line-height: 1.5 !important; letter-spacing: 0.12em !important; "
    "word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }"
)


def test_all_pages_clean_load_and_reflow(site_server: str, browser_instance) -> None:
    pages_yaml = yaml.safe_load((ROOT / "data" / "pages.yaml").read_text(encoding="utf-8"))
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    outside_requests: list[str] = []
    csp_violations: list[str] = []

    page.add_init_script(
        "window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));"
    )
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("request", lambda req: outside_requests.append(req.url) if not req.url.startswith(site_server) else None)

    for entry in pages_yaml:
        route = entry["route"]
        errors.clear()
        outside_requests.clear()
        resp = page.goto(site_server + route, wait_until="load")
        assert resp is not None and resp.status == 200, f"{route}: HTTP {resp and resp.status}"
        assert not errors, f"{route}: console/page errors: {errors}"
        assert not outside_requests, f"{route}: unexpected external requests: {outside_requests}"
        csp_violations = page.evaluate("() => window.__csp || []")
        assert not csp_violations, f"{route}: CSP violations: {csp_violations}"

    # Check 200% zoom (640px) and 400% zoom (320px) reflow + >=44px touch targets on key pages across all desks
    key_routes = [
        "/",
        "/start/",
        "/medicaid/",
        "/medicaid/oregon/",
        "/medicaid/oregon/check/",
        "/medicaid/washington/",
        "/medicaid/utah/",
        "/id-records/",
        "/id-records/path/",
        "/id-records/oregon/",
        "/id-records/oregon/court/",
        "/id-records/compare/",
        "/sources/",
    ]
    spacing_ctx = browser_instance.new_context(viewport={"width": 320, "height": 800}, bypass_csp=True)
    spacing_page = spacing_ctx.new_page()

    for route in key_routes:
        for width in (640, 320):
            page.set_viewport_size({"width": width, "height": 800})
            page.goto(site_server + route, wait_until="load")
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 1, f"{route}: horizontal overflow of {overflow}px at {width}px viewport"

        # Check touch target sizes (>= 44px height on standalone interactive controls, >= 24x24px everywhere)
        small = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('button, summary, .site-nav a, .footer-links a, .btn, .btn-secondary, .exit-link, .safety-exit-link, .choice').forEach(el => {
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

        spacing_page.goto(site_server + route, wait_until="load")
        spacing_page.add_style_tag(content=SPACING_CSS)
        sp_overflow = spacing_page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        assert sp_overflow <= 1, f"{route}: horizontal overflow of {sp_overflow}px with WCAG text spacing at 320px"

    spacing_ctx.close()
    ctx.close()


def test_start_router_and_medicaid_checks(site_server: str, browser_instance) -> None:
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    # 1. Test /start/ 2-question router
    page.goto(site_server + "/start/", wait_until="load")
    page.click("#start-form button[type=submit]")
    assert page.is_visible("#start-error")
    page.check('input[name="state"][value="oregon"]')
    page.check('input[name="topic"][value="medicaid"]')
    page.click("#start-form button[type=submit]")
    assert page.is_visible("#start-result")
    assert 'href="/medicaid/oregon/"' in page.inner_html("#start-result-body")
    assert 'href="/medicaid/oregon/check/"' in page.inner_html("#start-result-body")

    # 2. Test Washington 1-minute check (including the fixed 6th 'work' option!)
    page.goto(site_server + "/medicaid/washington/check/", wait_until="load")
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="none"]')
    page.click('[data-step="2"] button[data-next]')
    assert page.locator('input[name="meet"][value="work"]').count() == 1
    page.check('input[name="meet"][value="work"]')
    page.click('#checker button[type="submit"]')
    assert page.is_visible("#checker-result")
    assert "You may already meet the new rules." in page.inner_text("#checker-result")
    assert "You work at least 80 hours a month." in page.inner_text("#checker-result")

    # 3. Test Oregon 1-minute check under-19 short circuit
    page.goto(site_server + "/medicaid/oregon/check/", wait_until="load")
    page.check('input[name="age"][value="under19"]')
    page.click('[data-step="1"] button[data-next]')
    assert page.is_visible("#checker-result")
    assert "do not apply to your age group" in page.inner_text("#checker-result")

    # 4. Test Utah 1-minute check exemption path
    page.goto(site_server + "/medicaid/utah/check/", wait_until="load")
    page.check('input[name="age"][value="19to64"]')
    page.click('[data-step="1"] button[data-next]')
    page.check('input[name="reason"][value="pregnant"]')
    page.click('[data-step="2"] button[data-next]')
    page.check('input[name="meet"][value="none"]')
    page.click('#checker button[type="submit"]')
    assert page.is_visible("#checker-result")
    assert "You may not have to meet the new rules." in page.inner_text("#checker-result")

    ctx.close()


def test_broadened_id_records_path_finder(site_server: str, browser_instance) -> None:
    ctx = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    page.goto(site_server + "/id-records/path/", wait_until="load")

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
    assert "You do not need a court order for this path." in page.inner_text("#results-summary")

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
        page.goto(site_server + non_exit_route, wait_until="load")
        assert page.locator("[data-quick-exit]").count() == 0
        for _ in range(3):
            page.keyboard.press("Shift")
        page.wait_for_timeout(150)
        assert "weather.gov" not in page.url

    # 2. Leave quickly click AND triple-Shift work on /id-records/* (and Shift+letter does NOT false-trigger)
    page.goto(site_server + "/id-records/oregon/", wait_until="load")
    page.locator("body").click(position={"x": 10, "y": 250})
    for ch in ("A", "B", "C"):
        page.keyboard.down("Shift")
        page.keyboard.press(ch)
        page.keyboard.up("Shift")
    assert "weather.gov" not in page.url

    for _ in range(3):
        page.keyboard.press("Shift")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    page.goto(site_server + "/id-records/", wait_until="load")
    page.click("#quick-exit")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    # 3. Scoped domestic-safety callout Leave quickly works on /medicaid/oregon/reasons/
    page.goto(site_server + "/medicaid/oregon/reasons/", wait_until="load")
    assert page.locator(".exit-bar").count() == 0
    page.click(".safety-exit-link[data-quick-exit]")
    page.wait_for_url("https://www.weather.gov/**", timeout=5000)

    # 4. Reading settings modal, dark slate theme, and Forget everything
    page.goto(site_server + "/", wait_until="load")
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

    ctx.close()
