"""Recording paths: recordings/<slug>/YYYY/MM/DD/<slug>_<timestamp>_<id>.wav."""

from __future__ import annotations

import re
import secrets
from datetime import datetime

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ID_RE = re.compile(r"^[a-f0-9]{8}$")


def slugify(name: str, *, max_len: int = 48) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or "channel"


def new_unique_id() -> str:
    return secrets.token_hex(4)


def is_unique_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))


def build_recording_filename(channel_slug: str, started_local: datetime, unique_id: str) -> str:
    slug = slugify(channel_slug)
    if not is_unique_id(unique_id):
        raise ValueError("unique id must be 8 lowercase hex characters")
    stamp = started_local.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{slug}_{stamp}_{unique_id}.wav"


def build_recording_relative_path(channel_slug: str, started_local: datetime, unique_id: str) -> str:
    slug = slugify(channel_slug)
    filename = build_recording_filename(slug, started_local, unique_id)
    day = started_local.strftime("%Y/%m/%d")
    return f"recordings/{slug}/{day}/{filename}"
