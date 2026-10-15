"""End-to-end tests against a real backend process and the IPC simulator."""

import json
import subprocess
import sys
import time

import httpx
import pytest
import websockets.sync.client as wsc

from harness import CID
from harness import BackendProcess

pytestmark = pytest.mark.e2e

ACTIVITY = {"details": "E2E details", "state": "E2E state", "large_image": "logo"}


def setup_backend(b):
    r = httpx.post(f"{b.url}/api/v1/setup/complete", headers=b.headers(),
                   json={"client_id": CID, "activity": ACTIVITY}, timeout=10)
    assert r.status_code == 200, r.text


def wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not met")


def test_ac01_startup_health_and_instance_file(backend):
    r = httpx.get(f"{backend.url}/api/v1/health")
    assert r.json()["status"] == "ok"
    inst = json.loads((backend.home / "instance.json").read_text())
    assert inst["port"] == backend.port and inst["pid"] == backend.proc.pid
    assert inst["bind_host"] == "127.0.0.1"  # localhost by default


def test_ac04_ac05_enable_disable_over_real_ipc(backend, ipc):
    setup_backend(backend)
    r = httpx.post(f"{backend.url}/api/v1/presence/enable", headers=backend.headers(), timeout=30)
    assert r.status_code == 200, r.text
    wait(lambda: ipc.current_activity is not None)
    assert ipc.current_activity["details"] == "E2E details"
    r = httpx.post(f"{backend.url}/api/v1/presence/disable", headers=backend.headers(), timeout=30)
    assert r.status_code == 200 and r.json()["result"]["data"]["cleared"] == "clear"
    wait(lambda: ipc.live_connections == 0)
    assert ipc.current_activity is None


def test_ac08_two_clients_enable_single_connection(backend, ipc):
    setup_backend(backend)
    url = f"ws://127.0.0.1:{backend.port}/api/v1/ws"
    hdr = backend.headers()
    with wsc.connect(url, additional_headers=hdr) as a, wsc.connect(url, additional_headers=hdr) as b:
        for ws in (a, b):
            ws.recv(timeout=5); ws.recv(timeout=5)
        a.send(json.dumps({"type": "presence.enable", "request_id": "a1"}))
        b.send(json.dumps({"type": "presence.enable", "request_id": "b1"}))
        httpx.post(f"{backend.url}/api/v1/presence/enable", headers=hdr, timeout=30)
        wait(lambda: ipc.current_activity is not None)
        time.sleep(0.5)
    assert ipc.handshakes == 1 and ipc.live_connections == 1


def test_ac14_ac15_discord_restart_recovery(backend, ipc):
    setup_backend(backend)
    httpx.post(f"{backend.url}/api/v1/presence/enable", headers=backend.headers(), timeout=30)
    wait(lambda: ipc.current_activity is not None)
    ipc.stop()
    st = lambda: httpx.get(f"{backend.url}/api/v1/state", headers=backend.headers()).json()  # noqa: E731
    wait(lambda: st()["presence"]["connection_state"] in ("RECONNECTING", "CONNECTING"))
    assert st()["presence"]["enabled"] is True  # intent preserved, connection lost
    ipc.start()
    wait(lambda: st()["presence"]["connection_state"] == "CONNECTED", timeout=15)
    wait(lambda: ipc.current_activity is not None)


def test_duplicate_backend_refused(backend, tmp_path):
    second = BackendProcess(backend.home, backend.ipc_dir, port=backend.port)
    proc = second.start(wait=False)
    assert proc.wait(15) == 3  # EXIT_ALREADY_RUNNING
    assert httpx.get(f"{backend.url}/api/v1/health").status_code == 200


def test_ac16_ac20_shutdown_cleans_up_and_restart_restores(backend, ipc):
    setup_backend(backend)
    hdr = backend.headers()
    httpx.post(f"{backend.url}/api/v1/presets", headers=hdr, json={"name": "Persist", "activity": ACTIVITY})
    httpx.post(f"{backend.url}/api/v1/presence/enable", headers=hdr, timeout=30)
    wait(lambda: ipc.current_activity is not None)
    assert httpx.post(f"{backend.url}/api/v1/shutdown", headers=hdr).status_code == 200
    assert backend.proc.wait(15) == 0
    assert not (backend.home / "instance.json").exists()
    wait(lambda: ipc.live_connections == 0)
    assert ipc.current_activity is None
    backend.start()
    cfg = httpx.get(f"{backend.url}/api/v1/config", headers=hdr).json()
    assert cfg["application"]["client_id"] == CID and cfg["presence"]["details"] == "E2E details"
    assert [p["name"] for p in httpx.get(f"{backend.url}/api/v1/presets", headers=hdr).json()] == ["Persist"]


def test_sigterm_graceful(backend, ipc):
    setup_backend(backend)
    httpx.post(f"{backend.url}/api/v1/presence/enable", headers=backend.headers(), timeout=30)
    wait(lambda: ipc.current_activity is not None)
    backend.proc.terminate()
    # uvicorn re-raises the captured signal after a graceful shutdown
    assert backend.proc.wait(15) in (0, -15, 1)
    wait(lambda: ipc.current_activity is None)
    assert not (backend.home / "instance.json").exists()
    assert "backend stopped" in (backend.home / "logs" / "backend.log").read_text()


def test_cli_status_and_stop(backend):
    env = backend.env()
    out = subprocess.run([sys.executable, "-m", "discord_pro", "status"], env=env, capture_output=True, text=True,
                         timeout=30, cwd=str(backend.home))
    assert out.returncode == 0 and "running" in out.stdout.lower()
    out = subprocess.run([sys.executable, "-m", "discord_pro", "stop"], env=env, capture_output=True, text=True,
                         timeout=30, cwd=str(backend.home))
    assert out.returncode == 0
    assert backend.proc.wait(15) == 0


def test_log_contains_no_secrets(backend):
    setup_backend(backend)
    link = httpx.post(f"{backend.url}/api/v1/auth/login-link", headers=backend.headers()).json()["path"]
    httpx.get(f"{backend.url}{link}", follow_redirects=False)
    httpx.post(f"{backend.url}/api/v1/auth/login", json={"access_code": "WRONG"})
    backend.stop()
    logs = (backend.home / "logs" / "backend.log").read_text() + backend.log()
    auth = json.loads((backend.home / "auth.json").read_text())
    assert auth["api_token"] not in logs
    assert auth["access_code"] not in logs
    assert link.split("code=")[1] not in logs
