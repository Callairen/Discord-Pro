"""Setup wizard and preset editor dialogs."""

from __future__ import annotations

import webbrowser
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from discord_pro.gui.client import RestClient
from discord_pro.gui.widgets import ActivityForm, ErrorLabel, error_details, error_message

DEV_PORTAL = "https://discord.com/developers/applications"


def _text_page(title: str, html: str) -> QWizardPage:
    page = QWizardPage()
    page.setTitle(title)
    label = QLabel(html)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setOpenExternalLinks(True)
    lay = QVBoxLayout(page)
    lay.addWidget(label)
    lay.addStretch()
    return page


class _ClientIdPage(QWizardPage):
    def __init__(self, rest: RestClient, initial: str):
        super().__init__()
        self.rest = rest
        self.setTitle("Application ID")
        self.setSubTitle("Paste the Application ID from the Developer Portal (General Information).")
        self.edit = QLineEdit(initial)
        self.edit.setObjectName("wizard_client_id")
        self.edit.setPlaceholderText("e.g. 1468102806786281596")
        self.error = ErrorLabel()
        form = QFormLayout(self)
        form.addRow("Application ID", self.edit)
        form.addRow("", self.error)
        self._validated: str | None = None

    def validatePage(self) -> bool:  # noqa: N802 (Qt API)
        value = self.edit.text().strip()
        if self._validated == value:
            return True

        def done(status: int, body: Any) -> None:
            errs = [e for e in (body or {}).get("errors", []) if e["field"] == "client_id"] if status == 200 else []
            if status != 200:
                self.error.show_error(error_message(status, body))
            elif errs:
                self.error.show_error(errs[0]["message"])
            else:
                self.error.show_error(None)
                self._validated = value
                self.wizard().next()

        self.rest.post("/api/v1/setup/validate", {"client_id": value, "activity": None}, done)
        return False


class _ActivityPage(QWizardPage):
    def __init__(self, rest: RestClient, client_page: _ClientIdPage, activity: dict[str, Any]):
        super().__init__()
        self.rest = rest
        self.client_page = client_page
        self.setTitle("Activity")
        self.setSubTitle("What should your profile show? You can change this any time.")
        self.form = ActivityForm()
        self.form.set(activity)
        self.error = ErrorLabel()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.error)
        self._validated: dict[str, Any] | None = None

    def validatePage(self) -> bool:  # noqa: N802
        activity = self.form.read()
        if self._validated == activity:
            return True

        def done(status: int, body: Any) -> None:
            if status != 200:
                self.error.show_error(error_message(status, body))
                return
            errs = [e for e in body.get("errors", []) if e["field"].startswith("activity")]
            errs = [{**e, "field": "details" if e["field"] == "activity" else e["field"]} for e in errs]
            unmatched = self.form.set_errors(errs)
            self.error.show_error("; ".join(unmatched) if unmatched else None)
            if not errs:
                self._validated = activity
                self.wizard().next()

        self.rest.post("/api/v1/setup/validate",
                       {"client_id": self.client_page.edit.text().strip(), "activity": activity}, done)
        return False


class _TestPage(QWizardPage):
    def __init__(self, rest: RestClient, client_page: _ClientIdPage):
        super().__init__()
        self.rest = rest
        self.client_page = client_page
        self.setTitle("Test connection")
        self.setSubTitle("Checks that Discord is running and accepts this Application ID (a real RPC handshake).")
        self.button = QPushButton("Test connection")
        self.button.setObjectName("wizard_test")
        self.result = QLabel("Not tested yet.")
        self.result.setWordWrap(True)
        self.result.setObjectName("wizard_test_result")
        self.tested = False
        lay = QVBoxLayout(self)
        lay.addWidget(self.button)
        lay.addWidget(self.result)
        lay.addStretch()
        self.button.clicked.connect(self.run)

    def run(self) -> None:
        self.button.setEnabled(False)
        self.result.setText("Testing…")

        def done(status: int, body: Any) -> None:
            self.button.setEnabled(True)
            result = (body or {}).get("result") or {}
            self.tested = bool(result.get("success"))
            self.result.setText("✓ Connected to Discord." if self.tested else f"✕ {error_message(status, body)}")

        self.rest.post("/api/v1/setup/test-connection", {"client_id": self.client_page.edit.text().strip()}, done)


