"""Preset persistence (``presets.json``)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from discord_pro.backend.activity import Activity, validate_client_id
from discord_pro.backend.errors import NotFoundError
from discord_pro.paths import atomic_write_json

log = logging.getLogger(__name__)

MAX_PRESETS = 200


class PresetInput(BaseModel):
    """Fields a client may set when creating or updating a preset."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    client_id: str = ""  # optional override of the default Application ID
    activity: Activity = Field(default_factory=Activity)
    # Process names (e.g. "Code.exe") that activate this preset when the
    # auto-switch feature is enabled. Ported from the original agents.
    process_names: list[str] = Field(default_factory=list, max_length=20)
    # Logo shown on the preset's card in the App section (https image URL).
    icon: str = Field(default="", max_length=512)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        return (v or "").strip() if isinstance(v, str) else v

    @field_validator("client_id", mode="before")
    @classmethod
    def _cid(cls, v: Any) -> str:
        return validate_client_id(v if v is not None else "")

    @field_validator("process_names")
    @classmethod
    def _procs(cls, v: list[str]) -> list[str]:
        out = []
        for name in v:
            name = name.strip()
            if not name:
                continue
            if len(name) > 128 or any(c in name for c in "/\\\0"):
                raise ValueError("process names must be plain executable names, e.g. Code.exe")
            out.append(name)
        return out

    @field_validator("icon", mode="before")
    @classmethod
    def _icon(cls, v: Any) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            return v  # rejected by the str type check
        v = v.strip()
        if v and (not v.lower().startswith("https://") or len(v) <= 8 or any(c.isspace() for c in v)):
            raise ValueError("icon must be an https:// image URL")
        return v


class Preset(PresetInput):
    id: str
    created_at: float
    updated_at: float


class PresetStore:
    def __init__(self, path: Path):
        self.path = path
        self.warnings: list[str] = []
        self._presets: list[Preset] = self._load()

    def _load(self) -> list[Preset]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("presets", []) if isinstance(raw, dict) else None
            if not isinstance(items, list):
                raise ValueError("expected {\"presets\": [...]}")
        except (OSError, ValueError) as exc:
            self.warnings.append(f"presets file unreadable ({exc}); starting with no presets")
            self._keep_copy("corrupt")
            return []
        presets, invalid = [], []
        for item in items:
            try:
                presets.append(Preset.model_validate(item))
            except ValidationError:
                invalid.append(item)
        if invalid:
            self.warnings.append(f"{len(invalid)} invalid preset(s) skipped; saved to presets.invalid.json")
            atomic_write_json(self.path.with_name("presets.invalid.json"), {"presets": invalid})
        return presets

    def _keep_copy(self, suffix: str) -> None:
        try:
            self.path.with_name(f"{self.path.name}.{suffix}-{int(time.time())}").write_bytes(self.path.read_bytes())
        except OSError:
            pass

    def _save(self, presets: list[Preset]) -> None:
        atomic_write_json(self.path, {"schema_version": 1, "presets": [p.model_dump(mode="json") for p in presets]})
        self._presets = presets

    def list(self) -> list[Preset]:
        return list(self._presets)

    def get(self, preset_id: str) -> Preset:
        for p in self._presets:
            if p.id == preset_id:
                return p
        raise NotFoundError("preset_not_found", f"Preset '{preset_id}' does not exist")

    def create(self, data: PresetInput) -> Preset:
        if len(self._presets) >= MAX_PRESETS:
            raise ValueError(f"at most {MAX_PRESETS} presets are supported")
        now = time.time()
        preset = Preset(**data.model_dump(), id=uuid.uuid4().hex, created_at=now, updated_at=now)
        self._save([*self._presets, preset])
        return preset

    def update(self, preset_id: str, data: PresetInput) -> Preset:
        current = self.get(preset_id)
        updated = Preset(**data.model_dump(), id=current.id, created_at=current.created_at, updated_at=time.time())
        self._save([updated if p.id == preset_id else p for p in self._presets])
        return updated

    def delete(self, preset_id: str) -> None:
        self.get(preset_id)
        self._save([p for p in self._presets if p.id != preset_id])

    def duplicate(self, preset_id: str) -> Preset:
        src = self.get(preset_id)
        data = PresetInput.model_validate({**src.model_dump(include=set(PresetInput.model_fields)), "name": f"{src.name} (copy)"[:64]})
        return self.create(data)
