import json
import os

import pytest
from pydantic import ValidationError

from discord_pro.backend.config import AppConfig, ConfigStore, migrate
from discord_pro.backend.errors import NotFoundError
from discord_pro.backend.presets import PresetInput, PresetStore


def test_missing_config_creates_defaults(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    assert store.config == AppConfig()
    assert (tmp_path / "config.json").exists()
    assert store.config.server.host == "127.0.0.1" and store.config.server.allow_lan is False


def test_malformed_config_backed_up_and_defaults_used(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    store = ConfigStore(p)
    assert store.config == AppConfig()
    assert any("unreadable" in w for w in store.warnings)
    assert list(tmp_path.glob("config.json.corrupt-*"))


def test_invalid_section_reset_but_valid_sections_preserved(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "schema_version": 1,
        "application": {"client_id": "123456789012345678", "setup_completed": True},
        "presence": {"details": "x"},  # invalid (too short)
        "server": {"port": 9000},
    }))
    store = ConfigStore(p)
    assert store.config.application.client_id == "123456789012345678"
    assert store.config.server.port == 9000
    assert store.config.presence.details == ""
    assert any("presence" in w for w in store.warnings)


def test_v0_migration(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"client_id": "123456789012345678", "details": "Old", "state": "Format"}))
    store = ConfigStore(p)
    assert store.config.application.client_id == "123456789012345678"
    assert store.config.presence.details == "Old"
    assert json.loads(p.read_text())["schema_version"] == 1


def test_unsupported_version():
    with pytest.raises(ValueError):
        migrate({"schema_version": 99})


def test_update_is_validated_and_atomic(tmp_path):
    p = tmp_path / "config.json"
    store = ConfigStore(p)
    before = p.read_text()
    with pytest.raises(ValidationError):
        store.update({"server": {"host": "0.0.0.0"}})  # non-loopback host refused
    assert p.read_text() == before
    store.update({"server": {"port": 9123}})
    assert json.loads(p.read_text())["server"]["port"] == 9123
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_config_write_failure_keeps_memory_state(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    import discord_pro.backend.config as cfgmod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cfgmod, "atomic_write_json", boom)
    with pytest.raises(OSError):
        store.update({"server": {"port": 9999}})
    assert store.config.server.port == 8765


def test_preset_crud(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    p = store.create(PresetInput(name="VS Code", activity={"details": "Coding"}, process_names=["Code.exe"]))
    assert store.get(p.id).name == "VS Code"
    p2 = store.update(p.id, PresetInput(name="Renamed", activity={"details": "Coding"}))
    assert p2.created_at == p.created_at and p2.name == "Renamed"
    dup = store.duplicate(p.id)
    assert dup.name == "Renamed (copy)" and dup.id != p.id
    reloaded = PresetStore(tmp_path / "presets.json")
    assert [x.name for x in reloaded.list()] == ["Renamed", "Renamed (copy)"]
    store.delete(p.id)
    with pytest.raises(NotFoundError):
        store.get(p.id)
    with pytest.raises(NotFoundError):
        store.delete("nope")


def test_invalid_presets_skipped_and_kept(tmp_path):
    path = tmp_path / "presets.json"
    good = {"id": "a", "name": "Good", "client_id": "", "activity": {"details": "Hello"},
            "process_names": [], "created_at": 1, "updated_at": 1}
    bad = {"id": "b", "name": "", "activity": {"details": "x"}}
    path.write_text(json.dumps({"presets": [good, bad]}))
    store = PresetStore(path)
    assert [p.id for p in store.list()] == ["a"]
    assert json.loads((tmp_path / "presets.invalid.json").read_text())["presets"] == [bad]


def test_preset_process_names_validation():
    with pytest.raises(ValidationError):
        PresetInput(name="x", process_names=["../../etc/passwd"])
    assert PresetInput(name="x", process_names=[" Code.exe ", ""]).process_names == ["Code.exe"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_auth_file_is_private(tmp_path):
    from discord_pro.backend.security import AuthStore

    AuthStore(tmp_path / "auth.json")
    assert oct((tmp_path / "auth.json").stat().st_mode & 0o777) == "0o600"
