"""Small shared helpers for the Telegram handlers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .client import ApiClient, ApiError, ApiNetworkError, escape_markdown
from .config import Settings, get_settings

__all__ = [
    "URL_RE",
    "actor_for",
    "escape_markdown",
    "extract_url",
    "format_bytes",
    "format_duration",
    "format_video_status",
    "get_client",
    "get_settings_or_default",
    "is_admin",
    "user_message_for",
]

URL_RE = re.compile(
    r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def get_settings_or_default() -> Settings:
    return get_settings()


def get_client(context: Any) -> ApiClient:
    """Fetch the :class:`ApiClient` stored on ``application.bot_data``."""
    client = (context.bot_data or {}).get("api")
    if client is None:
        raise RuntimeError(
            "API client is not initialised — the bot application was not post_init'ed."
        )
    return client


def actor_for(update: Any) -> str:
    """The ``X-Requested-By`` value for a Telegram user."""
    user = getattr(update, "effective_user", None)
    uid = getattr(user, "id", None)
    return f"telegram:{uid}" if uid is not None else "telegram:unknown"


def is_admin(update: Any, context: Any) -> bool:
    """True when the calling Telegram user is on the admin allow-list."""
    settings = (context.bot_data or {}).get("settings") or get_settings()
    user = getattr(update, "effective_user", None)
    return settings.is_admin(getattr(user, "id", None))


def extract_url(text: str | None) -> str | None:
    """Return the first http(s) URL found in ``text``."""
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)")


def format_bytes(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "unknown"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def format_video_status(video: Mapping[str, Any]) -> str:
    """Render a compact status block for a video object."""
    status = video.get("status", "unknown")
    progress = video.get("progress")
    progress_txt = f"{progress}%" if progress is not None else "n/a"
    size_txt = format_bytes(video.get("file_size")) if video.get("file_size") else "n/a"
    dur_txt = format_duration(video.get("duration_seconds")) if video.get("duration_seconds") else "n/a"
    title = escape_markdown(video.get("title") or "(no title)")
    lines = [
        f"*Video* `{video.get('id', '?')}`",
        f"Title: {title}",
        f"Status: `{status}`  |  Progress: {progress_txt}",
        f"Size: {size_txt}  |  Duration: {dur_txt}",
    ]
    if video.get("error"):
        lines.append(f"Error: {escape_markdown(video['error'])}")
    transcripts = video.get("transcripts") or []
    ready = [t for t in transcripts if isinstance(t, Mapping) and t.get("status") == "ready"]
    if transcripts:
        latest = transcripts[0] if isinstance(transcripts[0], Mapping) else {}
        lines.append(
            f"Transcript: {len(transcripts)} version(s), "
            f"{len(ready)} ready, latest v{latest.get('version', '?')}"
        )
    else:
        lines.append("Transcript: none yet")
    return "\n".join(lines)


def user_message_for(exc: BaseException) -> str:
    """Render any exception raised by the API client for a Telegram user."""
    if isinstance(exc, ApiError):
        return exc.user_message()
    if isinstance(exc, ApiNetworkError):
        return exc.user_message()
    return f"[unexpected error] {exc.__class__.__name__}: {exc}"


def is_uuid(value: str | None) -> bool:
    return bool(value) and bool(_UUID_RE.match(value.strip()))
