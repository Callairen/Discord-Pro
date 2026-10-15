"""Start a real backend process (python -m discord_pro.backend) for E2E tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
CID = "123456789012345678"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackendProcess:
    def __init__(self, home: Path, ipc_dir: Path, port: int | None = None):
        self.home = home
        self.ipc_dir = ipc_dir
        self.port = port or free_port()
        self.proc: subprocess.Popen | None = None
        self.log_file = home / "stdout.log"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["DISCORD_PRO_HOME"] = str(self.home)
        env["XDG_RUNTIME_DIR"] = str(self.ipc_dir)
        env["PYTHONPATH"] = str(ROOT)
        return env

    def write_config(self, **overrides) -> None:
        cfg = {"schema_version": 1, "server": {"port": self.port},
               "rpc": {"reconnect_base_delay": 0.2, "reconnect_max_delay": 1.0, "min_update_interval": 0.0}}
        for k, v in overrides.items():
            cfg.setdefault(k, {}).update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
        (self.home / "config.json").write_text(json.dumps(cfg))

    def start(self, wait: bool = True) -> subprocess.Popen:
        self.proc = subprocess.Popen([sys.executable, "-m", "discord_pro.backend", "--log-level", "DEBUG"],
                                     cwd=str(ROOT), env=self.env(), stdout=open(self.log_file, "ab"),
                                     stderr=subprocess.STDOUT)
        if wait:
            self.wait_healthy()
        return self.proc

    def wait_healthy(self, timeout: float = 20) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"backend exited with {self.proc.returncode}: {self.log()}")
            try:
                if httpx.get(f"{self.url}/api/v1/health", timeout=1).status_code == 200 and \
                        (self.home / "instance.json").exists():
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"backend not healthy: {self.log()}")

    def token(self) -> str:
        return json.loads((self.home / "auth.json").read_text())["api_token"]

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def log(self) -> str:
        try:
            return self.log_file.read_text(errors="replace")[-4000:]
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
