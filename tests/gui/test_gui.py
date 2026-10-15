"""Desktop GUI tests (offscreen Qt) against a real backend process.

These cover the GUI <-> backend integration. The OS system tray itself cannot
be exercised offscreen; the hide-on-close logic is tested with the tray
reported as available (see docs/TESTING.md for the manual tray checklist).
"""

import os
import shutil
import sys
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = [pytest.mark.gui, pytest.mark.skipif(sys.platform == "win32", reason="uses the Unix IPC simulator")]

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from fakes import FakeDiscordIPC, short_socket_dir  # noqa: E402
from harness import CID, BackendProcess  # noqa: E402

from PySide6.QtWidgets import QApplication, QWizard  # noqa: E402

SHOTS = Path(os.environ.get("DISCORD_PRO_SCREENSHOTS", Path(__file__).resolve().parents[2] / "docs" / "screenshots"))


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    d = short_socket_dir(tmp_path)
    ipc = FakeDiscordIPC(d, valid_ids={CID})
    ipc.start()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DISCORD_PRO_HOME", str(home))
    b = BackendProcess(home, d)
    b.write_config()
    b.start()
    yield b, ipc
    b.stop()
    ipc.stop()
    shutil.rmtree(d, ignore_errors=True)


def pump(qapp, pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


@pytest.fixture
def window(env, qapp):
    from discord_pro.gui.client import LiveChannel, RestClient
    from discord_pro.gui.main_window import MainWindow
    from discord_pro.local_client import discover

    b, _ = env
    info = discover()
    assert info is not None and info.port == b.port
    w = MainWindow(RestClient(info), LiveChannel(info))
    w.live.start()
    w.show()
    pump(qapp, lambda: w.state is not None and w.ws_status == "open")
    yield w
    w._quitting = True
    w.live.stop()
    w.close()


def configure(b, details="GUI test"):
    r = httpx.post(f"{b.url}/api/v1/setup/complete", headers=b.headers(),
                   json={"client_id": CID, "activity": {"details": details, "state": "Offscreen Qt"}}, timeout=10)
    assert r.status_code == 200


def test_toggle_enables_and_disables(window, env, qapp):
    b, ipc = env
    configure(b)
    pump(qapp, lambda: window.config["application"]["client_id"] == CID)
    window.toggle.click()
    pump(qapp, lambda: ipc.current_activity is not None)
    pump(qapp, lambda: "Live on Discord" in window.status_label.text())
    assert window.toggle.text() == "Presence ON" and window.toggle.isChecked()
    pump(qapp, lambda: window.events.count() > 0 and "(desktop)" in window.events.item(0).text())
    SHOTS.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(SHOTS / "gui-dashboard-live.png"))
    window.toggle.click()
    pump(qapp, lambda: ipc.current_activity is None and not window.toggle.isChecked())


def test_remote_change_reflected_without_refresh(window, env, qapp):
    b, ipc = env
    configure(b)
    httpx.post(f"{b.url}/api/v1/presence/enable", headers=b.headers(), timeout=30)
    pump(qapp, lambda: window.toggle.isChecked() and "Live" in window.status_label.text())
    httpx.put(f"{b.url}/api/v1/presence", headers=b.headers(), timeout=30,
              json={"activity": {"details": "Changed in browser", "state": "Remote"}, "apply": True})
    pump(qapp, lambda: "Changed in browser" in window.preview.text())
    assert window.activity_form.inputs["details"].text() == "Changed in browser"  # editor refreshed (not dirty)


def test_remote_change_does_not_clobber_unsaved_edits(window, env, qapp):
    b, _ = env
    configure(b)
    pump(qapp, lambda: window.activity_form.inputs["details"].text() == "GUI test")
    window.activity_form.inputs["details"].setText("My unsaved edit")
    httpx.put(f"{b.url}/api/v1/presence", headers=b.headers(), json={"activity": {"details": "Remote"}, "apply": False})
    pump(qapp, lambda: not window.remote_banner.isHidden())
    assert window.activity_form.inputs["details"].text() == "My unsaved edit"


def test_activity_save_apply_and_validation(window, env, qapp):
    b, ipc = env
    configure(b)
    httpx.post(f"{b.url}/api/v1/presence/enable", headers=b.headers(), timeout=30)
    pump(qapp, lambda: window.activity_form.inputs["details"].text() == "GUI test")
    window.tabs.setCurrentIndex(1)
    form = window.activity_form
    form.inputs["button1_label"].setText("Bad")
    form.inputs["button1_url"].setText("javascript:alert(1)")
    window.save_apply_btn.click()
    pump(qapp, lambda: not form.errors["button1"].isHidden())
    assert "http" in form.errors["button1"].text()
    window.grab().save(str(SHOTS / "gui-activity-validation.png"))
    form.inputs["button1_url"].setText("https://github.com/Callairen/Discord-Pro")
    form.inputs["details"].setText("Applied from GUI")
    window.save_apply_btn.click()
    pump(qapp, lambda: ipc.current_activity is not None and ipc.current_activity.get("details") == "Applied from GUI")
    cfg = httpx.get(f"{b.url}/api/v1/config", headers=b.headers()).json()
    assert cfg["presence"]["buttons"][0]["url"] == "https://github.com/Callairen/Discord-Pro"


