"""Presence service: the single entry point for every command.

REST routes, WebSocket commands and the process auto-switch all call these
methods, so there is exactly one implementation of each operation and one
copy of the state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import psutil
from pydantic import ValidationError

from discord_pro.backend.activity import Activity
from discord_pro.backend.config import AppConfig, ConfigStore
from discord_pro.backend.errors import AppError, OperationResult
from discord_pro.backend.manager import Desired, RpcManager, RpcStatus
from discord_pro.backend.presets import Preset, PresetInput, PresetStore
from discord_pro.backend.state import StateManager

log = logging.getLogger(__name__)


def validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
        for err in exc.errors(include_url=False, include_input=False)
    ]


class PresenceService:
    def __init__(self, config: ConfigStore, presets: PresetStore, state: StateManager, manager: RpcManager):
        self.config_store = config
        self.presets = presets
        self.state = state
        self.manager = manager
        self._lock = asyncio.Lock()
        self._session_start: int | None = None
        self._auto_preset: str | None = None
        self._watch_task: asyncio.Task | None = None

    # -------------------------------------------------------------- helpers
    @property
    def config(self) -> AppConfig:
        return self.config_store.config

    def configuration_problems(self, cfg: AppConfig | None = None) -> list[str]:
        cfg = cfg or self.config
        problems = []
        if not cfg.effective_client_id():
            problems.append("Application ID is not set")
        if not cfg.presence.has_content():
            problems.append("Activity needs at least details, state or a large image")
        return problems

    def _desired_kwargs(self, cfg: AppConfig) -> dict[str, Any]:
        return cfg.presence.to_rpc_kwargs(self._session_start)

    async def refresh_config_state(self) -> None:
        cfg = self.config
        problems = self.configuration_problems(cfg)
        desired_kwargs = self._desired_kwargs(cfg) if self._session_start else cfg.presence.to_rpc_kwargs(None)
        published = self.manager.status.published
        applied = True
        if self.manager.desired.enabled:
            applied = published is not None and published == desired_kwargs and \
                self.manager.status.connected_client_id == cfg.effective_client_id()
        await self.state.update(
            configuration={
                "configured": cfg.application.setup_completed and bool(cfg.effective_client_id()),
                "configuration_valid": not problems,
                "problems": problems,
                "warnings": self.config_store.warnings + self.presets.warnings,
            },
            presence={"active_preset_id": cfg.active_preset_id, "active_app_id": cfg.active_app_id, "applied": applied},
            auto_switch={"enabled": cfg.auto_switch.enabled},
        )

    async def on_rpc_status(self, status: RpcStatus) -> None:
        await self.state.update(
            presence={
                "connection_state": status.state.value,
                "last_successful_update": status.last_successful_update,
                "current_activity": status.published,
            },
            discord={
                "rpc_connected": status.connected_client_id is not None,
                "connected_client_id": status.connected_client_id,
                "last_error": status.last_error,
                "reconnect_attempt": status.reconnect_attempt,
                "next_retry_in": status.next_retry_in,
            },
        )
        await self.refresh_config_state()

    async def _log(self, kind: str, result: OperationResult, source: str) -> OperationResult:
        msg = result.message or ("ok" if result.success else result.code)
        await self.state.record_event(kind, msg, success=result.success, source=source)
        if not result.success:
            log.info("%s from %s failed: %s (%s)", kind, source, result.message, result.code)
        return result

    # ------------------------------------------------------------- presence
    async def enable(self, source: str = "api") -> OperationResult:
        async with self._lock:
            return await self._enable_locked(source)

    async def _enable_locked(self, source: str) -> OperationResult:
        cfg = self.config
        problems = self.configuration_problems(cfg)
        if problems:
            return await self._log("presence.enable", OperationResult.fail("configuration_invalid", "; ".join(problems)), source)
        if not self.manager.desired.enabled or self._session_start is None:
            self._session_start = int(time.time())
        await self.state.update(presence={"enabled": True, "enabled_by": source})
        result = await self.manager.apply(Desired(True, cfg.effective_client_id(), self._desired_kwargs(cfg)))
        await self.refresh_config_state()
        return await self._log("presence.enable", result, source)

    async def disable(self, source: str = "api") -> OperationResult:
        async with self._lock:
            return await self._disable_locked(source)

    async def _disable_locked(self, source: str) -> OperationResult:
        await self.state.update(presence={"enabled": False, "enabled_by": None})
        result = await self.manager.apply(Desired(False))
        self._session_start = None
        if self.config.active_app_id is not None:
            # Presence is off, so no app is ON any more (also not after a restart).
            self._save_config({"active_app_id": None})
            self.state.broadcast("config.updated", self.config.public_dict())
        await self.refresh_config_state()
        return await self._log("presence.disable", result, source)

    async def _republish_if_enabled(self, source: str) -> OperationResult:
        if not self.manager.desired.enabled:
            return OperationResult.ok("Saved (presence is off, nothing to apply)", applied=False)
        cfg = self.config
        problems = self.configuration_problems(cfg)
        if problems:
            return OperationResult.fail("configuration_invalid", "; ".join(problems))
        result = await self.manager.apply(Desired(True, cfg.effective_client_id(), self._desired_kwargs(cfg)))
        await self.refresh_config_state()
        return await self._log("presence.update", result, source)

    async def update_activity(self, activity: Activity, *, apply: bool, client_id: str | None = None,
                              source: str = "api") -> OperationResult:
        async with self._lock:
            patch: dict[str, Any] = {"presence": activity.model_dump(mode="json"), "active_preset_id": None,
                                     "active_app_id": None}
            if client_id is not None:
                patch["presence_client_id"] = client_id
            self._save_config(patch)
            self.state.broadcast("config.updated", self.config.public_dict())
            await self.refresh_config_state()
            if not apply:
                await self.state.record_event("presence.save", "Activity saved", source=source)
                return OperationResult.ok("Saved", applied=False)
            return await self._republish_if_enabled(source)

    async def apply_saved(self, source: str = "api") -> OperationResult:
        async with self._lock:
            return await self._republish_if_enabled(source)

    # --------------------------------------------------------------- config
    def _save_config(self, patch: dict[str, Any]) -> AppConfig:
        try:
            return self.config_store.update(patch)
        except ValidationError as exc:
            raise AppError("validation_error", "Configuration is invalid", status_code=422,
                           details=validation_details(exc)) from exc
        except OSError as exc:
            log.error("could not write configuration: %s", exc)
            raise AppError("config_write_failed", "Could not save the configuration file", status_code=500) from exc

    async def update_config(self, patch: dict[str, Any], source: str = "api") -> AppConfig:
        async with self._lock:
            before = self.config
            patch = {k: v for k, v in patch.items() if k != "schema_version"}
            cfg = self._save_config(patch)
            if cfg.server != before.server:
                restart = cfg.server.bind_host() != before.server.bind_host() or cfg.server.port != before.server.port
                await self.state.update(server={"allow_lan": cfg.server.allow_lan, "restart_required": restart})
            if cfg.rpc != before.rpc:
                self.manager.settings = cfg.rpc
            self.state.broadcast("config.updated", cfg.public_dict())
            await self.state.record_event("config.update", "Settings saved", source=source)
            await self.refresh_config_state()
            if self.manager.desired.enabled and (
                cfg.presence != before.presence or cfg.effective_client_id() != before.effective_client_id()
            ):
                await self._republish_if_enabled(source)
            self._ensure_watcher()
            return cfg

    # -------------------------------------------------------------- presets
    def _broadcast_presets(self) -> None:
        self.state.broadcast("presets.updated", [p.model_dump(mode="json") for p in self.presets.list()])

    async def create_preset(self, data: PresetInput, source: str = "api") -> Preset:
        async with self._lock:
            preset = self.presets.create(data)
            self._broadcast_presets()
            await self.state.record_event("preset.create", f"Preset '{preset.name}' created", source=source)
            return preset

    async def update_preset(self, preset_id: str, data: PresetInput, source: str = "api") -> Preset:
        async with self._lock:
            preset = self.presets.update(preset_id, data)
            self._broadcast_presets()
            await self.state.record_event("preset.update", f"Preset '{preset.name}' updated", source=source)
            return preset

    async def duplicate_preset(self, preset_id: str, source: str = "api") -> Preset:
        async with self._lock:
            preset = self.presets.duplicate(preset_id)
            self._broadcast_presets()
            await self.state.record_event("preset.create", f"Preset '{preset.name}' created", source=source)
            return preset

    async def delete_preset(self, preset_id: str, source: str = "api") -> None:
        async with self._lock:
            preset = self.presets.get(preset_id)
            self.presets.delete(preset_id)
            patch = {key: None for key in ("active_preset_id", "active_app_id")
                     if getattr(self.config, key) == preset_id}
            if patch:
                self._save_config(patch)
                self.state.broadcast("config.updated", self.config.public_dict())
            if self._auto_preset == preset_id:
                self._auto_preset = None
            self._broadcast_presets()
            await self.state.record_event("preset.delete", f"Preset '{preset.name}' deleted", source=source)
            await self.refresh_config_state()

    async def activate_preset(self, preset_id: str, *, enable: bool = False, source: str = "api") -> OperationResult:
        async with self._lock:
            return await self._activate_locked(preset_id, enable=enable, source=source)

    async def _activate_locked(self, preset_id: str, *, enable: bool, source: str,
                               as_app: bool = False) -> OperationResult:
        preset = self.presets.get(preset_id)
        candidate_id = preset.client_id or self.config.application.client_id
        if not candidate_id:
            return await self._log("preset.activate", OperationResult.fail(
                "configuration_invalid", "Preset has no Application ID and no default ID is configured"), source)
        if not preset.activity.has_content():
            return await self._log("preset.activate", OperationResult.fail(
                "configuration_invalid", "Preset activity needs details, state or a large image"), source)
        # Validation passed: only now touch the active configuration.
        self._save_config({
            "presence": preset.activity.model_dump(mode="json"),
            "presence_client_id": preset.client_id,
            "active_preset_id": preset.id,
            "active_app_id": preset.id if as_app else None,
        })
        self.state.broadcast("config.updated", self.config.public_dict())
        await self.refresh_config_state()
        if as_app:
            await self.state.record_event("app.on", f"App '{preset.name}' turned on", source=source)
        else:
            await self.state.record_event("preset.activate", f"Preset '{preset.name}' activated", source=source)
        if enable and not self.manager.desired.enabled:
            return await self._enable_locked(source)
        result = await self._republish_if_enabled(source)
        return result

    # ------------------------------------------------------------------ apps
    async def toggle_app(self, preset_id: str, source: str = "api") -> OperationResult:
        """App section: switch a preset ON (replacing any other app) or OFF."""
        async with self._lock:
            preset = self.presets.get(preset_id)
            if self.config.active_app_id == preset_id and self.manager.desired.enabled:
                result = await self._disable_locked(source)
                await self.state.record_event("app.off", f"App '{preset.name}' turned off", source=source)
                return result
            return await self._activate_locked(preset_id, enable=True, source=source, as_app=True)

    async def restore_app(self) -> None:
        """At startup: turn the app that was ON at shutdown back on."""
        async with self._lock:
            app_id = self.config.active_app_id
            if app_id is None:
                return
            if all(p.id != app_id for p in self.presets.list()):  # deleted while the backend was down
                self._save_config({"active_app_id": None})
                await self.refresh_config_state()
                return
            await self._activate_locked(app_id, enable=True, source="startup", as_app=True)

    # ---------------------------------------------------------------- setup
    async def test_connection(self, client_id: str) -> OperationResult:
        return await self.manager.test_connection(client_id)

    # ---------------------------------------------------------- auto-switch
    def _ensure_watcher(self) -> None:
        if self.config.auto_switch.enabled and (self._watch_task is None or self._watch_task.done()):
            self._watch_task = asyncio.create_task(self._watch_processes(), name="auto-switch")

    def start(self) -> None:
        self._ensure_watcher()

    async def stop(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _watch_processes(self) -> None:
        log.info("process auto-switch started")
        loop = asyncio.get_running_loop()
        while self.config.auto_switch.enabled:
            try:
                running = await loop.run_in_executor(None, running_process_names)
                await self.auto_switch_tick(running)
            except Exception:
                log.exception("auto-switch tick failed")
            await asyncio.sleep(self.config.auto_switch.poll_interval)
        if self._auto_preset is not None:
            self._auto_preset = None
            await self.state.update(auto_switch={"matched_preset_id": None})
        log.info("process auto-switch stopped")

    async def auto_switch_tick(self, running: set[str]) -> None:
        """Activate the first preset whose process is running; turn presence off
        when the matched process exits (only if auto-switch turned it on)."""
        match = None
        for preset in self.presets.list():
            if any(name.lower() in running for name in preset.process_names):
                match = preset
                break
        async with self._lock:
            if self.config.active_app_id is not None:
                return  # an app switched on by hand takes priority over process detection
            if match is not None and match.id != self._auto_preset:
                self._auto_preset = match.id
                await self.state.update(auto_switch={"matched_preset_id": match.id})
                await self._activate_locked(match.id, enable=True, source="auto-switch")
            elif match is None and self._auto_preset is not None:
                self._auto_preset = None
                await self.state.update(auto_switch={"matched_preset_id": None})
                if self.manager.desired.enabled and self.state.snapshot()["presence"]["enabled_by"] == "auto-switch":
                    await self._disable_locked("auto-switch")


def running_process_names() -> set[str]:
    names = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
            if name:
                names.add(name.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names
