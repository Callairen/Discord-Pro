"""REST API integration tests (backend app + fake RPC adapter)."""

import time

from helpers import CLIENT_ID, GOOD_ACTIVITY, OTHER_ID, configure


def test_health_no_auth(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_auth_required(client):
    for method, path in [("get", "/api/v1/state"), ("get", "/api/v1/config"), ("post", "/api/v1/presence/enable"),
                         ("get", "/api/v1/presets"), ("get", "/api/v1/diagnostics")]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "unauthorized"
    assert client.get("/api/v1/state", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_unconfigured_start(client, auth):
    s = client.get("/api/v1/state", headers=auth).json()
    assert s["configuration"]["configured"] is False
    assert s["presence"]["enabled"] is False and s["presence"]["connection_state"] == "DISCONNECTED"
    assert "Application ID is not set" in s["configuration"]["problems"]
    r = client.post("/api/v1/presence/enable", headers=auth)
    assert r.status_code == 422 and r.json()["result"]["error"]["code"] == "configuration_invalid"


def test_setup_validate_reports_errors(client, auth):
    r = client.post("/api/v1/setup/validate", headers=auth, json={"client_id": "abc", "activity": {"details": "x"}})
    body = r.json()
    assert body["valid"] is False
    fields = {e["field"] for e in body["errors"]}
    assert "client_id" in fields and any(f.startswith("activity") for f in fields)
    r = client.post("/api/v1/setup/validate", headers=auth, json={"client_id": CLIENT_ID, "activity": GOOD_ACTIVITY})
    assert r.json() == {"valid": True, "errors": []}


def test_setup_test_connection(client, auth, discord):
    discord.valid_ids = {CLIENT_ID}
    r = client.post("/api/v1/setup/test-connection", headers=auth, json={"client_id": CLIENT_ID})
    assert r.status_code == 200 and r.json()["result"]["success"]
    r = client.post("/api/v1/setup/test-connection", headers=auth, json={"client_id": OTHER_ID})
    assert r.status_code == 422 and r.json()["result"]["error"]["code"] == "invalid_client_id"
    discord.running = False
    r = client.post("/api/v1/setup/test-connection", headers=auth, json={"client_id": CLIENT_ID})
    assert r.status_code == 503 and r.json()["result"]["error"]["code"] == "discord_not_found"
    r = client.post("/api/v1/setup/test-connection", headers=auth, json={"client_id": "12"})
    assert r.status_code == 422


def test_enable_disable_cycle(client, auth, discord):
    configure(client, auth)
    r = client.post("/api/v1/presence/enable", headers=auth)
    assert r.status_code == 200, r.text
    st = r.json()["state"]
    assert st["presence"]["enabled"] and st["presence"]["connection_state"] == "CONNECTED"
    assert st["discord"]["rpc_connected"] and st["presence"]["applied"]
    assert discord.activity["details"] == "Coding Session"
    assert discord.activity["buttons"] == [{"label": "GitHub", "url": "https://github.com/Callairen"}]
    assert "start" in discord.activity

    r = client.post("/api/v1/presence/disable", headers=auth)
    assert r.status_code == 200 and r.json()["result"]["data"]["cleared"] == "clear"
    st = r.json()["state"]
    assert not st["presence"]["enabled"] and st["presence"]["connection_state"] == "DISCONNECTED"
    assert discord.activity is None and discord.open_connections == 0


def test_enable_when_discord_closed_is_pending_not_success(client, auth, discord):
    configure(client, auth)
    discord.running = False
    r = client.post("/api/v1/presence/enable", headers=auth)
    assert r.status_code == 503
    res = r.json()["result"]
    assert res["success"] is False and res["pending"] is True
    st = r.json()["state"]
    assert st["presence"]["enabled"] is True  # desired ON ...
    assert st["presence"]["connection_state"] == "RECONNECTING"  # ... but not connected
    discord.running = True
    deadline = time.time() + 3
    while time.time() < deadline:
        st = client.get("/api/v1/state", headers=auth).json()
        if st["presence"]["connection_state"] == "CONNECTED":
            break
        time.sleep(0.05)
    assert st["presence"]["connection_state"] == "CONNECTED" and discord.activity is not None


def test_update_save_vs_apply(client, auth, discord):
    configure(client, auth)
    client.post("/api/v1/presence/enable", headers=auth)
    new = {**GOOD_ACTIVITY, "details": "Saved only"}
    r = client.put("/api/v1/presence", headers=auth, json={"activity": new, "apply": False})
    assert r.status_code == 200
    assert discord.activity["details"] == "Coding Session"
    assert r.json()["state"]["presence"]["applied"] is False
    r = client.post("/api/v1/presence/apply", headers=auth)
    assert r.status_code == 200 and discord.activity["details"] == "Saved only"
    assert r.json()["state"]["presence"]["applied"] is True
    r = client.put("/api/v1/presence", headers=auth, json={"activity": {**new, "state": "Applied now"}})
    assert discord.activity["state"] == "Applied now"


def test_invalid_update_rejected_and_config_unchanged(client, auth):
    configure(client, auth)
    r = client.put("/api/v1/presence", headers=auth,
                   json={"activity": {"details": "ok", "buttons": [{"label": "x", "url": "javascript:alert(1)"}]}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert client.get("/api/v1/config", headers=auth).json()["presence"]["details"] == "Coding Session"


def test_config_update_and_invalid_patch(client, auth):
    r = client.put("/api/v1/config", headers=auth, json={"rpc": {"reconnect_max_attempts": 7}})
    assert r.status_code == 200 and r.json()["rpc"]["reconnect_max_attempts"] == 7
    r = client.put("/api/v1/config", headers=auth, json={"server": {"port": 80}})
    assert r.status_code == 422
    r = client.put("/api/v1/config", headers=auth, json={"server": {"allow_lan": True}})
    assert r.status_code == 200
    st = client.get("/api/v1/state", headers=auth).json()
    assert st["server"]["allow_lan"] is True and st["server"]["restart_required"] is True
    assert st["server"]["lan_active"] is False


def test_preset_lifecycle(client, auth, discord):
    configure(client, auth)
    r = client.post("/api/v1/presets", headers=auth, json={
        "name": "Kali Linux", "client_id": OTHER_ID, "process_names": ["VirtualBox.exe"],
        "activity": {"details": "Pentesting Mode", "state": "Kali Linux on VirtualBox", "large_image": "kalinux"}})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert len(client.get("/api/v1/presets", headers=auth).json()) == 1

    r = client.post(f"/api/v1/presets/{pid}/activate", headers=auth, json={"enable": True})
    assert r.status_code == 200, r.text
    st = r.json()["state"]
    assert st["presence"]["active_preset_id"] == pid and st["discord"]["connected_client_id"] == OTHER_ID
    assert discord.activity["details"] == "Pentesting Mode"

    r = client.put(f"/api/v1/presets/{pid}", headers=auth, json={"name": "Kali 2", "activity": {"details": "Lab"}})
    assert r.status_code == 200 and r.json()["name"] == "Kali 2"
    r = client.post(f"/api/v1/presets/{pid}/duplicate", headers=auth)
    assert r.status_code == 201 and r.json()["name"] == "Kali 2 (copy)"

    assert client.delete(f"/api/v1/presets/{pid}", headers=auth).status_code == 204
    assert client.get("/api/v1/config", headers=auth).json()["active_preset_id"] is None


def test_preset_errors(client, auth):
    r = client.delete("/api/v1/presets/doesnotexist", headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "preset_not_found"
    assert client.post("/api/v1/presets/nope/activate", headers=auth).status_code == 404
    r = client.post("/api/v1/presets", headers=auth, json={"name": "", "activity": {}})
    assert r.status_code == 422


def test_invalid_preset_activation_does_not_touch_config(client, auth):
    configure(client, auth)
    r = client.post("/api/v1/presets", headers=auth, json={"name": "Empty", "activity": {}})
    pid = r.json()["id"]
    before = client.get("/api/v1/config", headers=auth).json()
    r = client.post(f"/api/v1/presets/{pid}/activate", headers=auth)
    assert r.status_code == 422
    assert client.get("/api/v1/config", headers=auth).json() == before


def test_persistence_across_restart(make_client, home, discord):
    c1 = make_client()
    a = {"Authorization": f"Bearer {c1.app.state.backend.auth.api_token}"}
    configure(c1, a)
    c1.post("/api/v1/presets", headers=a, json={"name": "Keep me", "activity": {"details": "Persisted"}})
    c1.__exit__(None, None, None)
    c2 = make_client()
    a2 = {"Authorization": f"Bearer {c2.app.state.backend.auth.api_token}"}
    assert a2 == a  # token persisted
    cfg = c2.get("/api/v1/config", headers=a2).json()
    assert cfg["application"]["client_id"] == CLIENT_ID and cfg["application"]["setup_completed"]
    assert [p["name"] for p in c2.get("/api/v1/presets", headers=a2).json()] == ["Keep me"]


def test_start_with_presence_enabled(make_client, discord):
    c1 = make_client()
    a = {"Authorization": f"Bearer {c1.app.state.backend.auth.api_token}"}
    configure(c1, a)
    c1.put("/api/v1/config", headers=a, json={"application": {"start_with_presence_enabled": True}})
    c1.__exit__(None, None, None)
    assert discord.activity is None  # shutdown cleared it
    c2 = make_client()
    deadline = time.time() + 3
    while discord.activity is None and time.time() < deadline:
        time.sleep(0.05)
    assert discord.activity is not None


def test_diagnostics_has_no_secrets(client, auth, token):
    body = client.get("/api/v1/diagnostics", headers=auth).text
    assert token not in body
    assert client.app.state.backend.auth.access_code not in body
    assert "rpc" in client.get("/api/v1/diagnostics", headers=auth).json()


def test_shutdown_requires_token_and_calls_back(make_client):
    called = []
    c = make_client(shutdown=lambda: called.append(True))
    a = {"Authorization": f"Bearer {c.app.state.backend.auth.api_token}"}
    assert c.post("/api/v1/shutdown").status_code == 401
    assert c.post("/api/v1/shutdown", headers=a).status_code == 200
    time.sleep(0.4)
    assert called == [True]


def test_errors_do_not_leak_tracebacks(make_client, monkeypatch):
    client = make_client(raise_errors=False)
    auth = {"Authorization": f"Bearer {client.app.state.backend.auth.api_token}"}
    svc = client.app.state.backend.service

    async def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(svc, "enable", boom)
    r = client.post("/api/v1/presence/enable", headers=auth)
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal_error", "message": "Internal server error"}}
