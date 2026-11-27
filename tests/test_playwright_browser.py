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
    assert "You may already meet the new rules." in page.inner_text("#checker-result")
    assert "You work at least 80 hours a month." in page.inner_text("#checker-result")

    # 3. Test Oregon 1-minute check under-19 short circuit
    goto_local(page, site_server + "/medicaid/oregon/check/")
    page.check('input[name="age"][value="under19"]')
    page.click('[data-step="1"] button[data-next]')
    assert page.is_visible("#checker-result")
    assert "do not apply to your age group" in page.inner_text("#checker-result")

    # 4. Test Utah 1-minute check exemption path
    goto_local(page, site_server + "/medicaid/utah/check/")
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



