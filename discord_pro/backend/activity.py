"""Rich Presence activity model and payload validation.

Limits:
- Buttons: max 2, label 1-32 chars, URL 1-512 chars (Discord developer docs,
  Activity object / Activity Buttons).
- Text fields (details, state, image tooltips): 2-128 chars. The docs do not
  publish these limits; Discord's RPC server rejects shorter/longer values
  with "child ... fails because ..." errors, so we enforce them up front.
- Image fields: an uploaded Art Asset key or an http(s) URL, max 256 chars.
- Update rate: Discord allows 5 activity updates per 20 seconds; the RPC
  manager enforces a minimum interval between SET_ACTIVITY calls.

The limits live in one place so they can be adjusted if Discord changes them.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TEXT_MIN = 2
TEXT_MAX = 128
IMAGE_MAX = 256
MAX_BUTTONS = 2
BUTTON_LABEL_MAX = 32
BUTTON_URL_MAX = 512
CLIENT_ID_RE = re.compile(r"^\d{17,20}$")
ASSET_KEY_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,256}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ActivityType(IntEnum):
    """Activity types a desktop RPC client may set (streaming/custom excluded:
    streaming needs a Twitch/YouTube URL and custom status is user-only)."""

    PLAYING = 0
    LISTENING = 2
    WATCHING = 3
    COMPETING = 5


def validate_client_id(value: str, *, allow_empty: bool = True) -> str:
    value = (value or "").strip()
    if not value and allow_empty:
        return ""
    if not CLIENT_ID_RE.match(value):
        raise ValueError("Application ID must be the 17-20 digit number from the Discord Developer Portal")
    return value


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("must be a string")
    value = _CONTROL_RE.sub(" ", value).strip()
    return value


def _check_http_url(url: str, max_len: int) -> str:
    url = _clean_text(url)
    if not url:
        raise ValueError("URL is required")
    if len(url) > max_len:
        raise ValueError(f"URL must be at most {max_len} characters")
    if any(ch.isspace() for ch in url):
        raise ValueError("URL must not contain spaces")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("URL must include a host name")
    if parts.username or parts.password:
        raise ValueError("URL must not contain credentials")
    return url


class ActivityButton(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=BUTTON_LABEL_MAX)
    url: str

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, v: Any) -> str:
        return _clean_text(v)

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, v: Any) -> str:
        return _check_http_url(v, BUTTON_URL_MAX)


class Activity(BaseModel):
    """Editable Rich Presence activity (what the user configures)."""

    model_config = ConfigDict(extra="forbid")

    activity_type: ActivityType = ActivityType.PLAYING
    details: str = ""
    state: str = ""
    large_image: str = ""
    large_text: str = ""
    small_image: str = ""
    small_text: str = ""
    buttons: list[ActivityButton] = Field(default_factory=list, max_length=MAX_BUTTONS)
    show_elapsed: bool = True

    @field_validator("details", "state", "large_text", "small_text", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        v = _clean_text(v)
        if v and len(v) < TEXT_MIN:
            raise ValueError(f"must be empty or at least {TEXT_MIN} characters")
        if len(v) > TEXT_MAX:
            raise ValueError(f"must be at most {TEXT_MAX} characters")
        return v

    @field_validator("large_image", "small_image", mode="before")
    @classmethod
    def _image(cls, v: Any) -> str:
        v = _clean_text(v)
        if not v:
            return ""
        if v.lower().startswith(("http://", "https://")):
            return _check_http_url(v, IMAGE_MAX)
        if not ASSET_KEY_RE.match(v):
            raise ValueError("must be an Art Asset key (letters, digits, _ - .) or an http(s) image URL")
        return v

    @model_validator(mode="after")
    def _tooltips_need_images(self) -> "Activity":
        if self.small_text and not self.small_image:
            raise ValueError("small image tooltip requires a small image")
        return self

    def has_content(self) -> bool:
        """Discord shows nothing useful for an activity without any text/image."""
        return bool(self.details or self.state or self.large_image)

    def to_rpc_kwargs(self, start: int | None) -> dict[str, Any]:
        """Keyword arguments for pypresence ``update`` (only set fields)."""
        out: dict[str, Any] = {"activity_type": int(self.activity_type)}
        for key in ("details", "state", "large_image", "large_text", "small_image", "small_text"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.buttons:
            out["buttons"] = [{"label": b.label, "url": b.url} for b in self.buttons]
        if self.show_elapsed and start:
            out["start"] = int(start)
        return out
