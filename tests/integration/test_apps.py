"""App section: manual one-at-a-time toggling of presets."""

import time

from helpers import OTHER_ID, WS_URL, configure


def _app(client, auth, name, details, **extra):
    r = client.post("/api/v1/presets", headers=auth,
                    json={"name": name, "activity": {"details": details, "state": "Playing now"}, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def test_toggle_on_switch_and_off(client, auth, discord):
    configure(client, auth)
    ml = _app(client, auth, "Mobile Legends", "Ranked match", icon="https://example.com/ml.png")
    sp = _app(client, auth, "Spotify", "Listening", client_id=OTHER_ID)

    r = client.post(f"/api/v1/apps/{ml['id']}/toggle", headers=auth)
    assert r.status_code == 200, r.text
    st = r.json()["state"]["presence"]
    assert st["enabled"] and st["active_app_id"] == ml["id"] and st["active_preset_id"] == ml["id"]
    assert discord.activity["details"] == "Ranked match"

    # Switching to another app replaces the first one immediately.
    r = client.post(f"/api/v1/apps/{sp['id']}/toggle", headers=auth)
    st = r.json()["state"]
    assert st["presence"]["active_app_id"] == sp["id"] and st["discord"]["connected_client_id"] == OTHER_ID
    assert discord.activity["details"] == "Listening"

    # Clicking the app that is on turns presence off.
    r = client.post(f"/api/v1/apps/{sp['id']}/toggle", headers=auth)
    st = r.json()["state"]["presence"]
    assert st["enabled"] is False and st["active_app_id"] is None
    assert discord.activity is None
    assert client.get("/api/v1/config", headers=auth).json()["active_app_id"] is None


def test_other_commands_clear_the_app(client, auth, discord):
    configure(client, auth)
    ml = _app(client, auth, "Mobile Legends", "Ranked match")
    client.post(f"/api/v1/apps/{ml['id']}/toggle", headers=auth)
    client.post("/api/v1/presence/disable", headers=auth)
    assert client.get("/api/v1/config", headers=auth).json()["active_app_id"] is None

    client.post(f"/api/v1/apps/{ml['id']}/toggle", headers=auth)
    client.post(f"/api/v1/presets/{ml['id']}/activate", headers=auth)  # plain preset activation
    assert client.get("/api/v1/config", headers=auth).json()["active_app_id"] is None

    client.post(f"/api/v1/apps/{ml['id']}/toggle", headers=auth)
    assert client.delete(f"/api/v1/presets/{ml['id']}", headers=auth).status_code == 204
    assert client.get("/api/v1/config", headers=auth).json()["active_app_id"] is None


def test_toggle_errors(client, auth):
    r = client.post("/api/v1/apps/nope/toggle", headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "preset_not_found"
    p = _app(client, auth, "No ID", "Something")  # no Application ID configured anywhere
    r = client.post(f"/api/v1/apps/{p['id']}/toggle", headers=auth)
    assert r.status_code == 422 and r.json()["result"]["error"]["code"] == "configuration_invalid"
    assert client.get("/api/v1/config", headers=auth).json()["active_app_id"] is None


def test_icon_validation(client, auth):
    for bad in ("http://example.com/a.png", "javascript:alert(1)", "https://", "https://a b.png"):
        r = client.post("/api/v1/presets", headers=auth, json={"name": "X", "icon": bad, "activity": {"details": "Hello"}})
        assert r.status_code == 422, bad
    p = _app(client, auth, "X", "Hello", icon="  https://cdn.example.com/x.png ")
    assert p["icon"] == "https://cdn.example.com/x.png"
    assert _app(client, auth, "Y", "Hello")["icon"] == ""


def test_app_restored_after_restart(make_client, discord):
    c1 = make_client()
    a = {"Authorization": f"Bearer {c1.app.state.backend.auth.api_token}"}
    configure(c1, a)
    ml = _app(c1, a, "Mobile Legends", "Ranked match")
    c1.post(f"/api/v1/apps/{ml['id']}/toggle", headers=a)
    c1.__exit__(None, None, None)
    assert discord.activity is None  # shutdown cleared it, but the choice is saved

    c2 = make_client()
    deadline = time.time() + 3
    while discord.activity is None and time.time() < deadline:
        time.sleep(0.05)
    assert discord.activity["details"] == "Ranked match"
    st = c2.get("/api/v1/state", headers=a).json()["presence"]
    assert st["enabled"] and st["enabled_by"] == "startup" and st["active_app_id"] == ml["id"]


def test_websocket_app_toggle(client, auth, discord):
    configure(client, auth)
    ml = _app(client, auth, "Mobile Legends", "Ranked match")
    with client.websocket_connect(WS_URL, headers=auth) as ws:
        ws.send_json({"type": "app.toggle", "request_id": "t1", "payload": {"id": ml["id"]}})
        while True:
            msg = ws.receive_json()
            if msg["type"] == "operation.result" and msg["request_id"] == "t1":
                break
    assert msg["payload"]["success"], msg
    assert discord.activity["details"] == "Ranked match"
