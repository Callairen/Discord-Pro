"""Error types shared by the backend layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AppError(Exception):
    """An expected, user-facing error with a stable machine-readable code."""

    status_code = 400

    def __init__(self, code: str, message: str, *, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        if status_code is not None:
            self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


class NotFoundError(AppError):
    status_code = 404


class ConflictError(AppError):
    status_code = 409


@dataclass
class OperationResult:
    """Outcome of a command (enable, disable, update, ...)."""

    success: bool
    code: str = "ok"
    message: str = ""
    pending: bool = False  # True when the backend keeps retrying in the background
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "success": self.success,
            "pending": self.pending,
            "error": None if self.success else {"code": self.code, "message": self.message},
        }
        if self.success and self.message:
            out["message"] = self.message
        if self.data:
            out["data"] = self.data
        return out

    @classmethod
    def ok(cls, message: str = "", **data: Any) -> "OperationResult":
        return cls(True, "ok", message, data=data)

    @classmethod
    def fail(cls, code: str, message: str, *, pending: bool = False) -> "OperationResult":
        return cls(False, code, message, pending=pending)
