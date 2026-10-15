"""Process auto-switch (port of the original legacy/agents/*.py behaviour)."""

from discord_pro.backend.service import running_process_names
from helpers import OTHER_ID, configure


def _presets(client, auth):
    vs = client.post("/api/v1/presets", headers=auth, json={
        "name": "VS Code", "process_names": ["Code.exe"],
        "activity": {"details": "Coding Session", "state": "Visual Studio Code", "large_image": "vscode"}}).json()
    kali = client.post("/api/v1/presets", headers=auth, json={
        "name": "Kali", "client_id": OTHER_ID, "process_names": ["VirtualBox.exe", "VirtualBoxVM.exe"],
        "activity": {"details": "Pentesting Mode", "state": "Kali Linux on VirtualBox"}}).json()
    return vs, kali


def tick(client, running):
    svc = client.app.state.backend.service
    client.portal.call(svc.auto_switch_tick, {n.lower() for n in running})


def test_process_starts_and_stops(client, auth, discord):
    configure(client, auth)
    vs, kali = _presets(client, auth)
    tick(client, {"explorer.exe"})
    assert discord.activity is None
    tick(client, {"Code.exe"})
    st = client.get("/api/v1/state", headers=auth).json()
    assert st["presence"]["enabled"] and st["presence"]["enabled_by"] == "auto-switch"
    assert st["auto_switch"]["matched_preset_id"] == vs["id"]
    assert discord.activity["details"] == "Coding Session"
    tick(client, {"Code.exe", "VirtualBoxVM.exe"})  # first matching preset (list order) wins
    assert discord.activity["details"] == "Coding Session"
    tick(client, {"VirtualBoxVM.exe"})
    assert discord.activity["details"] == "Pentesting Mode"
    assert client.get("/api/v1/state", headers=auth).json()["discord"]["connected_client_id"] == OTHER_ID
    tick(client, set())
    assert discord.activity is None
    assert client.get("/api/v1/state", headers=auth).json()["presence"]["enabled"] is False


def test_manual_enable_not_turned_off_by_auto_switch(client, auth, discord):
    configure(client, auth)
    _presets(client, auth)
    client.post("/api/v1/presence/enable", headers=auth)
    tick(client, {"code.exe"})
    tick(client, set())
    st = client.get("/api/v1/state", headers=auth).json()
    assert st["presence"]["enabled"] is True and st["presence"]["enabled_by"] == "desktop"


def test_manual_disable_respected_until_match_changes(client, auth, discord):
    configure(client, auth)
    _presets(client, auth)
    tick(client, {"code.exe"})
    client.post("/api/v1/presence/disable", headers=auth)
    tick(client, {"code.exe"})  # same match: does not override the user's choice
    assert discord.activity is None


def test_watcher_task_follows_setting(client, auth):
    svc = client.app.state.backend.service
    client.put("/api/v1/config", headers=auth, json={"auto_switch": {"enabled": True, "poll_interval": 1}})
    assert svc._watch_task is not None and not svc._watch_task.done()
    assert client.get("/api/v1/state", headers=auth).json()["auto_switch"]["enabled"] is True


def test_running_process_names_lists_this_process():
    names = running_process_names()
    assert names and all(n == n.lower() for n in names)
    assert any("python" in n for n in names)


def test_app_switched_on_by_hand_is_not_overridden(client, auth, discord):
    configure(client, auth)
    vs, kali = _presets(client, auth)
    client.post(f"/api/v1/apps/{kali['id']}/toggle", headers=auth)
    tick(client, {"code.exe"})
    st = client.get("/api/v1/state", headers=auth).json()
    assert st["presence"]["active_app_id"] == kali["id"]
    assert discord.activity["details"] == "Pentesting Mode"
