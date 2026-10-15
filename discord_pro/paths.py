"""Filesystem locations and safe file writes shared by backend and GUI."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

APP_DIR_ENV = "DISCORD_PRO_HOME"


def app_data_dir() -> Path:
    """Return the writable per-user application data directory.

    ``DISCORD_PRO_HOME`` overrides the location (used by tests and portable
    installs).
    """
    override = os.environ.get(APP_DIR_ENV)
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / "DiscordPro"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "DiscordPro"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / "discord-pro"
    path.mkdir(parents=True, exist_ok=True)
    return path


def web_dir() -> Path:
    """Directory containing the static dashboard (works when frozen too)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "discord_pro" / "web"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent / "web"


def config_path() -> Path:
    return app_data_dir() / "config.json"


def presets_path() -> Path:
    return app_data_dir() / "presets.json"


def auth_path() -> Path:
    return app_data_dir() / "auth.json"


def instance_path() -> Path:
    return app_data_dir() / "instance.json"


def lock_path() -> Path:
    return app_data_dir() / "backend.lock"


def log_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def restrict_permissions(path: Path) -> None:
    """Best-effort: make a file readable only by the current user (POSIX)."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def atomic_write_json(path: Path, data: Any, *, private: bool = False) -> None:
    """Write JSON atomically: temp file in the same directory + fsync + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if private:
            restrict_permissions(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
