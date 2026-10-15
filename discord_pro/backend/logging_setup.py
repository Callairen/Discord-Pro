"""Logging configuration with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path

_SECRETS: set[str] = set()
_SECRETS_LOCK = threading.Lock()
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_CODE_RE = re.compile(r"(?i)((?:code|token|access_code)=)[^&\s\"']+")


def register_secret(value: str) -> None:
    """Values registered here are replaced with *** in every log record."""
    if value and len(value) >= 6:
        with _SECRETS_LOCK:
            _SECRETS.add(value)


def redact(text: str) -> str:
    with _SECRETS_LOCK:
        secrets = list(_SECRETS)
    for secret in secrets:
        text = text.replace(secret, "***")
    text = _BEARER_RE.sub(r"\1***", text)
    return _CODE_RE.sub(r"\1***", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: keep the record, unformatted
            return True
        record.msg = redact(message)
        record.args = None
        return True


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    redactor = RedactingFilter()

    if sys.stderr is not None:  # windowed (frozen) builds have no console
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.addFilter(redactor)
        root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "backend.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # uvicorn's access log would print query strings (one-time login codes).
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
