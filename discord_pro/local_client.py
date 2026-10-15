"""Discovering, starting and calling the local backend (used by GUI and CLI)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from discord_pro import paths
from discord_pro.backend.instance import read_instance_file


@dataclass
class BackendInfo:
    url: str
    port: int
    token: str
    pid: int | None


def read_token() -> str | None:
    try:
        return json.loads(paths.auth_path().read_text(encoding="utf-8")).get("api_token")
    except (OSError, ValueError):
        return None


def call(info: BackendInfo, method: str, path: str, body: Any = None, timeout: float = 30.0) -> tuple[int, Any]:
    data = None
    headers = {"Authorization": f"Bearer {info.token}"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(info.url + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, None


def healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/v1/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def discover() -> BackendInfo | None:
    """Return connection info for a running, healthy backend, else None."""
    inst = read_instance_file(paths.instance_path())
    token = read_token()
    if not inst or not token:
        return None
    url = f"http://127.0.0.1:{inst['port']}"
    if not healthy(url):
        return None
    return BackendInfo(url=url, port=inst["port"], token=token, pid=inst.get("pid"))


def backend_command() -> list[str]:
    if getattr(sys, "frozen", False):  # PyInstaller build: same executable, backend mode
        return [sys.executable, "--backend"]
    return [sys.executable, "-m", "discord_pro.backend"]


def spawn_backend() -> subprocess.Popen:
    """Start the backend as an independent background process."""
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(backend_command(), **kwargs)


def ensure_backend(timeout: float = 20.0) -> BackendInfo:
    """Connect to a running backend or start one and wait until it is healthy."""
    info = discover()
    if info is not None:
        return info
    proc = spawn_backend()
    end = time.time() + timeout
    while time.time() < end:
        info = discover()
        if info is not None:
            return info
        code = proc.poll()
        if code is not None and code != 3:  # 3 = another instance won the race; keep polling
            raise RuntimeError(f"backend exited during startup (code {code}); see {paths.log_dir() / 'backend.log'}")
        time.sleep(0.2)
    raise RuntimeError(f"backend did not become ready within {timeout:.0f}s; see {paths.log_dir() / 'backend.log'}")


def dashboard_login_url(info: BackendInfo) -> str:
    status, body = call(info, "POST", "/api/v1/auth/login-link")
    if status != 200 or not body:
        raise RuntimeError("could not create a dashboard login link")
    return info.url + body["path"]
