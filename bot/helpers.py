"""Small shared helpers for the Telegram handlers."""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .client import ApiClient, ApiError, ApiNetworkError, escape_markdown
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "URL_RE",
    "AdminIdsCache",
    "actor_for",
    "escape_markdown",
    "extract_url",
    "format_bytes",
    "format_duration",
    "format_video_status",
    "get_client",
    "admin_url",
    "get_settings_or_default",
    "is_admin",
    "portal_button",
    "portal_url",
    "user_message_for",
    "web_links",
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


class AdminIdsCache:
    """Telegram ids of console admins linked to this bot, refreshed from the backend.

    ``bot_data['admin_ids_cache']`` holds the single shared instance. The
    background refresher (see ``bot.__main__``) is the concurrent actor while
    PTB handlers only read ``ids``, so a refresh swaps in a brand-new frozenset
    instead of mutating the old one — a reader either sees the previous set or
    the next one, never a half-updated one. A failed refresh keeps the previous
    set (the env allow-list still applies regardless), so a transient outage
    never silently revokes admin rights.
    """

    def __init__(self) -> None:
        self.ids: frozenset[int] = frozenset()

    async def refresh(self, client: Any) -> None:
        """Pull the linked-admin ids from the backend, swapping ``ids`` atomically."""
        try:
            data = await client.get_admin_ids()
            # Atomic reference swap — never mutate in place.
            self.ids = frozenset(int(item) for item in data or [])
        except (ApiError, ApiNetworkError) as exc:
            logger.warning(
                "admin-ids refresh failed, keeping %s cached id(s): %s",
                len(self.ids),
                exc,
            )


def _admin_cache_ids(context: Any) -> frozenset[int]:
    """The linked-admin ids cached in ``bot_data`` (empty when absent)."""
    cache = (context.bot_data or {}).get("admin_ids_cache")
    return getattr(cache, "ids", None) or frozenset()


def is_admin(update: Any, context: Any) -> bool:
    """True when the calling Telegram user is an admin.

    Admin rights come from two sources, OR'd together: the operator's env
    allow-list (``ADMIN_TELEGRAM_IDS``) and the ids of console admin accounts
    linked to this bot, as refreshed into ``bot_data['admin_ids_cache']``.
    """
    settings = (context.bot_data or {}).get("settings") or get_settings()
    user = getattr(update, "effective_user", None)
    uid = getattr(user, "id", None)
    if settings.is_admin(uid):
        return True
    return uid in _admin_cache_ids(context)


def _join_url(base: str, *segments: Any) -> str:
    """Join a base URL and path segments. Ids are UUIDs/digits, no escaping."""
    return "/".join([base.rstrip("/"), *(str(segment).strip("/") for segment in segments)])


_LOOPBACK_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_LOOPBACK_HOST_SUFFIXES = (".localhost", ".localdomain", ".localhost")
_warned_link_bases: set[str] = set()