class SetupWizard(QWizard):
    def __init__(self, rest: RestClient, config: dict[str, Any] | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.rest = rest
        self.setWindowTitle("Discord-Pro setup")
        self.setObjectName("setup_wizard")
        cfg = config or {"application": {}, "presence": {}}
        self.addPage(_text_page("Welcome", (
            "<p>Discord-Pro shows a custom Rich Presence (the “Playing …” card) on your Discord profile.</p>"
            "<p>You need:</p><ul>"
            "<li>The <b>Discord desktop app</b> running on this computer (the browser version cannot be used).</li>"
            "<li>A free <b>application</b> in the Discord Developer Portal — its name appears after “Playing”.</li>"
            "<li>In Discord: User Settings → Activity Privacy → <b>Share my activity</b> turned on.</li></ul>")))
        portal = _text_page("Create a Discord application", (
            f"<ol><li>Open <a href='{DEV_PORTAL}'>discord.com/developers/applications</a> and sign in.</li>"
            "<li>Click <b>New Application</b>, choose the name to show, accept the terms.</li>"
            "<li>On <b>General Information</b>, copy the <b>Application ID</b>.</li>"
            "<li>Optional: under <b>Rich Presence → Art Assets</b> upload images; use the asset name "
            "(e.g. <code>logo</code>) in the image fields.</li></ol>"
            "<p style='color:gray'>Discord has no API to create applications for you, so this is manual. "
            "Never paste your Discord account token anywhere.</p>"))
        open_btn = QPushButton("Open Developer Portal")
        open_btn.clicked.connect(lambda: webbrowser.open(DEV_PORTAL))
        portal.layout().insertWidget(1, open_btn)
        self.addPage(portal)
        self.client_page = _ClientIdPage(rest, cfg["application"].get("client_id", ""))
        self.addPage(self.client_page)
        self.activity_page = _ActivityPage(rest, self.client_page, cfg.get("presence") or {})
        self.addPage(self.activity_page)
        self.test_page = _TestPage(rest, self.client_page)
        self.addPage(self.test_page)
        finish = QWizardPage()
        finish.setTitle("Save")
        self.test_activation = QCheckBox("Turn the presence on now to try it")
        self.finish_error = ErrorLabel()
        fl = QVBoxLayout(finish)
        fl.addWidget(QLabel("Your settings are saved in your user profile on this computer."))
        fl.addWidget(self.test_activation)
        fl.addWidget(self.finish_error)
        fl.addStretch()
        self.addPage(finish)
        self.resize(640, 560)
        self._saved = False

    def accept(self) -> None:
        if self._saved:
            super().accept()
            return
        body = {"client_id": self.client_page.edit.text().strip(), "activity": self.activity_page.form.read(),
                "test_activation": self.test_activation.isChecked()}
        self.button(QWizard.WizardButton.FinishButton).setEnabled(False)

        def done(status: int, resp: Any) -> None:
            self.button(QWizard.WizardButton.FinishButton).setEnabled(True)
            result = (resp or {}).get("result")
            if status == 200 or (result and result.get("pending")):
                self._saved = True
                if result and not result.get("success"):
                    QMessageBox.information(self, "Saved", f"Settings saved. {error_message(status, resp)}")
                super(SetupWizard, self).accept()
            elif result is not None and status in (502, 503):
                self._saved = True
                QMessageBox.warning(self, "Saved", f"Settings saved, but presence could not start: "
                                    f"{error_message(status, resp)}")
                super(SetupWizard, self).accept()
            else:
                self.finish_error.show_error(error_message(status, resp))

        self.rest.post("/api/v1/setup/complete", body, done)


class PresetDialog(QDialog):
    """Create or edit a preset; validation errors come from the backend."""

    def __init__(self, rest: RestClient, preset: dict[str, Any] | None, default_activity: dict[str, Any] | None,
                 on_saved: Callable[[], None], parent: QWidget | None = None):
        super().__init__(parent)
        self.rest = rest
        self.preset = preset
        self.on_saved = on_saved
        self.setWindowTitle(f"Edit preset “{preset['name']}”" if preset else "New preset")
        self.setObjectName("preset_dialog")
        p = preset or {"name": "", "client_id": "", "process_names": [], "activity": default_activity or {}}
        self.name = QLineEdit(p["name"])
        self.name.setObjectName("preset_name")
        self.name.setMaxLength(64)
        self.client_id = QLineEdit(p.get("client_id", ""))
        self.client_id.setPlaceholderText("optional — empty uses the default Application ID")
        self.procs = QLineEdit(", ".join(p.get("process_names", [])))
        self.procs.setPlaceholderText("Code.exe, Figma.exe")
        self.icon = QLineEdit(p.get("icon", ""))
        self.icon.setMaxLength(512)
        self.icon.setPlaceholderText("optional — https:// image URL shown in the App tab")
        self.form = ActivityForm()
        self.form.set(p.get("activity"))
        self.error = ErrorLabel()
        head = QFormLayout()
        head.addRow("Name", self.name)
        head.addRow("Application ID", self.client_id)
        head.addRow("Auto-switch processes", self.procs)
        head.addRow("App logo", self.icon)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(scroll)
        lay.addWidget(self.error)
        lay.addWidget(buttons)
        self.resize(600, 640)

    def body(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "client_id": self.client_id.text().strip(),
            "process_names": [s.strip() for s in self.procs.text().split(",") if s.strip()],
            "icon": self.icon.text().strip(),
            "activity": self.form.read(),
        }

    def save(self) -> None:
        self.save_button.setEnabled(False)

        def done(status: int, resp: Any) -> None:
            self.save_button.setEnabled(True)
            if status in (200, 201):
                self.on_saved()
                self.accept()
                return
            details = error_details(resp)
            unmatched = self.form.set_errors([{**d, "field": d["field"]} for d in details
                                               if str(d.get("field", "")).startswith("activity")])
            other = [f"{d['field']}: {d['message']}" for d in details if not str(d.get("field", "")).startswith("activity")]
            self.error.show_error("; ".join(unmatched + other) or error_message(status, resp))

        if self.preset:
            self.rest.put(f"/api/v1/presets/{self.preset['id']}", self.body(), done)
        else:
            self.rest.post("/api/v1/presets", self.body(), done)
