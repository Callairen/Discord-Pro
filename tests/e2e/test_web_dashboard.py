"""Browser E2E tests for the dashboard (Playwright + Chromium)."""

import os
import time
from pathlib import Path

import httpx
import pytest

from harness import CID

pytestmark = pytest.mark.e2e
playwright_api = pytest.importorskip("playwright.sync_api")

SHOTS = Path(os.environ.get("DISCORD_PRO_SCREENSHOTS", Path(__file__).resolve().parents[2] / "docs" / "screenshots"))
CHROMIUM = os.environ.get("DISCORD_PRO_CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM) if CHROMIUM else p.chromium.launch()
        yield b
        b.close()


def login_page(browser, backend, **ctx):
    context = browser.new_context(**ctx)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    link = httpx.post(f"{backend.url}/api/v1/auth/login-link", headers=backend.headers()).json()["path"]
    page.goto(backend.url + link)
    page.wait_for_selector("#presence-toggle")
    page.errors = errors
    return page


def wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met")


def test_wizard_then_toggle(browser, backend, ipc):
    page = login_page(browser, backend)
    page.click("#nav a[data-route=setup]")
    page.get_by_role("button", name="Next", exact=True).click()
    page.get_by_role("button", name="Next", exact=True).click()
    page.fill("#wiz-cid", "123")
    page.get_by_role("button", name="Next", exact=True).click()
    page.locator(".field-error:visible").first.wait_for()  # AC-03: invalid ID rejected
    assert "17-20 digit" in page.locator(".field-error:visible").first.inner_text()
    page.fill("#wiz-cid", CID)
    page.get_by_role("button", name="Next", exact=True).click()
    page.fill("#wiz-details", "x")  # too short
    page.get_by_role("button", name="Next", exact=True).click()
    page.locator(".field-error:visible").first.wait_for()
    assert "at least 2" in page.locator(".field-error:visible").first.inner_text()
    page.fill("#wiz-details", "Browser wizard")
    page.fill("#wiz-state", "Playwright")
    page.get_by_role("button", name="Next", exact=True).click()
    page.click("#wiz-run-test")
    page.wait_for_selector("text=✓ Connected to Discord.")
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.get_by_role("button", name="Next", exact=True).click()
    page.check("#wiz-test")
    page.get_by_role("button", name="Save & finish", exact=True).click()
    page.wait_for_selector("#presence-toggle[aria-pressed=true]")
    wait(lambda: ipc.current_activity is not None)
    assert ipc.current_activity["details"] == "Browser wizard"
    page.wait_for_selector("text=Live on Discord")
    page.screenshot(path=str(SHOTS / "web-dashboard-live.png"), full_page=True)
    page.click("#presence-toggle")
    page.wait_for_selector("#presence-toggle[aria-pressed=false]")
    wait(lambda: ipc.current_activity is None)
    assert page.errors == []


def configure(backend):
    httpx.post(f"{backend.url}/api/v1/setup/complete", headers=backend.headers(),
               json={"client_id": CID, "activity": {"details": "Initial", "state": "Setup"}}, timeout=10)


def test_realtime_sync_between_clients(browser, backend, ipc):
    configure(backend)
    a = login_page(browser, backend)
    b = login_page(browser, backend)
    # change from "desktop" (REST with the API token) -> browser updates without reload
    httpx.post(f"{backend.url}/api/v1/presence/enable", headers=backend.headers(), timeout=30)
    a.wait_for_selector("#presence-toggle[aria-pressed=true]")
    b.wait_for_selector("#presence-toggle[aria-pressed=true]")
    # change from browser A -> browser B updates
    a.click("#presence-toggle")
    b.wait_for_selector("#presence-toggle[aria-pressed=false]")
    # activity edited in A appears in B's dashboard preview
    a.goto(backend.url + "/#/activity")
    a.fill("#edit-details", "Synced text")
    a.get_by_role("button", name="Save & apply", exact=True).click()
    b.wait_for_selector("text=Synced text")
    assert a.errors == [] and b.errors == []


def test_activity_validation_errors_shown(browser, backend):
    configure(backend)
    page = login_page(browser, backend)
    page.goto(backend.url + "/#/activity")
    page.fill("#edit-b1l", "Bad")
    page.fill("#edit-b1u", "javascript:alert(1)")
    page.get_by_role("button", name="Save & apply", exact=True).click()
    err = page.locator(".field-error:visible").first
    err.wait_for()
    assert "http" in err.inner_text()
    cfg = httpx.get(f"{backend.url}/api/v1/config", headers=backend.headers()).json()
    assert cfg["presence"]["buttons"] == []
    page.screenshot(path=str(SHOTS / "web-activity-validation.png"), full_page=True)


def test_presets_crud_in_browser(browser, backend, ipc):
    configure(backend)
    page = login_page(browser, backend)
    page.goto(backend.url + "/#/presets")
    page.wait_for_selector("text=No presets yet")
    page.get_by_role("button", name="+ New preset", exact=True).click()
    page.fill("#preset-name", "VS Code")
    page.fill("#preset-procs", "Code.exe")
    page.fill("#preset-details", "Coding Session")
    page.fill("#preset-state", "Visual Studio Code")
    page.get_by_role("button", name="Create preset", exact=True).click()
    page.wait_for_selector("li >> text=VS Code")
    page.get_by_role("button", name="Activate & turn on", exact=True).click()
    page.wait_for_selector("text=✓ active")
    wait(lambda: ipc.current_activity is not None and ipc.current_activity["details"] == "Coding Session")
    page.get_by_role("button", name="Duplicate", exact=True).click()
    page.wait_for_selector("text=VS Code (copy)")
    page.screenshot(path=str(SHOTS / "web-presets.png"), full_page=True)
    page.locator("li", has_text="VS Code (copy)").get_by_role("button", name="Delete").click()  # confirm() auto-accepted
    wait(lambda: len(httpx.get(f"{backend.url}/api/v1/presets", headers=backend.headers()).json()) == 1)


def test_access_code_login_and_wrong_code(browser, backend):
    context = browser.new_context()
    page = context.new_page()
    page.goto(backend.url + "/")
    page.wait_for_selector("#login-code")
    page.fill("#login-code", "WRONG-CODE-0000")
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_selector("text=Access code is incorrect")
    code = httpx.get(f"{backend.url}/api/v1/auth/access-code", headers=backend.headers()).json()["access_code"]
    page.fill("#login-code", code)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_selector("#presence-toggle")
    cookies = context.cookies()
    assert any(c["name"] == "dp_session" and c["httpOnly"] for c in cookies)
    assert page.evaluate("document.cookie") == ""  # session not readable by JS


def test_expired_link_message(browser, backend):
    page = browser.new_context().new_page()
    page.goto(backend.url + "/auth/redeem?code=bogus")
    page.wait_for_selector("text=has expired")


def test_mobile_layout_no_horizontal_scroll(browser, backend):
    configure(backend)
    page = login_page(browser, backend, viewport={"width": 375, "height": 740}, device_scale_factor=2, is_mobile=True)
    for route in ("dashboard", "activity", "presets", "setup", "settings"):
        page.goto(f"{backend.url}/#/{route}")
        page.wait_for_timeout(300)
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, f"{route} overflows by {overflow}px"
    page.goto(f"{backend.url}/#/dashboard")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOTS / "web-mobile-dashboard.png"), full_page=True)


def test_backend_restart_requires_reauth_and_resyncs(browser, backend):
    configure(backend)
    page = login_page(browser, backend)
    backend.stop()
    page.wait_for_selector("text=Live updates interrupted")  # AC-11/12: page notices, keeps working
    backend.start()
    # sessions are in memory: after a backend restart the browser must sign in again
    page.wait_for_selector("#login-code", timeout=40000)
