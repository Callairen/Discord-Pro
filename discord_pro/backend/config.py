"""Versioned configuration schema, loading, migration and persistence."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from discord_pro.backend.activity import Activity, validate_client_id
from discord_pro.paths import atomic_write_json

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ApplicationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = ""
    start_with_presence_enabled: bool = False
    setup_completed: bool = False

    @field_validator("client_id", mode="before")
    @classmethod
    def _cid(cls, v: Any) -> str:
        return validate_client_id(v if v is not None else "")


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    allow_lan: bool = False

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        if v not in LOOPBACK_HOSTS:
            raise ValueError("host must be a loopback address; enable allow_lan for LAN access")
        return v

    def bind_host(self) -> str:
        return "0.0.0.0" if self.allow_lan else self.host


class RpcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reconnect_max_attempts: int = Field(default=20, ge=1, le=1000)
    reconnect_base_delay: float = Field(default=2.0, ge=0.0, le=60.0)
    reconnect_max_delay: float = Field(default=60.0, ge=0.01, le=3600.0)
    min_update_interval: float = Field(default=4.0, ge=0.0, le=60.0)

    @model_validator(mode="after")
    def _order(self) -> "RpcSettings":
        if self.reconnect_max_delay < self.reconnect_base_delay:
            raise ValueError("reconnect_max_delay must be >= reconnect_base_delay")
        return self


class AutoSwitchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    poll_interval: float = Field(default=5.0, ge=1.0, le=60.0)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    presence: Activity = Field(default_factory=Activity)
    # Optional Application ID used for the current activity instead of
    # application.client_id (set when a preset with its own ID is activated).
    presence_client_id: str = ""
    active_preset_id: str | None = None
    # Preset switched ON from the App section. Kept across restarts: the
    # backend turns that app back on when it starts.
    active_app_id: str | None = None
    server: ServerSettings = Field(default_factory=ServerSettings)
    rpc: RpcSettings = Field(default_factory=RpcSettings)
    auto_switch: AutoSwitchSettings = Field(default_factory=AutoSwitchSettings)

    @field_validator("presence_client_id", mode="before")
    @classmethod
    def _pcid(cls, v: Any) -> str:
        return validate_client_id(v if v is not None else "")

    def effective_client_id(self) -> str:
        return self.presence_client_id or self.application.client_id

    def public_dict(self) -> dict[str, Any]:
        """Configuration safe to send to clients (contains no secrets)."""
        return self.model_dump(mode="json")


SECTIONS = ("application", "presence", "server", "rpc", "auto_switch")


def _migrate_v0(raw: dict[str, Any]) -> dict[str, Any]:
    """Version 0: flat legacy layout ({client_id, details, state, ...})."""
    presence_keys = ("details", "state", "large_image", "large_text", "small_image", "small_text", "buttons")
    return {
        "schema_version": 1,
        "application": {"client_id": raw.get("client_id", "")},
        "presence": {k: raw[k] for k in presence_keys if k in raw},
    }


MIGRATIONS = {0: _migrate_v0}


def migrate(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Bring a raw config dict to the current schema. Returns (data, migrated)."""
    version = raw.get("schema_version", 0)
    migrated = False
    while isinstance(version, int) and version < SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ValueError(f"no migration from schema version {version}")
        raw = step(raw)
        version = raw.get("schema_version", version + 1)
        migrated = True
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version!r}")
    return raw, migrated


class ConfigStore:
    """Loads, validates and atomically persists ``config.json``."""

    def __init__(self, path: Path):
        self.path = path
        self.warnings: list[str] = []
        self.config = self._load()

    def _backup(self, suffix: str) -> None:
        try:
            backup = self.path.with_name(f"{self.path.name}.{suffix}-{int(time.time())}")
            backup.write_bytes(self.path.read_bytes())
            self.warnings.append(f"previous file kept as {backup.name}")
        except OSError:
            pass

    def _load(self) -> AppConfig:
        if not self.path.exists():
            cfg = AppConfig()
            self.save(cfg)
            log.info("created default configuration at %s", self.path)
            return cfg
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top-level JSON value must be an object")
        except (OSError, ValueError) as exc:
            self.warnings.append(f"configuration file unreadable ({exc}); defaults used")
            log.warning("configuration unreadable, using defaults: %s", exc)
            self._backup("corrupt")
            cfg = AppConfig()
            self.save(cfg)
            return cfg

        try:
            raw, migrated = migrate(raw)
        except ValueError as exc:
            self.warnings.append(f"configuration migration failed ({exc}); defaults used")
            self._backup("unsupported")
            cfg = AppConfig()
            self.save(cfg)
            return cfg

        try:
            cfg = AppConfig.model_validate(raw)
            if migrated:
                self._backup("pre-migration")
                self.save(cfg)
            return cfg
        except ValidationError as exc:
            log.warning("configuration partially invalid: %s", exc.errors(include_url=False, include_input=False))

        # Preserve every valid section; reset only the invalid ones.
        data: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        defaults = AppConfig()
        model_fields = AppConfig.model_fields
        for name in SECTIONS:
            section_model = model_fields[name].annotation
            try:
                data[name] = section_model.model_validate(raw.get(name, {})).model_dump(mode="json")  # type: ignore[union-attr]
            except (ValidationError, AttributeError):
                self.warnings.append(f"section '{name}' was invalid and has been reset to defaults")
                data[name] = getattr(defaults, name).model_dump(mode="json")
        for name in ("presence_client_id", "active_preset_id", "active_app_id"):
            try:
                data[name] = AppConfig.model_validate({**data, name: raw.get(name)}).model_dump()[name]
            except ValidationError:
                self.warnings.append(f"field '{name}' was invalid and has been reset")
        self._backup("invalid")
        cfg = AppConfig.model_validate(data)
        self.save(cfg)
        return cfg

    def save(self, cfg: AppConfig) -> None:
        atomic_write_json(self.path, cfg.model_dump(mode="json"))
        self.config = cfg

    def update(self, patch: dict[str, Any]) -> AppConfig:
        """Deep-merge ``patch`` into the current config, validate, then persist.

        Nothing is written (and the in-memory config is unchanged) if validation
        or the write fails.
        """
        merged = _deep_merge(self.config.model_dump(mode="json"), patch)
        cfg = AppConfig.model_validate(merged)
        self.save(cfg)
        return cfg


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "presence":
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
