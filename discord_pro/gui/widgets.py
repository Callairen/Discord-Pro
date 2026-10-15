"""Reusable widgets: activity form, status icon painter, helpers."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

ACTIVITY_TYPES = [(0, "Playing"), (2, "Listening to"), (3, "Watching"), (5, "Competing in")]
TEXT_FIELDS = [
    ("details", "Details (line 1)", "e.g. Coding Session (2–128 characters)", 128),
    ("state", "State (line 2)", "e.g. Visual Studio Code (2–128 characters)", 128),
    ("large_image", "Large image", "Art Asset key or https:// image URL", 256),
    ("large_text", "Large image tooltip", "", 128),
    ("small_image", "Small image", "Art Asset key or https:// image URL", 256),
    ("small_text", "Small image tooltip", "", 128),
]

STATE_COLORS = {
    "CONNECTED": "#1f8a4c", "CONNECTING": "#a96500", "RECONNECTING": "#a96500",
    "ERROR": "#c0392b", "STOPPING": "#7a7f8c", "DISCONNECTED": "#7a7f8c", "OFFLINE": "#c0392b",
}


def status_icon(state: str, size: int = 64) -> QIcon:
    """Tray/window icon: a ring that is filled when presence is live."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(STATE_COLORS.get(state, "#7a7f8c"))
    p.setBrush(QColor("#5865f2"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, size - 4, size - 4, size * 0.22, size * 0.22)
    p.setBrush(color if state == "CONNECTED" else QColor("#ffffff"))
    p.setPen(QColor(color))
    r = size * 0.22
    p.drawEllipse(int(size - r * 2 - 4), int(size - r * 2 - 4), int(r * 2), int(r * 2))
    p.setPen(QColor("#ffffff"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.42))
    p.setFont(f)
    p.drawText(0, 0, int(size * 0.72), int(size * 0.72), Qt.AlignmentFlag.AlignCenter, "D")
    p.end()
    return QIcon(pix)


def letter_icon(name: str, size: int = 64) -> QIcon:
    """Fallback app logo: the first letter of the name on a rounded tile."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#5865f2"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
    p.setPen(QColor("#ffffff"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.5))
    p.setFont(f)
    p.drawText(0, 0, size, size, Qt.AlignmentFlag.AlignCenter, (name.strip()[:1] or "?").upper())
    p.end()
    return QIcon(pix)


class IconCache(QObject):
    """Downloads app logos (https URLs) once; ``loaded`` fires when one arrives."""

    loaded = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._icons: dict[str, QIcon | None] = {}  # None = downloading or failed

    def get(self, url: str) -> QIcon | None:
        if url not in self._icons:
            self._icons[url] = None
            req = QNetworkRequest(QUrl(url))
            req.setTransferTimeout(15000)
            reply = self._nam.get(req)
            reply.finished.connect(lambda: self._finished(url, reply))
        return self._icons[url]

    def _finished(self, url: str, reply: QNetworkReply) -> None:
        pix = QPixmap()
        if reply.error() == QNetworkReply.NetworkError.NoError and pix.loadFromData(bytes(reply.readAll())):
            self._icons[url] = QIcon(pix)
            self.loaded.emit(url)
        reply.deleteLater()


class ErrorLabel(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet("color: #c0392b;")
        self.setWordWrap(True)
        self.hide()

    def show_error(self, text: str | None) -> None:
        if text:
            self.setText("⚠ " + text)
            self.show()
        else:
            self.clear()
            self.hide()


class ActivityForm(QWidget):
    """Editable Rich Presence activity. ``read()`` returns the API payload."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.inputs: dict[str, Any] = {}
        self.errors: dict[str, ErrorLabel] = {}
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        type_box = QComboBox()
        for value, label in ACTIVITY_TYPES:
            type_box.addItem(label, value)
        type_box.currentIndexChanged.connect(self.changed)
        self.inputs["activity_type"] = type_box
        form.addRow("Activity type", self._with_error("activity_type", type_box))

        for key, label, hint, max_len in TEXT_FIELDS:
            edit = QLineEdit()
            edit.setObjectName(f"field_{key}")
            edit.setPlaceholderText(hint)
            edit.setMaxLength(max_len)
            edit.textChanged.connect(self.changed)
            self.inputs[key] = edit
            form.addRow(label, self._with_error(key, edit))

        for i in (1, 2):
            label_edit, url_edit = QLineEdit(), QLineEdit()
            label_edit.setPlaceholderText("Label (max 32)")
            label_edit.setMaxLength(32)
            url_edit.setPlaceholderText("https://…")
            url_edit.setMaxLength(512)
            label_edit.textChanged.connect(self.changed)
            url_edit.textChanged.connect(self.changed)
            self.inputs[f"button{i}_label"] = label_edit
            self.inputs[f"button{i}_url"] = url_edit
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(label_edit)
            lay.addWidget(url_edit)
            form.addRow(f"Button {i}", self._with_error(f"button{i}", box))
        hint = QLabel("Buttons are shown to other people, not on your own profile.")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow("", hint)

        elapsed = QCheckBox("Show time since activation")
        elapsed.toggled.connect(self.changed)
        self.inputs["show_elapsed"] = elapsed
        form.addRow("Elapsed timer", elapsed)
        self.setLayout(form)
        self._initial: dict[str, Any] = {}
        self.set({})

    def _with_error(self, key: str, widget: QWidget) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(widget)
        err = ErrorLabel()
        self.errors[key] = err
        lay.addWidget(err)
        return box

    def set(self, activity: dict[str, Any] | None) -> None:
        a = activity or {}
        self.blockSignals(True)
        box = self.inputs["activity_type"]
        idx = box.findData(int(a.get("activity_type", 0) or 0))
        box.setCurrentIndex(max(0, idx))
        for key, *_ in TEXT_FIELDS:
            self.inputs[key].setText(a.get(key) or "")
        buttons = a.get("buttons") or []
        for i in (1, 2):
            b = buttons[i - 1] if len(buttons) >= i else {}
            self.inputs[f"button{i}_label"].setText(b.get("label", ""))
            self.inputs[f"button{i}_url"].setText(b.get("url", ""))
        self.inputs["show_elapsed"].setChecked(a.get("show_elapsed", True) is not False)
        self.blockSignals(False)
        self._initial = self.read()
        self.set_errors([])

    def read(self) -> dict[str, Any]:
        out: dict[str, Any] = {"activity_type": self.inputs["activity_type"].currentData()}
        for key, *_ in TEXT_FIELDS:
            out[key] = self.inputs[key].text().strip()
        out["buttons"] = []
        for i in (1, 2):
            label = self.inputs[f"button{i}_label"].text().strip()
            url = self.inputs[f"button{i}_url"].text().strip()
            if label or url:
                out["buttons"].append({"label": label, "url": url})
        out["show_elapsed"] = self.inputs["show_elapsed"].isChecked()
        return out

    def is_dirty(self) -> bool:
        return self.read() != self._initial

    def mark_clean(self) -> None:
        self._initial = self.read()

    def set_errors(self, details: list[dict[str, Any]]) -> list[str]:
        """Show server validation errors next to fields; return unmatched messages."""
        for err in self.errors.values():
            err.show_error(None)
        unmatched = []
        for d in details or []:
            field = str(d.get("field", ""))
            for prefix in ("activity.", "presence.", "body."):
                if field.startswith(prefix):
                    field = field[len(prefix):]
            msg = str(d.get("message", "")).removeprefix("Value error, ")
            if field.startswith("buttons."):
                parts = field.split(".")
                key = f"button{int(parts[1]) + 1}" if len(parts) > 1 and parts[1].isdigit() else "button1"
            elif field in ("buttons", "activity"):
                key = "button1" if field == "buttons" else "details"
            else:
                key = field.split(".")[0]
            target = self.errors.get(key)
            if target is not None:
                target.show_error(msg)
            else:
                unmatched.append(f"{d.get('field')}: {msg}")
        return unmatched


def error_message(status: int, body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error") or (body.get("result") or {}).get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("code") or f"HTTP {status}"
    return f"HTTP {status}"


def error_details(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("details") or []
    return []
