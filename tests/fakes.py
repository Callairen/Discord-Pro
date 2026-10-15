"""Test doubles.

* ``FakeAdapter`` / ``FakeDiscordState``: in-process RPC adapter for fast,
  deterministic unit and API tests.
* ``FakeDiscordIPC``: a Unix-socket server that speaks Discord's local IPC
  framing (op/length/JSON). It lets the *real* pypresence-based adapter be
  exercised end to end without a Discord client. It is not proof that the real
  Discord client behaves identically; see docs/TESTING.md for the manual check.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_pro.backend.rpc import RpcAdapter, RpcError


# ------------------------------------------------------------ in-process fake
@dataclass
class FakeDiscordState:
    running: bool = True
    valid_ids: set[str] | None = None
    reject_payload: bool = False
    connects: int = 0
    open_connections: int = 0
    max_open_connections: int = 0
    activity: dict[str, Any] | None = None
    set_calls: int = 0
    clear_calls: int = 0
    generation: int = 0
    log: list[str] = field(default_factory=list)

    def crash(self) -> None:
        """Discord quits: every connection dies and its activity disappears."""
        self.running = False
        self.generation += 1
        self.open_connections = 0
        self.activity = None


class FakeAdapter(RpcAdapter):
    def __init__(self, discord: FakeDiscordState):
        self.d = discord
        self.client_id = None
        self._gen: int | None = None

    async def connect(self, client_id: str) -> None:
        if not self.d.running:
            raise RpcError("transient", "discord_not_found", "Discord desktop client not detected")
        if self.d.valid_ids is not None and client_id not in self.d.valid_ids:
            raise RpcError("permanent", "invalid_client_id", "Discord rejected the Application ID")
        self.d.connects += 1
        self.d.open_connections += 1
        self.d.max_open_connections = max(self.d.max_open_connections, self.d.open_connections)
        self.client_id = client_id
        self._gen = self.d.generation
        self.d.log.append(f"connect {client_id}")

    def _check(self) -> None:
        if self._gen is None or self._gen != self.d.generation or not self.d.running:
            raise RpcError("transient", "connection_interrupted", "Connection to Discord was interrupted")

    async def set_activity(self, kwargs: dict[str, Any]) -> None:
        self._check()
        if self.d.reject_payload:
            raise RpcError("payload", "activity_rejected", "Discord rejected the activity")
        self.d.set_calls += 1
        self.d.activity = dict(kwargs)
        self.d.log.append("set")

    async def clear(self) -> None:
        self._check()
        self.d.clear_calls += 1
        self.d.activity = None
        self.d.log.append("clear")

    async def close(self) -> None:
        if self._gen is not None and self._gen == self.d.generation:
            self.d.open_connections -= 1
            self.d.activity = None
        self._gen = None
        self.client_id = None
        self.d.log.append("close")

    async def is_alive(self) -> bool:
        return self._gen is not None and self._gen == self.d.generation and self.d.running


# --------------------------------------------------------- IPC socket fake
class FakeDiscordIPC:
    """Minimal Discord IPC server on ``<dir>/discord-ipc-0``."""

    def __init__(self, directory: Path, *, valid_ids: set[str] | None = None):
        self.dir = Path(directory)
        self.path = self.dir / "discord-ipc-0"
        self.valid_ids = valid_ids
        self.reject_payload = False
        self.activities: dict[int, Any] = {}  # connection id -> activity
        self.handshakes = 0
        self._server: socket.socket | None = None
        self._conns: dict[int, socket.socket] = {}
        self._lock = threading.Lock()
        self._next = 0
        self._thread: threading.Thread | None = None

    @property
    def current_activity(self) -> Any:
        with self._lock:
            live = [a for cid, a in self.activities.items() if cid in self._conns and a]
            return live[-1] if live else None

    @property
    def live_connections(self) -> int:
        with self._lock:
            return len(self._conns)

    def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.path))
        srv.listen(8)
        self._server = srv
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Simulate Discord quitting: close listener and every connection."""
        if self._server is not None:
            try:
                self._server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._server.close()
            self._server = None
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        try:
            self.path.unlink()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        srv = self._server
        while srv is not None:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with self._lock:
                self._next += 1
                cid = self._next
                self._conns[cid] = conn
            threading.Thread(target=self._serve, args=(cid, conn), daemon=True).start()

    @staticmethod
    def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _send(conn: socket.socket, op: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        conn.sendall(struct.pack("<II", op, len(raw)) + raw)

    def _serve(self, cid: int, conn: socket.socket) -> None:
        try:
            while True:
                header = self._recv_exact(conn, 8)
                if header is None:
                    return
                op, length = struct.unpack("<II", header)
                body = self._recv_exact(conn, length)
                if body is None:
                    return
                msg = json.loads(body)
                if op == 0:
                    self.handshakes += 1
                    if self.valid_ids is not None and msg.get("client_id") not in self.valid_ids:
                        self._send(conn, 2, {"code": 4000, "message": "Invalid Client ID"})
                        return
                    self._send(conn, 1, {"cmd": "DISPATCH", "evt": "READY", "nonce": None,
                                         "data": {"v": 1, "config": {}, "user": {"id": "1", "username": "tester"}}})
                elif op == 1 and msg.get("cmd") == "SET_ACTIVITY":
                    activity = msg.get("args", {}).get("activity")
                    if activity is not None and self.reject_payload:
                        self._send(conn, 1, {"cmd": "SET_ACTIVITY", "evt": "ERROR", "nonce": msg.get("nonce"),
                                             "data": {"code": 4000, "message": "child \"activity\" fails"}})
                        continue
                    with self._lock:
                        self.activities[cid] = activity
                    self._send(conn, 1, {"cmd": "SET_ACTIVITY", "evt": None, "nonce": msg.get("nonce"),
                                         "data": activity})
                elif op == 2:
                    return
        finally:
            with self._lock:
                self._conns.pop(cid, None)
                self.activities.pop(cid, None)  # Discord drops activity on disconnect
            try:
                conn.close()
            except OSError:
                pass


def short_socket_dir(tmp_path: Path) -> Path:
    """Unix socket paths are limited to ~107 bytes; keep them short."""
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="dpipc", dir="/tmp"))
    os.chmod(d, 0o700)
    return d