def test_preset_activation_from_gui(window, env, qapp):
    b, ipc = env
    configure(b)
    httpx.post(f"{b.url}/api/v1/presets", headers=b.headers(),
               json={"name": "VS Code", "process_names": ["Code.exe"],
                     "activity": {"details": "Coding Session", "state": "Visual Studio Code"}})
    pump(qapp, lambda: window.preset_list.count() == 1)
    window.tabs.setCurrentIndex(2)
    window.preset_list.setCurrentRow(0)
    window.activate_preset(True)
    pump(qapp, lambda: ipc.current_activity is not None and ipc.current_activity.get("details") == "Coding Session")
    pump(qapp, lambda: window.preset_list.item(0).text().startswith("✓"))
    window.grab().save(str(SHOTS / "gui-presets.png"))


def test_close_hides_window_backend_keeps_running(window, env, qapp, monkeypatch):
    b, _ = env
    monkeypatch.setattr(window, "tray_available", lambda: True)
    window.close()
    qapp.processEvents()
    assert not window.isVisible()  # AC-09
    assert httpx.get(f"{b.url}/api/v1/health").status_code == 200
    configure(b, details="Changed while hidden")
    httpx.post(f"{b.url}/api/v1/presence/enable", headers=b.headers(), timeout=30)
    window.show_window()  # tray "Show window"
    pump(qapp, lambda: window.isVisible() and window.toggle.isChecked())  # AC-10: current state
    assert "Changed while hidden" in window.preview.text()


def test_close_without_tray_minimizes(window, qapp, monkeypatch):
    monkeypatch.setattr(window, "tray_available", lambda: False)
    window.close()
    qapp.processEvents()
    assert window.isVisible() or window.isMinimized()


def test_backend_restart_gui_reconnects(window, env, qapp):
    b, _ = env
    b.stop()
    pump(qapp, lambda: window.ws_status in ("waiting", "connecting"))
    assert not window.toggle.isEnabled()
    b.start()
    pump(qapp, lambda: window.ws_status == "open", timeout=40)
    pump(qapp, lambda: window.toggle.isEnabled())


def test_settings_save(window, env, qapp):
    b, _ = env
    window.tabs.setCurrentIndex(3)
    pump(qapp, lambda: window.access_code.text() != "")
    window.attempts.setValue(42)
    window.save_settings()
    pump(qapp, lambda: httpx.get(f"{b.url}/api/v1/config", headers=b.headers()).json()["rpc"]["reconnect_max_attempts"] == 42)
    window.grab().save(str(SHOTS / "gui-settings.png"))


def test_setup_wizard_flow(window, env, qapp):
    b, ipc = env
    from discord_pro.gui.dialogs import SetupWizard

    wiz = SetupWizard(window.rest, window.config, window)
    wiz.show()
    wiz.next()
    wiz.next()
    wiz.client_page.edit.setText("123")
    wiz.next()
    pump(qapp, lambda: not wiz.client_page.error.isHidden())  # invalid ID rejected
    wiz.client_page.edit.setText(CID)
    wiz.next()
    pump(qapp, lambda: wiz.currentPage() is wiz.activity_page)
    wiz.activity_page.form.inputs["details"].setText("From the wizard")
    wiz.next()
    pump(qapp, lambda: wiz.currentPage() is wiz.test_page)
    wiz.test_page.button.click()
    pump(qapp, lambda: "Connected" in wiz.test_page.result.text())
    wiz.grab().save(str(SHOTS / "gui-wizard-test.png"))
    wiz.next()
    wiz.test_activation.setChecked(True)
    wiz.button(QWizard.WizardButton.FinishButton).click()
    pump(qapp, lambda: ipc.current_activity is not None and ipc.current_activity.get("details") == "From the wizard")
    cfg = httpx.get(f"{b.url}/api/v1/config", headers=b.headers()).json()
    assert cfg["application"]["setup_completed"] is True


def test_exit_and_stop_backend(window, env, qapp, monkeypatch):
    b, _ = env
    monkeypatch.setattr("PySide6.QtWidgets.QApplication.quit", lambda *a: None)
    window.exit_and_stop_backend()
    pump(qapp, lambda: b.proc.poll() is not None)
    assert not (b.home / "instance.json").exists()