def _telegram_accepts_url(url: str) -> bool:
    """True when Telegram will accept ``url`` as an inline-button link.

    Probed against the live Bot API: **loopback** is refused — by name
    (``localhost``) and as an address (``127.0.0.1``, ``[::1]``) — and a
    refused URL fails the entire ``sendMessage`` ("BadRequest: ... is invalid:
    wrong http url"), losing the whole reply. A **private/LAN address is
    accepted** (``http://192.168.1.50:5174/...`` is a valid button), which is
    what local development uses: the operator's phone on the same Wi-Fi can
    open the Mac's portal by LAN IP, while ``localhost`` on a phone would only
    mean the phone itself.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if (parts.scheme or "").lower() not in {"http", "https"} or not parts.hostname:
        return False
    host = parts.hostname.lower().strip(".")
    if host in _LOOPBACK_HOSTNAMES or host.endswith(_LOOPBACK_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A real domain needs a dot; "caddy"/"api" are container names, and a
        # single-label name may even be resolved by a local search domain.
        return "." in host
    # Loopback and link-local are unreachable from Telegram's side; everything
    # else — including RFC1918 LAN ranges — is a working button.
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def _web_button(
    label: str, base: str, env_var: str, *segments: Any
) -> InlineKeyboardButton | None:
    """A URL button for ``base``, or None when Telegram would reject the link.

    Dropping the button keeps the message deliverable; the alternative is a
    BadRequest that loses the whole reply.
    """
    url = _join_url(base, *segments)
    if _telegram_accepts_url(url):
        return InlineKeyboardButton(label, url=url)
    if base not in _warned_link_bases:
        _warned_link_bases.add(base)
        logger.warning(
            "%s=%r is a loopback address, which Telegram refuses in a button "
            "URL; it is hidden. Use this machine's LAN IP (e.g. "
            "http://192.168.1.50:5174) so the link also opens on a phone.",
            env_var,
            base,
        )
    return None


def portal_button(video_id: Any, settings: Settings | None = None) -> InlineKeyboardButton | None:
    """The "Open in portal" button for one unit, or None when unavailable.

    Standalone so a reply that never saw an ``Update`` — the oversized-file
    notice inside ``deliver_media`` — can still carry the link.
    """
    settings = settings or get_settings()
    return _web_button(
        "🌐 Go to portal",
        settings.portal_frontend_url,
        "PORTAL_FRONTEND_URL",
        "video",
        video_id,
    )


def portal_url(video_id: Any, settings: Settings | None = None) -> str | None:
    """The portal page for one unit as plain text, or None when unusable.

    Used where the reply must state the address itself (the "too large for
    Telegram" notice), so the link survives copy-paste even on a client that
    will not render the button.
    """
    settings = settings or get_settings()
    url = _join_url(settings.portal_frontend_url, "video", video_id)
    return url if _telegram_accepts_url(url) else None


def admin_url(video_id: Any, settings: Settings | None = None) -> str | None:
    """The console's page for one unit as plain text, or None when unusable."""
    settings = settings or get_settings()
    url = _join_url(settings.admin_frontend_url, "videos", video_id)
    return url if _telegram_accepts_url(url) else None


def web_links(
    update: Any,
    context: Any,
    *,
    video: Mapping[str, Any] | None = None,
    folder_id: Any = None,
    video_id: Any = None,
    markup: InlineKeyboardMarkup | None = None,
) -> InlineKeyboardMarkup | None:
    """Tappable web buttons for a reply that mentions a video or folder.

    A video gets one portal button, ``/video/<id>`` (shown to everyone, admins
    included): it is the page that can play the video even when the file is too
    large for Telegram to carry, and it works for an unfiled unit too — a
    folder link exists only for a unit that has a folder. A folder-only reply
    (no video id) gets ``/folder/<id>`` instead. The admin console has a
    per-video page of its own, so admins also get a "Go to admin" button;
    folder-only replies skip it because the console has no per-folder route.

    ``folder_id`` / ``video_id`` override the video mapping's values, for call
    sites that know an id but have no video dict (``/transcript <id>``, the
    admin ``/retry`` watch). ``markup`` is an existing keyboard to extend with
    the link row, so its buttons survive. Returns ``markup`` unchanged (None
    when not given) when there is nothing to show — ``reply_markup=None`` is
    Telegram's "no keyboard", so callers can pass the result straight through.

    A link whose base URL is loopback (``localhost``, ``127.0.0.1``, ``[::1]``)
    is dropped rather than sent: the Bot API rejects it with "wrong http url"
    and fails the whole message. A LAN address is accepted, so local
    development uses the machine's LAN IP — see ``_telegram_accepts_url``.
    """
    settings = (context.bot_data or {}).get("settings") or get_settings()

    if folder_id is None and isinstance(video, Mapping):
        folder_id = video.get("folder_id")
    if video_id is None and isinstance(video, Mapping):
        video_id = video.get("id")

    buttons: list[InlineKeyboardButton] = []
    if video_id:
        button = portal_button(video_id, settings)
        if button is not None:
            buttons.append(button)
    elif folder_id:
        button = _web_button(
            "🌐 Open folder",
            settings.portal_frontend_url,
            "PORTAL_FRONTEND_URL",
            "folder",
            folder_id,
        )
        if button is not None:
            buttons.append(button)
    if video_id and is_admin(update, context):
        button = _web_button(
            "🛠 Go to admin",
            settings.admin_frontend_url,
            "ADMIN_FRONTEND_URL",
            "videos",
            video_id,
        )
        if button is not None:
            buttons.append(button)

    if not buttons:
        return markup
    rows = list(markup.inline_keyboard) if markup is not None else []
    return InlineKeyboardMarkup(rows + [buttons])


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
