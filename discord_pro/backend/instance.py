"""Single-instance lock and the discovery file clients use to find the backend."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, IO

from discord_pro.paths import atomic_write_json


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """An OS-level exclusive lock on a file; released automatically if the
    process dies, so a crash never leaves a stale lock behind."""

    def __init__(self, path: Path):
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning("another Discord-Pro backend is already running") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def write_instance_file(path: Path, *, port: int, bind_host: str, version: str) -> None:
    atomic_write_json(path, {
        "pid": os.getpid(),
        "port": port,
        "bind_host": bind_host,
        "url": f"http://127.0.0.1:{port}",
        "version": version,
        "started_at": time.time(),
    }, private=True)


def read_instance_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("port"), int) else None
    except (OSError, ValueError):
        return None


def remove_instance_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
