"""Real pypresence adapter against a fake Discord IPC socket (Linux/macOS).

This exercises the actual library code path (pipe discovery, handshake,
SET_ACTIVITY framing, disconnect detection) - but against a simulator, not
the real Discord client.
"""

import asyncio
import shutil
import sys

import pytest

from discord_pro.backend.config import RpcSettings
from discord_pro.backend.manager import ConnState, Desired, RpcManager
from discord_pro.backend.rpc import PypresenceAdapter, RpcThread
from fakes import FakeDiscordIPC, short_socket_dir

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix socket simulator")

CID = "123456789012345678"
ACT = {"activity_type": 0, "details": "Real IPC", "state": "pypresence", "large_image": "logo",
       "buttons": [{"label": "Repo", "url": "https://github.com/Callairen/Discord-Pro"}], "start": 1700000000}


@pytest.fixture
def ipc(monkeypatch, tmp_path):
    d = short_socket_dir(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    server = FakeDiscordIPC(d, valid_ids={CID})
    yield server
    server.stop()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def rpc_thread():
    t = RpcThread()
    yield t
    t.stop()


def manager(rpc_thread, **kw):
    settings = RpcSettings(reconnect_max_attempts=kw.get("attempts", 50), reconnect_base_delay=0.05,
                           reconnect_max_delay=0.2, min_update_interval=0.0)
    return RpcManager(lambda: PypresenceAdapter(rpc_thread, timeout=2), settings, health_interval=0.05)


async def wait_for(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.02)


async def test_publish_and_clear_over_real_ipc(ipc, rpc_thread):
    ipc.start()
    m = manager(rpc_thread)
    m.start()
    r = await m.apply(Desired(True, CID, ACT))
    assert r.success, r.message
    await wait_for(lambda: ipc.current_activity is not None)
    act = ipc.current_activity
    assert act["details"] == "Real IPC" and act["buttons"][0]["label"] == "Repo"
    assert act["assets"]["large_image"] == "logo" and act["timestamps"]["start"] == 1700000000
    r = await m.apply(Desired(False))
    assert r.success and r.data["cleared"] == "clear"
    await wait_for(lambda: ipc.live_connections == 0)
    assert ipc.current_activity is None
    await m.stop()


async def test_discord_not_running_then_started(ipc, rpc_thread):
    m = manager(rpc_thread)
    m.start()
    r = await m.apply(Desired(True, CID, ACT))
    assert not r.success and r.pending and r.code == "discord_not_found"
    ipc.start()
    await wait_for(lambda: m.status.state == ConnState.CONNECTED)
    await wait_for(lambda: ipc.current_activity is not None)
    await m.stop()


async def test_invalid_client_id_over_real_ipc(ipc, rpc_thread):
    ipc.start()
    m = manager(rpc_thread)
    m.start()
    r = await m.apply(Desired(True, "999999999999999999", ACT))
    assert not r.success and r.code == "invalid_client_id" and m.status.state == ConnState.ERROR
    await m.stop()


async def test_discord_crash_detected_and_recovered(ipc, rpc_thread):
    ipc.start()
    m = manager(rpc_thread)
    m.start()
    assert (await m.apply(Desired(True, CID, ACT))).success
    ipc.stop()  # Discord quits
    await wait_for(lambda: m.status.state in (ConnState.RECONNECTING, ConnState.CONNECTING))
    assert m.status.last_error is not None
    ipc.start()  # Discord restarts
    await wait_for(lambda: m.status.state == ConnState.CONNECTED and ipc.current_activity is not None)
    await m.stop()


async def test_payload_rejected_by_discord(ipc, rpc_thread):
    ipc.start()
    ipc.reject_payload = True
    m = manager(rpc_thread)
    m.start()
    r = await m.apply(Desired(True, CID, ACT))
    assert not r.success and r.code == "activity_rejected" and m.status.state == ConnState.CONNECTED
    await m.stop()


async def test_shutdown_clears_over_real_ipc(ipc, rpc_thread):
    ipc.start()
    m = manager(rpc_thread)
    m.start()
    await m.apply(Desired(True, CID, ACT))
    await m.stop()
    await wait_for(lambda: ipc.live_connections == 0)
    assert ipc.current_activity is None
