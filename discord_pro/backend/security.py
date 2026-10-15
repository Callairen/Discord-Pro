"""Authentication, network policy and request hardening.

Authentication model (same rules for REST and WebSocket):

* **API token** - random secret in ``auth.json`` (user-only permissions).
  Local clients that can read that file (the desktop GUI, the CLI) send it as
  ``Authorization: Bearer <token>``. It is never sent to browsers.
* **Browser session** - an HttpOnly, SameSite=Strict cookie issued by
  ``/auth/redeem`` (one-time link created by the GUI/CLI) or by
  ``/api/v1/auth/login`` with the *access code* shown in the GUI. JavaScript
  never sees a secret.
* Cookie-authenticated state-changing requests must carry the
  ``X-Discord-Pro: 1`` header and, when present, a same-origin ``Origin``
  header (CSRF protection; the custom header cannot be sent cross-site
  without a CORS preflight, which this server never grants).

Network policy:

* The ``Host`` header must be ``localhost`` or an IP literal. Domain names are
  refused, which defeats DNS-rebinding attacks against the local service.
* With LAN access disabled only loopback peers are accepted, even if the
  socket were bound more widely.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_pro.backend.logging_setup import register_secret
from discord_pro.paths import atomic_write_json

SESSION_COOKIE = "dp_session"
SESSION_TTL = 12 * 3600
LOGIN_CODE_TTL = 120
CSRF_HEADER = "x-discord-pro"
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_access_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_code(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


class AuthStore:
    """Persists the API token and the access code."""

    def __init__(self, path: Path):
        self.path = path
        data = self._load()
        self.api_token: str = data["api_token"]
        self.access_code: str = data["access_code"]
        register_secret(self.api_token)
        register_secret(self.access_code)
        register_secret(normalize_code(self.access_code))

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data.get("api_token"), str) and len(data["api_token"]) >= 32 \
                    and isinstance(data.get("access_code"), str) and len(normalize_code(data["access_code"])) == 12:
                return data
        except (OSError, ValueError, AttributeError):
            pass
        data = {"api_token": secrets.token_urlsafe(32), "access_code": _new_access_code()}
        atomic_write_json(self.path, data, private=True)
        return data

    def rotate_access_code(self) -> str:
        self.access_code = _new_access_code()
        register_secret(self.access_code)
        register_secret(normalize_code(self.access_code))
        atomic_write_json(self.path, {"api_token": self.api_token, "access_code": self.access_code}, private=True)
        return self.access_code

    def check_token(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.api_token.encode())

    def check_access_code(self, code: str) -> bool:
        return hmac.compare_digest(normalize_code(code).encode(), normalize_code(self.access_code).encode())


@dataclass
class SessionManager:
    sessions: dict[str, float] = field(default_factory=dict)
    login_codes: dict[str, float] = field(default_factory=dict)

    def create_session(self) -> str:
        self._purge()
        sid = secrets.token_urlsafe(32)
        register_secret(sid)
        self.sessions[sid] = time.time() + SESSION_TTL
        return sid

    def valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        expiry = self.sessions.get(sid)
        if expiry is None or expiry < time.time():
            self.sessions.pop(sid, None)
            return False
        return True

    def revoke(self, sid: str | None) -> None:
        if sid:
            self.sessions.pop(sid, None)

    def revoke_all(self) -> None:
        self.sessions.clear()

    def create_login_code(self) -> str:
        self._purge()
        code = secrets.token_urlsafe(24)
        register_secret(code)
        self.login_codes[code] = time.time() + LOGIN_CODE_TTL
        return code

    def redeem_login_code(self, code: str) -> bool:
        expiry = self.login_codes.pop(code, None)  # single use
        return expiry is not None and expiry >= time.time()

    def _purge(self) -> None:
        now = time.time()
        for store in (self.sessions, self.login_codes):
            for key in [k for k, exp in store.items() if exp < now]:
                store.pop(key, None)


class RateLimiter:
    """Sliding-window limiter keyed by client address."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        """Record a hit; return False when the limit is exceeded."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


def is_loopback(address: str | None) -> bool:
    if not address:
        return False
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def split_host(host_header: str) -> str:
    host = host_header.strip()
    if host.startswith("["):  # [::1]:8765
        return host[1:host.find("]")] if "]" in host else host
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def host_allowed(host_header: str | None, *, allow_lan: bool) -> bool:
    """Accept only 'localhost' or IP literals (DNS-rebinding defence)."""
    if not host_header:
        return False
    host = split_host(host_header).lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or (allow_lan and (ip.is_private or ip.is_link_local))


def peer_allowed(peer: str | None, *, allow_lan: bool) -> bool:
    if is_loopback(peer):
        return True
    if not allow_lan or not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """A browser Origin must match the Host the request was sent to."""
    if origin is None:
        return True  # non-browser client
    if not host_header:
        return False
    return origin.lower() in (f"http://{host_header.lower()}", f"https://{host_header.lower()}")


SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def bearer_token(headers: Any) -> str | None:
    value = headers.get("authorization")
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None
