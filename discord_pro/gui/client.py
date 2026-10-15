"""Backend access for the GUI: async REST (Qt network) + WebSocket thread."""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from discord_pro.local_client import BackendInfo

Callback = Callable[[int, Any], None]


class RestClient(QObject):
    """Non-blocking REST calls; callbacks run on the GUI thread."""

    def __init__(self, info: BackendInfo, parent: QObject | None = None):
        super().__init__(parent)
        self.info = info
        self._nam = QNetworkAccessManager(self)
        self._pending: set[QNetworkReply] = set()

    def request(self, method: str, path: str, body: Any = None, callback: Callback | None = None,
                timeout_ms: int = 30000) -> None:
        req = QNetworkRequest(QUrl(self.info.url + path))
        req.setRawHeader(b"Authorization", f"Bearer {self.info.token}".encode())
        req.setTransferTimeout(timeout_ms)
        data = QByteArray()
        if body is not None:
            req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            data = QByteArray(json.dumps(body).encode())
        reply = self._nam.sendCustomRequest(req, method.encode(), data)
        self._pending.add(reply)

        def finished() -> None:
            self._pending.discard(reply)
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            raw = bytes(reply.readAll())
            payload: Any = None
            if raw:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
            if status == 0:
                payload = {"error": {"code": "network_error", "message": "Backend unreachable"}}
            reply.deleteLater()
            if callback is not None:
                callback(int(status), payload)

        reply.finished.connect(finished)

    def get(self, path: str, callback: Callback | None = None) -> None:
        self.request("GET", path, None, callback)

    def post(self, path: str, body: Any = None, callback: Callback | None = None) -> None:
        self.request("POST", path, body, callback)

    def put(self, path: str, body: Any, callback: Callback | None = None) -> None:
        self.request("PUT", path, body, callback)

    def delete(self, path: str, callback: Callback | None = None) -> None:
        self.request("DELETE", path, None, callback)


class _WsWorker(QObject):
    event = Signal(dict)
    status = Signal(str, int)  # "connecting" | "open" | "waiting" | "gave_up" | "auth_failed", attempt

    MAX_ATTEMPTS = 30

    def __init__(self, info: BackendInfo):
        super().__init__()
        self.info = info
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ws: Any = None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def retry_now(self) -> None:
        self._wake.set()

    def run(self) -> None:
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        from websockets.sync.client import connect

        attempt = 0
        while not self._stop.is_set():
            url = self.info.url.replace("http://", "ws://") + "/api/v1/ws"
            self.status.emit("connecting", attempt)
            try:
                with connect(url, additional_headers={"Authorization": f"Bearer {self.info.token}"},
                             open_timeout=5, max_size=2 ** 22) as ws:
                    self._ws = ws
                    attempt = 0
                    self.status.emit("open", 0)
                    for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            self.event.emit(msg)
            except ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code == 4401:
                    self.status.emit("auth_failed", attempt)
                    self._refresh_info()
            except InvalidStatus:
                self._refresh_info()
            except Exception:
                pass
            finally:
                self._ws = None
            if self._stop.is_set():
                break
            attempt += 1
            if attempt > self.MAX_ATTEMPTS:
                self.status.emit("gave_up", attempt)
                self._wake.clear()
                self._wake.wait()  # until retry_now() or stop()
                attempt = 0
                self._refresh_info()
                continue
            delay = min(30.0, 2 ** (attempt - 1)) * (0.8 + 0.4 * random.random())
            self.status.emit("waiting", attempt)
            self._wake.clear()
            if self._wake.wait(delay):
                attempt = 0
            self._refresh_info()

    def _refresh_info(self) -> None:
        """The backend may have restarted on another port or with a new token."""
        from discord_pro.local_client import discover

        info = discover()
        if info is not None:
            self.info = info


class LiveChannel(QObject):
    """Owns the WebSocket worker thread and re-emits its signals on the GUI thread."""

    event = Signal(dict)
    status = Signal(str, int)

    def __init__(self, info: BackendInfo, parent: QObject | None = None):
        super().__init__(parent)
        self._thread = QThread()
        self._worker = _WsWorker(info)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self.event)
        self._worker.status.connect(self.status)

    @property
    def info(self) -> BackendInfo:
        return self._worker.info

    def start(self) -> None:
        self._thread.start()

    def retry_now(self) -> None:
        self._worker.retry_now()

    def stop(self) -> None:
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(3000)


def describe_state(state: dict[str, Any]) -> tuple[str, str]:
    """Return (icon, text) for the connection state; never color-only."""
    p, d = state["presence"], state["discord"]
    cs = p["connection_state"]
    if not p["enabled"] and cs == "DISCONNECTED":
        return "○", "Off (disabled)"
    if p["enabled"] and cs == "DISCONNECTED":
        return "◌", "Starting…"
    if cs == "CONNECTED":
        return "●", "Live on Discord"
    if cs == "CONNECTING":
        return "◐", "Connecting to Discord…"
    if cs == "RECONNECTING":
        nxt = d.get("next_retry_in")
        extra = f", next try in {int(nxt + 0.99)}s" if nxt is not None else ""
        return "◐", f"Reconnecting (attempt {d['reconnect_attempt']}{extra})"
    if cs == "ERROR":
        return "✕", "Error — waiting for you"
    if cs == "STOPPING":
        return "◌", "Stopping…"
    return "○", cs


def fmt_ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


__all__ = ["RestClient", "LiveChannel", "describe_state", "fmt_ago", "QTimer"]
