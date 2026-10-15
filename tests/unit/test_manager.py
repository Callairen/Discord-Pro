"""RPC state machine tests with an in-process fake adapter."""

import asyncio

import pytest

from discord_pro.backend.config import RpcSettings
from discord_pro.backend.manager import ConnState, Desired, RpcManager, backoff_delay
from fakes import FakeAdapter, FakeDiscordState

CID = "123456789012345678"
ACT = {"activity_type": 0, "details": "Hello"}


def make(discord, **settings):
    base = dict(reconnect_max_attempts=4, reconnect_base_delay=0.02, reconnect_max_delay=0.05, min_update_interval=0.0)
    base.update(settings)
    states = []

    async def on_change(status):
        states.append(status.state.value)

    m = RpcManager(lambda: FakeAdapter(discord), RpcSettings(**base), on_change, health_interval=0.02)
    return m, states


async def wait_for(pred, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


def test_backoff_bounds():
    assert backoff_delay(0, 2, 60) == 0
    assert backoff_delay(1, 2, 60, rng=lambda: 0.5) == 2
    assert backoff_delay(3, 2, 60, rng=lambda: 0.5) == 8
    assert backoff_delay(20, 2, 60, rng=lambda: 1.0) == 60
    for attempt in range(1, 10):
        d = backoff_delay(attempt, 2, 60)
        assert 0 <= d <= 60


async def test_enable_publish_and_disable_clear():
    d = FakeDiscordState()
    m, states = make(d)
    m.start()
    r = await m.apply(Desired(True, CID, ACT))
    assert r.success and m.status.state == ConnState.CONNECTED
    assert d.activity == ACT and m.status.last_successful_update
    r = await m.apply(Desired(False))
    assert r.success and r.data["cleared"] == "clear"
    assert d.activity is None and d.open_connections == 0
    assert m.status.state == ConnState.DISCONNECTED
    assert "CONNECTING" in states
    await m.stop()


async def test_disable_when_never_connected_reports_nothing_published():
    m, _ = make(FakeDiscordState())
    m.start()
    r = await m.apply(Desired(False))
    assert r.success and r.data["cleared"] == "none"
    await m.stop()


async def test_duplicate_enable_does_not_reconnect_or_resend():
    d = FakeDiscordState()
    m, _ = make(d)
    m.start()
    await m.apply(Desired(True, CID, ACT))
    results = await asyncio.gather(*[m.apply(Desired(True, CID, ACT)) for _ in range(5)])
    assert all(r.success for r in results)
    assert d.connects == 1 and d.max_open_connections == 1 and d.set_calls == 1
    await m.stop()


async def test_discord_not_running_retries_then_connects():
    d = FakeDiscordState(running=False)
    m, _ = make(d, reconnect_max_attempts=50)
    m.start()
    r = await m.apply(Desired(True, CID, ACT))
    assert not r.success and r.pending and r.code == "discord_not_found"
    assert m.status.state == ConnState.RECONNECTING
    d.running = True
    await wait_for(lambda: m.status.state == ConnState.CONNECTED)
    assert d.activity == ACT
    await m.stop()


async def test_reconnect_exhaustion_goes_to_error_and_recovers_on_command():
    d = FakeDiscordState(running=False)
    m, _ = make(d, reconnect_max_attempts=3)
    m.start()
    await m.apply(Desired(True, CID, ACT))
    await wait_for(lambda: m.status.state == ConnState.ERROR)
    assert m.status.last_error["code"] == "reconnect_exhausted"
    attempts = m.status.reconnect_attempt
    await asyncio.sleep(0.2)
    assert m.status.reconnect_attempt == attempts  # no further retries
    d.running = True
    r = await m.apply(Desired(True, CID, ACT))
    assert r.success and m.status.state == ConnState.CONNECTED
    await m.stop()


async def test_invalid_client_id_is_permanent_no_retry():
    d = FakeDiscordState(valid_ids={CID})
    m, _ = make(d)
    m.start()
    r = await m.apply(Desired(True, "999999999999999999", ACT))
    assert not r.success and not r.pending and r.code == "invalid_client_id"
    assert m.status.state == ConnState.ERROR
    await asyncio.sleep(0.15)
    assert m.status.reconnect_attempt == 0 and d.connects == 0
    await m.stop()


async def test_crash_detected_and_activity_republished():
    d = FakeDiscordState()
    m, states = make(d, reconnect_max_attempts=100)
    m.start()
    await m.apply(Desired(True, CID, ACT))
    d.crash()
    await wait_for(lambda: m.status.state == ConnState.RECONNECTING)
    d.running = True
    await wait_for(lambda: m.status.state == ConnState.CONNECTED and d.activity == ACT)
    assert d.connects == 2
    await m.stop()


async def test_payload_rejection_keeps_connection():
    d = FakeDiscordState(reject_payload=True)
    m, _ = make(d)
    m.start()
    r = await m.apply(Desired(True, CID, ACT))
    assert not r.success and r.code == "activity_rejected"
    assert m.status.state == ConnState.CONNECTED and d.open_connections == 1
    d.reject_payload = False
    r = await m.apply(Desired(True, CID, {**ACT, "details": "Fixed"}))
    assert r.success and d.activity["details"] == "Fixed"
    await m.stop()


async def test_switching_client_id_reconnects_single_connection():
    d = FakeDiscordState()
    m, _ = make(d)
    m.start()
    await m.apply(Desired(True, CID, ACT))
    await m.apply(Desired(True, "876543210987654321", ACT))
    assert d.connects == 2 and d.open_connections == 1 and d.max_open_connections == 1
    assert m.status.connected_client_id == "876543210987654321"
    await m.stop()


async def test_throttle_coalesces_updates():
    d = FakeDiscordState()
    m, _ = make(d, min_update_interval=0.3)
    m.start()
    await m.apply(Desired(True, CID, ACT))
    tasks = [asyncio.create_task(m.apply(Desired(True, CID, {**ACT, "details": f"v{i}"}))) for i in range(5)]
    results = await asyncio.gather(*tasks)
    assert all(r.success for r in results)
    assert d.set_calls == 2  # initial + one coalesced publish
    assert d.activity["details"] == "v4"
    await m.stop()


async def test_stop_clears_activity():
    d = FakeDiscordState()
    m, states = make(d)
    m.start()
    await m.apply(Desired(True, CID, ACT))
    await m.stop()
    assert d.activity is None and d.open_connections == 0
    assert "STOPPING" in states
    r = await m.apply(Desired(True, CID, ACT))
    assert r.code == "stopping"


async def test_test_connection_does_not_disturb_live_connection():
    d = FakeDiscordState(valid_ids={CID})
    m, _ = make(d)
    m.start()
    assert (await m.test_connection(CID)).success
    assert d.open_connections == 0
    assert (await m.test_connection("999999999999999999")).code == "invalid_client_id"
    await m.apply(Desired(True, CID, ACT))
    assert (await m.test_connection(CID)).success
    r = await m.test_connection("876543210987654321")
    assert r.code == "presence_active" and d.open_connections == 1
    await m.stop()
