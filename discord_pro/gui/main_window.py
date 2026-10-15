"""Main window and system tray."""

from __future__ import annotations

import time
import webbrowser
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from discord_pro.gui.client import LiveChannel, RestClient, describe_state, fmt_ago
from discord_pro.gui.dialogs import PresetDialog, SetupWizard
from discord_pro.gui.widgets import (
    ActivityForm,
    ErrorLabel,
    IconCache,
    error_details,
    error_message,
    letter_icon,
    status_icon,
)

TOGGLE_ON = ("QPushButton { background: #1f8a4c; color: white; border-radius: 22px; padding: 10px 28px;"
             " font-size: 16px; font-weight: bold; }")
TOGGLE_OFF = ("QPushButton { background: palette(button); border: 2px solid palette(mid); border-radius: 22px;"
              " padding: 10px 28px; font-size: 16px; font-weight: bold; }")


class MainWindow(QMainWindow):
    """All controls call the backend; the UI only reflects backend state."""

    state_applied = Signal(dict)

    def __init__(self, rest: RestClient, live: LiveChannel, *, owns_backend: bool = False):
        super().__init__()
        self.rest = rest
        self.live = live
        self.owns_backend = owns_backend
        self.state: dict[str, Any] | None = None
        self.config: dict[str, Any] | None = None
        self.presets: list[dict[str, Any]] = []
        self.ws_status = "connecting"
        self._busy = False
        self._quitting = False
        self._tray_hint_shown = False

        self.setWindowTitle("Discord-Pro")
        self.setWindowIcon(status_icon("DISCONNECTED"))
        self.resize(820, 680)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.setStatusBar(QStatusBar())
        self.icons = IconCache(self)
        self.icons.loaded.connect(lambda _url: self._render_apps())
        self._build_dashboard()
        self._build_apps()
        self._build_activity()
        self._build_presets()
        self._build_settings()
        self._build_tray()

        live.event.connect(self.on_event)
        live.status.connect(self.on_ws_status)
        self._ticker = QTimer(self)
        self._ticker.timeout.connect(self._refresh_dashboard)
        self._ticker.start(1000)

    # ------------------------------------------------------------ dashboard
    def _build_dashboard(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.setup_box = QGroupBox("Finish setup")
        sl = QHBoxLayout(self.setup_box)
        sl.addWidget(QLabel("Add your Discord Application ID and an activity to get started."))
        wiz_btn = QPushButton("Open setup wizard")
        wiz_btn.clicked.connect(self.open_wizard)
        sl.addWidget(wiz_btn)
        lay.addWidget(self.setup_box)

        hero = QHBoxLayout()
        left = QVBoxLayout()
        title = QLabel("Rich Presence")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        self.status_label = QLabel("Connecting to backend…")
        self.status_label.setObjectName("status_label")
        self.status_label.setStyleSheet("font-size: 14px;")
        self.error_label = ErrorLabel()
        self.error_label.setObjectName("error_label")
        self.apply_button = QPushButton("Apply saved changes")
        self.apply_button.clicked.connect(lambda: self._presence_command("POST", "/api/v1/presence/apply", "Changes applied"))
        self.apply_button.hide()
        left.addWidget(title)
        left.addWidget(self.status_label)
        left.addWidget(self.error_label)
        left.addWidget(self.apply_button, alignment=Qt.AlignmentFlag.AlignLeft)
        hero.addLayout(left, 1)
        self.toggle = QPushButton("Presence OFF")
        self.toggle.setObjectName("presence_toggle")
        self.toggle.setCheckable(True)
        self.toggle.setMinimumHeight(48)
        self.toggle.setMinimumWidth(200)
        self.toggle.setStyleSheet(TOGGLE_OFF)
        self.toggle.clicked.connect(self.on_toggle)
        self.toggle.setEnabled(False)
        hero.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignVCenter)
        hero_box = QGroupBox()
        hero_box.setLayout(hero)
        lay.addWidget(hero_box)

        grid = QGridLayout()
        self.info_labels: dict[str, QLabel] = {}
        for i, (key, label) in enumerate([("discord", "Discord connection"), ("preset", "Active preset"),
                                          ("updated", "Last successful update"), ("backend", "Backend")]):
            box = QGroupBox(label)
            bl = QVBoxLayout(box)
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bl.addWidget(value)
            self.info_labels[key] = value
            grid.addWidget(box, i // 2, i % 2)
        lay.addLayout(grid)

        self.preview = QLabel()
        self.preview.setObjectName("preview")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        pv = QGroupBox("Activity")
        QVBoxLayout(pv).addWidget(self.preview)
        lay.addWidget(pv)

        ev = QGroupBox("Recent events")
        el = QVBoxLayout(ev)
        self.events = QListWidget()
        self.events.setMaximumHeight(160)
        el.addWidget(self.events)
        lay.addWidget(ev)

        row = QHBoxLayout()
        dash_btn = QPushButton("Open web dashboard")
        dash_btn.clicked.connect(self.open_dashboard)
        row.addWidget(dash_btn)
        row.addStretch()
        lay.addLayout(row)
        self.tabs.addTab(page, "Dashboard")

    def _refresh_dashboard(self) -> None:
        s = self.state
        if s is None or self.ws_status != "open":
            label = {"gave_up": "✕ Backend unreachable — use the tray menu or restart the app",
                     "auth_failed": "✕ Backend rejected the API token"}.get(self.ws_status, "◌ Connecting to backend…")
            self.status_label.setText(label)
            self.toggle.setEnabled(False)
            self.tray.setToolTip("Discord-Pro — backend offline")
            self.tray.setIcon(status_icon("OFFLINE"))
            return
        p, d = s["presence"], s["discord"]
        icon, text = describe_state(s)
        self.status_label.setText(f"{icon} {text}")
        self.toggle.setEnabled(not self._busy)
        self.toggle.setChecked(p["enabled"])
        self.toggle.setText("Presence ON" if p["enabled"] else "Presence OFF")
        self.toggle.setStyleSheet(TOGGLE_ON if p["enabled"] else TOGGLE_OFF)
        err = d.get("last_error")
        self.error_label.show_error(f"{err['message']} ({err['code']})" if err else None)
        self.apply_button.setVisible(p["enabled"] and not p["applied"] and p["connection_state"] == "CONNECTED")
        self.setup_box.setVisible(not s["configuration"]["configured"] and not p["enabled"])
        preset = next((x for x in self.presets if x["id"] == p["active_preset_id"]), None)
        self.info_labels["discord"].setText(
            f"{'Connected' if d['rpc_connected'] else 'Not connected'}\nState: {p['connection_state']}")
        auto = "\nAuto-switch by process is on" if s["auto_switch"]["enabled"] else ""
        self.info_labels["preset"].setText((preset["name"] if preset else "Custom activity") + auto)
        applied = "" if not p["enabled"] else ("\n✓ saved settings applied" if p["applied"] else "\n! saved changes not applied")
        self.info_labels["updated"].setText(fmt_ago(p["last_successful_update"]) + applied)
        self.info_labels["backend"].setText(
            f"v{s['application']['version']} · {s['server']['websocket_connected_clients']} live client(s)\n"
            f"{'LAN access on' if s['server']['lan_active'] else 'This computer only'}"
            + ("\nRestart the backend to apply network changes" if s["server"]["restart_required"] else ""))
        act = p["current_activity"]
        source = "Published on Discord" if act else "Saved (not published)"
        act = act or (self.config or {}).get("presence") or {}
        lines = [source, ""]
        for key in ("details", "state"):
            if act.get(key):
                lines.append(act[key])
        if act.get("large_image"):
            lines.append(f"Image: {act['large_image']}")
        for b in act.get("buttons") or []:
            lines.append(f"[{b['label']}] → {b['url']}")
        self.preview.setText("\n".join(lines) if len(lines) > 2 else source + "\n\n(empty)")
        events = list(reversed(s.get("recent_events") or []))[:15]
        if [self.events.item(i).text() for i in range(self.events.count())] != [self._event_text(e) for e in events]:
            self.events.clear()
            for e in events:
                self.events.addItem(self._event_text(e))
        self.tray.setIcon(status_icon(p["connection_state"] if p["enabled"] else "DISCONNECTED"))
        self.setWindowIcon(self.tray.icon())
        self.tray.setToolTip(f"Discord-Pro — {text}")
        self.tray_toggle.setText("Turn presence OFF" if p["enabled"] else "Turn presence ON")
        self.tray_toggle.setEnabled(not self._busy)

    @staticmethod
    def _event_text(e: dict[str, Any]) -> str:
        return f"{time.strftime('%H:%M:%S', time.localtime(e['at']))}  {'✓' if e['success'] else '✕'} {e['message']}  ({e['source']})"

    def on_toggle(self) -> None:
        if self.state is None:
            return
        enabled = self.state["presence"]["enabled"]
        self._presence_command("POST", f"/api/v1/presence/{'disable' if enabled else 'enable'}",
                               "Presence turned off" if enabled else "Presence is live")

    def _presence_command(self, method: str, path: str, ok_text: str, body: Any = None) -> None:
        self._busy = True
        self._refresh_dashboard()

        def done(status: int, resp: Any) -> None:
            self._busy = False
            if isinstance(resp, dict) and isinstance(resp.get("state"), dict):
                self._apply_state(resp["state"])
            result = (resp or {}).get("result") if isinstance(resp, dict) else None
            if result and result.get("success"):
                self.notify(result.get("message") or ok_text)
            elif result:
                pending = " — still retrying in the background" if result.get("pending") else ""
                self.notify(f"{result['error']['message']}{pending}", error=not result.get("pending"))
            else:
                self.notify(error_message(status, resp), error=True)
            self._refresh_dashboard()

        self.rest.request(method, path, body, done)

    # -------------------------------------------------------------------- apps
    def _build_apps(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        info = QLabel("Click an app to show it on Discord. Only one app is on at a time; click it again to turn it off.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.app_list = QListWidget()
        self.app_list.setObjectName("app_list")
        self.app_list.setViewMode(QListView.ViewMode.IconMode)
        self.app_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.app_list.setMovement(QListView.Movement.Static)
        self.app_list.setWrapping(True)
        self.app_list.setSpacing(10)
        self.app_list.setIconSize(QSize(64, 64))
        self.app_list.setGridSize(QSize(140, 130))
        self.app_list.setWordWrap(True)
        self.app_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.app_list.itemClicked.connect(lambda item: self.toggle_app(item.data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.app_list, 1)
        self.app_empty = QLabel("No apps yet. Create a preset in the Presets tab — every preset appears here as an app.")
        self.app_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.app_empty.setWordWrap(True)
        lay.addWidget(self.app_empty)
        self.tabs.addTab(page, "App")

    def _active_app_id(self) -> str | None:
        p = (self.state or {}).get("presence", {})
        return p.get("active_app_id") if p.get("enabled") else None

    def _render_apps(self) -> None:
        self.app_list.clear()
        on_id = self._active_app_id()
        for p in self.presets:
            on = p["id"] == on_id
            icon = (self.icons.get(p["icon"]) if p.get("icon") else None) or letter_icon(p["name"])
            item = QListWidgetItem(icon, f"{p['name']}\n{'● ON' if on else 'OFF'}")
            item.setData(Qt.ItemDataRole.UserRole, p["id"])
            item.setToolTip(f"Turn {p['name']} off" if on else f"Show {p['name']} on Discord")
            if on:
                item.setBackground(QColor(31, 138, 76, 60))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.app_list.addItem(item)
        self.app_empty.setVisible(not self.presets)

    def toggle_app(self, preset_id: str) -> None:
        if self._busy or self.state is None:
            return
        p = next((x for x in self.presets if x["id"] == preset_id), None)
        if p is None:
            return
        on = preset_id == self._active_app_id()
        self._presence_command("POST", f"/api/v1/apps/{preset_id}/toggle",
                               f"{p['name']} turned off" if on else f"{p['name']} is live")

    # ---------------------------------------------------------------- activity
    def _build_activity(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        info = QLabel("<b>Save</b> stores the activity. <b>Save &amp; apply</b> also publishes it to Discord when presence is on.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.remote_banner = QLabel("The saved activity was changed elsewhere (e.g. in the browser).")
        self.remote_banner.setStyleSheet("background: palette(alternate-base); padding: 6px;")
        self.remote_banner.hide()
        lay.addWidget(self.remote_banner)
        head = QFormLayout()
        self.override_id = QLineEdit()
        self.override_id.setObjectName("override_client_id")
        self.override_id.setPlaceholderText("optional — empty uses the default Application ID")
        head.addRow("Application ID override", self.override_id)
        lay.addLayout(head)
        self.activity_form = ActivityForm()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.activity_form)
        lay.addWidget(scroll, 1)
        self.activity_error = ErrorLabel()
        lay.addWidget(self.activity_error)
        row = QHBoxLayout()
        self.save_apply_btn = QPushButton("Save && apply")
        self.save_apply_btn.setObjectName("save_apply")
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("save_only")
        reset = QPushButton("Reset")
        self.save_apply_btn.clicked.connect(lambda: self.save_activity(True))
        self.save_btn.clicked.connect(lambda: self.save_activity(False))
        reset.clicked.connect(self.load_activity)
        row.addWidget(self.save_apply_btn)
        row.addWidget(self.save_btn)
        row.addWidget(reset)
        row.addStretch()
        lay.addLayout(row)
        self._override_initial = ""
        self.tabs.addTab(page, "Activity")

    def activity_dirty(self) -> bool:
        return self.activity_form.is_dirty() or self.override_id.text().strip() != self._override_initial

    def load_activity(self) -> None:
        cfg = self.config or {}
        self.activity_form.set(cfg.get("presence"))
        self.override_id.setText(cfg.get("presence_client_id", ""))
        self._override_initial = self.override_id.text().strip()
        self.activity_error.show_error(None)
        self.remote_banner.hide()

    def save_activity(self, apply: bool) -> None:
        body = {"activity": self.activity_form.read(), "apply": apply, "client_id": self.override_id.text().strip()}
        self.save_apply_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

        def done(status: int, resp: Any) -> None:
            self.save_apply_btn.setEnabled(True)
            self.save_btn.setEnabled(True)
            if isinstance(resp, dict) and isinstance(resp.get("state"), dict):
                self._apply_state(resp["state"])
            result = (resp or {}).get("result") if isinstance(resp, dict) else None
            if result is not None:
                self.activity_form.mark_clean()
                self._override_initial = self.override_id.text().strip()
                self.remote_banner.hide()
                self.activity_error.show_error(None if result.get("success") else result["error"]["message"])
                self.notify(result.get("message") or ("Saved and applied" if apply else "Saved"),
                            error=not result.get("success"))
                return
            details = error_details(resp)
            unmatched = self.activity_form.set_errors(details)
            self.activity_error.show_error("; ".join(unmatched) if unmatched else
                                           (None if details else error_message(status, resp)))
            self.notify("Please fix the highlighted fields", error=True)

        self.rest.put("/api/v1/presence", body, done)

    # ----------------------------------------------------------------- presets
    def _build_presets(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(QLabel("Activating a preset copies it into the current activity "
                             "(and publishes it if presence is on)."))
        self.preset_list = QListWidget()
        self.preset_list.setObjectName("preset_list")
        self.preset_list.itemDoubleClicked.connect(lambda _i: self.edit_preset())
        lay.addWidget(self.preset_list, 1)
        self.preset_empty = QLabel("No presets yet. Create one to switch activities with one click.")
        self.preset_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.preset_empty)
        row = QHBoxLayout()
        for text, slot in [("New", self.new_preset), ("Edit", self.edit_preset), ("Duplicate", self.duplicate_preset),
                           ("Delete", self.delete_preset), ("Activate", lambda: self.activate_preset(False)),
                           ("Activate && turn on", lambda: self.activate_preset(True))]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        self.tabs.addTab(page, "Presets")

    def _selected_preset(self) -> dict[str, Any] | None:
        item = self.preset_list.currentItem()
        if item is None:
            self.notify("Select a preset first", error=True)
            return None
        pid = item.data(Qt.ItemDataRole.UserRole)
        return next((p for p in self.presets if p["id"] == pid), None)

    def _render_presets(self) -> None:
        current = self.preset_list.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.preset_list.clear()
        active = (self.state or {}).get("presence", {}).get("active_preset_id")
        for p in self.presets:
            a = p["activity"]
            summary = " · ".join(x for x in (a.get("details"), a.get("state")) if x) or "(no text)"
            procs = f"   [auto: {', '.join(p['process_names'])}]" if p["process_names"] else ""
            mark = "✓ " if p["id"] == active else "   "
            item = QListWidgetItem(f"{mark}{p['name']} — {summary}{procs}")
            item.setData(Qt.ItemDataRole.UserRole, p["id"])
            self.preset_list.addItem(item)
            if p["id"] == selected:
                self.preset_list.setCurrentItem(item)
        self.preset_empty.setVisible(not self.presets)
        self._render_apps()

    def new_preset(self) -> None:
        PresetDialog(self.rest, None, (self.config or {}).get("presence"), lambda: self.notify("Preset created"), self).exec()

    def edit_preset(self) -> None:
        p = self._selected_preset()
        if p:
            PresetDialog(self.rest, p, None, lambda: self.notify("Preset saved"), self).exec()

    def duplicate_preset(self) -> None:
        p = self._selected_preset()
        if p:
            self.rest.post(f"/api/v1/presets/{p['id']}/duplicate", None,
                           lambda s, r: self.notify("Preset duplicated" if s == 201 else error_message(s, r), error=s != 201))

    def delete_preset(self) -> None:
        p = self._selected_preset()
        if not p:
            return
        if QMessageBox.question(self, "Delete preset", f"Delete preset “{p['name']}”? This cannot be undone.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.rest.delete(f"/api/v1/presets/{p['id']}",
                         lambda s, r: self.notify("Preset deleted" if s == 204 else error_message(s, r), error=s != 204))

    def activate_preset(self, enable: bool) -> None:
        p = self._selected_preset()
        if p:
            self._presence_command("POST", f"/api/v1/presets/{p['id']}/activate",
                                   "Preset is live" if enable else "Preset activated", {"enable": enable})

    # ---------------------------------------------------------------- settings
    def _build_settings(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        pres = QGroupBox("Presence")
        pf = QFormLayout(pres)
        self.start_on = QCheckBox("Turn presence on when the backend starts")
        self.auto_switch = QCheckBox("Activate presets when their programs are running")
        self.poll = QDoubleSpinBox()
        self.poll.setRange(1, 60)
        self.poll.setSuffix(" s")
        pf.addRow("Start enabled", self.start_on)
        pf.addRow("Auto-switch", self.auto_switch)
        pf.addRow("Check every", self.poll)
        lay.addWidget(pres)

        rec = QGroupBox("Reconnect")
        rf = QFormLayout(rec)
        self.attempts = QSpinBox()
        self.attempts.setRange(1, 1000)
        self.max_delay = QDoubleSpinBox()
        self.max_delay.setRange(1, 3600)
        self.max_delay.setSuffix(" s")
        rf.addRow("Max attempts", self.attempts)
        rf.addRow("Max delay", self.max_delay)
        lay.addWidget(rec)

        net = QGroupBox("Network")
        nf = QFormLayout(net)
        self.allow_lan = QCheckBox("Allow other devices on this Wi-Fi (requires restart)")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        nf.addRow("LAN access", self.allow_lan)
        nf.addRow("Port", self.port)
        self.access_code = QLineEdit()
        self.access_code.setReadOnly(True)
        self.access_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.access_code.setObjectName("access_code")
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.access_code.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.access_code.text()))
        rotate = QPushButton("New code")
        rotate.clicked.connect(self.rotate_code)
        code_row = QHBoxLayout()
        code_row.addWidget(self.access_code, 1)
        for b in (show, copy, rotate):
            code_row.addWidget(b)
        nf.addRow("Dashboard access code", code_row)
        hint = QLabel("Other devices sign in to the web dashboard with this code. "
                      "“New code” signs out every browser.")
        hint.setWordWrap(True)
        nf.addRow("", hint)
        lay.addWidget(net)

        self.settings_error = ErrorLabel()
        lay.addWidget(self.settings_error)
        row = QHBoxLayout()
        save = QPushButton("Save settings")
        save.setObjectName("save_settings")
        save.clicked.connect(self.save_settings)
        wiz = QPushButton("Run setup wizard")
        wiz.clicked.connect(self.open_wizard)
        diag = QPushButton("Diagnostics")
        diag.clicked.connect(self.show_diagnostics)
        stop = QPushButton("Stop backend && exit")
        stop.clicked.connect(self.exit_and_stop_backend)
        for b in (save, wiz, diag, stop):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.diag_text = QPlainTextEdit()
        self.diag_text.setReadOnly(True)
        self.diag_text.hide()
        lay.addWidget(self.diag_text)
        lay.addStretch()
        self._settings_initial: dict[str, Any] | None = None
        self.tabs.addTab(page, "Settings")

    def _settings_patch(self) -> dict[str, Any]:
        return {
            "application": {"start_with_presence_enabled": self.start_on.isChecked()},
            "auto_switch": {"enabled": self.auto_switch.isChecked(), "poll_interval": self.poll.value()},
            "rpc": {"reconnect_max_attempts": self.attempts.value(), "reconnect_max_delay": self.max_delay.value()},
            "server": {"allow_lan": self.allow_lan.isChecked(), "port": self.port.value()},
        }

    def load_settings(self, force: bool = False) -> None:
        c = self.config
        if c is None:
            return
        if not force and self._settings_initial is not None and self._settings_patch() != self._settings_initial:
            return  # keep unsaved edits
        self.start_on.setChecked(c["application"]["start_with_presence_enabled"])
        self.auto_switch.setChecked(c["auto_switch"]["enabled"])
        self.poll.setValue(c["auto_switch"]["poll_interval"])
        self.attempts.setValue(c["rpc"]["reconnect_max_attempts"])
        self.max_delay.setValue(c["rpc"]["reconnect_max_delay"])
        self.allow_lan.setChecked(c["server"]["allow_lan"])
        self.port.setValue(c["server"]["port"])
        self._settings_initial = self._settings_patch()

    def save_settings(self) -> None:
        patch = self._settings_patch()
        if patch["server"]["allow_lan"] and not (self.config or {}).get("server", {}).get("allow_lan"):
            answer = QMessageBox.warning(
                self, "Enable LAN access?",
                "Devices on your local network will be able to open the dashboard (they need the access code). "
                "Only enable this on a trusted network. The backend must be restarted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        def done(status: int, resp: Any) -> None:
            if status == 200:
                self.config = resp
                self._settings_initial = None
                self.load_settings(force=True)
                self.settings_error.show_error(None)
                self.notify("Settings saved")
            else:
                details = error_details(resp)
                self.settings_error.show_error("; ".join(f"{d['field']}: {d['message']}" for d in details)
                                               or error_message(status, resp))

        self.rest.put("/api/v1/config", patch, done)

    def load_access_code(self) -> None:
        self.rest.get("/api/v1/auth/access-code",
                      lambda s, r: self.access_code.setText(r["access_code"]) if s == 200 else None)

    def rotate_code(self) -> None:
        if QMessageBox.question(self, "New access code", "Create a new code and sign out every browser?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.rest.post("/api/v1/auth/access-code/rotate", None,
                       lambda s, r: (self.access_code.setText(r["access_code"]), self.notify("New access code created"))
                       if s == 200 else self.notify(error_message(s, r), error=True))

    def show_diagnostics(self) -> None:
        import json

        def done(status: int, resp: Any) -> None:
            self.diag_text.setPlainText(json.dumps(resp, indent=2) if status == 200 else error_message(status, resp))
            self.diag_text.show()

        self.rest.get("/api/v1/diagnostics", done)

    # ------------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(status_icon("DISCONNECTED"), self)
        menu = QMenu()
        self.tray_show = QAction("Show window", self)
        self.tray_show.triggered.connect(self.show_window)
        self.tray_hide = QAction("Hide window", self)
        self.tray_hide.triggered.connect(self.hide)
        self.tray_toggle = QAction("Turn presence ON", self)
        self.tray_toggle.triggered.connect(self.on_toggle)
        open_dash = QAction("Open web dashboard", self)
        open_dash.triggered.connect(self.open_dashboard)
        exit_gui = QAction("Exit (keep presence running)", self)
        exit_gui.triggered.connect(self.exit_gui)
        exit_all = QAction("Exit and stop backend", self)
        exit_all.triggered.connect(self.exit_and_stop_backend)
        for a in (self.tray_show, self.tray_hide, None, self.tray_toggle, open_dash, None, exit_gui, exit_all):
            if a is None:
                menu.addSeparator()
            else:
                menu.addAction(a)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setToolTip("Discord-Pro")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def tray_available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        # Re-sync in case events were missed while hidden.
        self.rest.get("/api/v1/state", lambda s, r: self._apply_state(r) if s == 200 else None)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._quitting:
            event.accept()
            return
        event.ignore()
        if self.tray_available():
            self.hide()  # backend keeps running
            if not self._tray_hint_shown:
                self.tray.showMessage("Discord-Pro is still running",
                                      "Presence keeps running in the background. Use the tray icon to reopen or exit.",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
                self._tray_hint_shown = True
        else:
            # Without a tray there would be no way back to a hidden window.
            self.showMinimized()
            self.notify("No system tray available: minimized instead. Use Settings → Stop backend & exit to quit.")

    def exit_gui(self) -> None:
        self._quitting = True
        self.live.stop()
        self.tray.hide()
        QApplication.quit()

    def exit_and_stop_backend(self) -> None:
        def done(status: int, resp: Any) -> None:
            if status != 200:
                QMessageBox.warning(self, "Stop backend", f"Could not stop the backend: {error_message(status, resp)}")
            self.exit_gui()

        self.rest.post("/api/v1/shutdown", None, done)

    # ---------------------------------------------------------------- events
    def on_ws_status(self, status: str, attempt: int) -> None:
        self.ws_status = status
        if status == "open":
            self.statusBar().showMessage("Live connection to backend established", 3000)
            # The worker may have discovered a new port/token after a backend restart.
            self.rest.info = self.live.info
        elif status == "waiting":
            self.statusBar().showMessage(f"Backend connection lost — retrying (attempt {attempt})")
        elif status == "gave_up":
            self.statusBar().showMessage("Backend unreachable")
        self._refresh_dashboard()

    def on_event(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        payload = msg.get("payload")
        if kind == "state.snapshot":
            self.config = payload["config"]
            self.presets = payload["presets"]
            self._apply_state(payload["state"], force=True)
            self._config_changed()
            self._render_presets()
            self.load_access_code()
        elif kind == "state.updated":
            self._apply_state(payload)
        elif kind == "config.updated":
            self.config = payload
            self._config_changed()
        elif kind == "presets.updated":
            self.presets = payload
            self._render_presets()

    def _apply_state(self, state: dict[str, Any], force: bool = False) -> None:
        cur = self.state
        if (not force and cur is not None and state.get("revision", 0) < cur.get("revision", 0)
                and state["application"]["started_at"] == cur["application"]["started_at"]):
            return  # stale response
        self.state = state
        self._refresh_dashboard()
        self._render_presets()
        self.state_applied.emit(state)

    def _config_changed(self) -> None:
        if self.activity_dirty():
            self.remote_banner.show()
        else:
            self.load_activity()
        self.load_settings()
        self._refresh_dashboard()

    # ----------------------------------------------------------------- misc
    def notify(self, text: str, error: bool = False) -> None:
        self.statusBar().showMessage(("⚠ " if error else "") + text, 8000)
        self.last_notification = (text, error, time.time())

    def open_wizard(self) -> None:
        SetupWizard(self.rest, self.config, self).exec()

    def open_dashboard(self) -> None:
        def done(status: int, resp: Any) -> None:
            if status == 200:
                webbrowser.open(self.rest.info.url + resp["path"])
            else:
                self.notify(error_message(status, resp), error=True)

        self.rest.post("/api/v1/auth/login-link", None, done)
