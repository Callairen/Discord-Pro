"""WebSocket protocol tests: snapshot, broadcast, commands, robustness."""

import json

import pytest

from helpers import GOOD_ACTIVITY, WS_URL, configure


def recv_until(ws, pred, limit=50):
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("expected message not received")


def test_ws_requires_auth(client):
    with pytest.raises(Exception):
        with client.websocket_connect(WS_URL) as ws:
            ws.receive_json()


def test_ws_rejects_foreign_origin(client, auth):
    with pytest.raises(Exception):
        with client.websocket_connect(WS_URL, headers={**auth, "Origin": "http://evil.com"}) as ws:
            ws.receive_json()


def test_snapshot_on_connect(client, auth):
    with client.websocket_connect(WS_URL, headers=auth) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["payload"]["protocol"] == 1
        snap = ws.receive_json()
        assert snap["type"] == "state.snapshot"
        assert set(snap["payload"]) == {"state", "config", "presets"}
        assert snap["payload"]["state"]["server"]["websocket_connected_clients"] >= 1


def test_rest_change_broadcast_to_all_clients(client, auth, discord):
    configure(client, auth)
    with client.websocket_connect(WS_URL, headers=auth) as a, \
            client.websocket_connect(WS_URL, headers=auth) as b:
        for ws in (a, b):
            recv_until(ws, lambda m: m["type"] == "state.snapshot")
        client.post("/api/v1/presence/enable", headers=auth)
        for ws in (a, b):
            msg = recv_until(ws, lambda m: m["type"] == "state.updated"
                             and m["payload"]["presence"]["connection_state"] == "CONNECTED")
            assert msg["payload"]["presence"]["enabled"] is True


def test_ws_command_routes_through_service(client, auth, discord):
    configure(client, auth)
    with client.websocket_connect(WS_URL, headers=auth) as ws:
        recv_until(ws, lambda m: m["type"] == "state.snapshot")
        ws.send_json({"type": "presence.enable", "request_id": "r1", "payload": {}})
        res = recv_until(ws, lambda m: m["type"] == "operation.result" and m["request_id"] == "r1")
        assert res["payload"]["success"] is True
        assert discord.activity is not None
        # REST sees exactly the same canonical state
        assert client.get("/api/v1/state", headers=auth).json()["presence"]["connection_state"] == "CONNECTED"

        ws.send_json({"type": "presence.update", "request_id": "r2",
                      "payload": {"activity": {**GOOD_ACTIVITY, "details": "From WS"}, "apply": True}})
        res = recv_until(ws, lambda m: m["type"] == "operation.result" and m["request_id"] == "r2")
        assert res["payload"]["success"] and discord.activity["details"] == "From WS"

        ws.send_json({"type": "preset.create", "request_id": "r3",
                      "payload": {"preset": {"name": "WS preset", "activity": {"details": "Hi there"}}}})
        res = recv_until(ws, lambda m: m["type"] == "operation.result" and m["request_id"] == "r3")
        pid = res["payload"]["data"]["preset"]["id"]
        recv_until(ws, lambda m: m["type"] == "presets.updated")
        ws.send_json({"type": "preset.activate", "request_id": "r4", "payload": {"id": pid}})
        res = recv_until(ws, lambda m: m["type"] == "operation.result" and m["request_id"] == "r4")
        assert res["payload"]["success"] and discord.activity["details"] == "Hi there"

        ws.send_json({"type": "presence.disable", "request_id": "r5", "payload": {}})
        recv_until(ws, lambda m: m["type"] == "operation.result" and m["request_id"] == "r5")
        assert discord.activity is None


def test_duplicate_request_id_not_reexecuted(client, auth, discord):
    configure(client, auth)
    with client.websocket_connect(WS_URL, headers=auth) as ws:
        recv_until(ws, lambda m: m["type"] == "state.snapshot")
        ws.send_json({"type": "presence.enable", "request_id": "dup", "payload": {}})
        first = recv_until(ws, lambda m: m["type"] == "operation.result" and m["request_id"] == "dup")
        ws.send_json({"type": "presence.enable", "request_id": "dup", "payload": {}})
        second = recv_until(ws, lambda m: m["type"] == "operation.result" and m["request_id"] == "dup")
        assert second["payload"]["duplicate"] is True and second["payload"]["success"] == first["payload"]["success"]
        assert discord.connects == 1 and discord.set_calls == 1


@pytest.mark.parametrize("raw,code", [
    ("not json", "invalid_json"),
    ("[1,2]", "invalid_message"),
    (json.dumps({"type": 5}), "invalid_message"),
    (json.dumps({"type": "nope", "request_id": "x"}), "unknown_command"),
    (json.dumps({"type": "presence.enable", "request_id": 7}), "invalid_request_id"),
    (json.dumps({"type": "presence.enable", "payload": []}), "invalid_payload"),
    (json.dumps({"type": "preset.delete", "request_id": "d", "payload": {}}), "validation_error"),
    (json.dumps({"type": "preset.delete", "request_id": "d2", "payload": {"id": "missing"}}), "preset_not_found"),
    (json.dumps({"type": "presence.update", "request_id": "u", "payload": {"activity": {"details": "x"}}}),
     "validation_error"),
    ("x" * 70000, "message_too_large"),
])
def test_malformed_messages_rejected_safely(client, auth, raw, code):
    with client.websocket_connect(WS_URL, headers=auth) as ws:
        recv_until(ws, lambda m: m["type"] == "state.snapshot")
        ws.send_text(raw)
        res = recv_until(ws, lambda m: m["type"] == "operation.result")
        assert res["payload"]["success"] is False and res["payload"]["error"]["code"] == code
        ws.send_json({"type": "ping", "request_id": "p"})  # connection still healthy
        assert recv_until(ws, lambda m: m["type"] == "pong")["request_id"] == "p"


def test_disconnect_and_reconnect_gets_fresh_snapshot(client, auth, discord):
    configure(client, auth)
    with client.websocket_connect(WS_URL, headers=auth) as ws:
        recv_until(ws, lambda m: m["type"] == "state.snapshot")
    client.post("/api/v1/presence/enable", headers=auth)  # change while disconnected
    assert client.get("/api/v1/health").status_code == 200  # backend unaffected (AC-12)
    with client.websocket_connect(WS_URL, headers=auth) as ws:
        snap = recv_until(ws, lambda m: m["type"] == "state.snapshot")
        assert snap["payload"]["state"]["presence"]["connection_state"] == "CONNECTED"  # AC-13


def test_session_cookie_websocket(client, auth):
    path = client.post("/api/v1/auth/login-link", headers=auth).json()["path"]
    client.get(path)
    with client.websocket_connect(WS_URL, headers={"Origin": "http://127.0.0.1:8765"}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_slow_subscriber_dropped():
    import asyncio

    from discord_pro.backend.state import SUBSCRIBER_QUEUE_SIZE, EventBus

    bus = EventBus()
    q = bus.subscribe()
    for i in range(SUBSCRIBER_QUEUE_SIZE + 5):
        bus.publish({"type": "x", "i": i})
    assert bus.count == 0
    assert q.qsize() == SUBSCRIBER_QUEUE_SIZE
    assert isinstance(q, asyncio.Queue)
