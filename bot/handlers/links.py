"""Plain video-link messages: create the video, then watch it in the background."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiClient, ApiError, ApiNetworkError
from ..helpers import (
    BOT_NOT_READY,
    actor_for,
    escape_markdown,
    extract_url,
    get_client,
    web_links,
)
from ..keyboards import video_keyboard
from .cookies import CONSENT_REQUIRED_HINT
from .media import deliver_media

logger = logging.getLogger(__name__)

__all__ = ["WatchRegistry", "handle_link", "start_watch", "watch_video"]

TERMINAL_STATUSES = frozenset({"transcribed", "failed", "quarantined"})
DOWNLOAD_DONE_STATUSES = frozenset({"downloaded"})


class WatchRegistry:
    """Keeps track of the background polling tasks so a video is never polled twice."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def is_watching(self, video_id: str) -> bool:
        task = self._tasks.get(video_id)
        return task is not None and not task.done()

    def start(self, video_id: str, coro: Awaitable[Any]) -> bool:
        """Schedule ``coro`` as a tracked task. Returns False if already watching."""
        if self.is_watching(video_id):
            return False
        task = asyncio.ensure_future(coro)
        self._tasks[video_id] = task
        task.add_done_callback(lambda t: self._forget(video_id, t))
        return True

    def _forget(self, video_id: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(video_id) is task:
            self._tasks.pop(video_id, None)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("watch task for %s failed: %s", video_id, exc)

    def cancel(self, video_id: str) -> None:
        task = self._tasks.pop(video_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def cancel_all(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("watch task raised during shutdown: %s", exc)


def _video_id_from(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        if payload.get("id"):
            return str(payload["id"])
        nested = payload.get("video")
        if isinstance(nested, Mapping) and nested.get("id"):
            return str(nested["id"])
    return None


def _transcript_summary(video: Mapping[str, Any]) -> str:
    transcripts = video.get("transcripts") or []
    if not transcripts:
        return "not created yet"
    ready = [t for t in transcripts if isinstance(t, Mapping) and t.get("status") == "ready"]
    if not ready:
        statuses = ", ".join(
            str(t.get("status")) for t in transcripts if isinstance(t, Mapping)
        )
        return f"pending ({statuses or 'unknown'})"
    latest = ready[0]
    return f"ready (v{latest.get('version', '?')}, source={latest.get('source', '?')})"


async def watch_video(
    client: ApiClient,
    bot: Any,
    chat_id: int,
    video_id: str,
    *,
    interval: float = 5.0,
    timeout: float = 1200.0,
    deliver: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll a video until it settles, notifying the user along the way.

    ``deliver`` (the default) announces the finished download and sends the
    file to the chat right away — the drop-a-link flow. A direct upload
    passes False: the user already has the file, so the watch stays silent
    until the terminal notification ("Transcript ready" or the failure
    equivalent).

    ``reply_markup`` is the web-link keyboard the starting flow computed for
    the user who queued the video, shown under every notification (and the
    delivered file) this watch sends.

    Returns the final observed status. Never raises for expected API errors.
    """
    deadline = clock() + timeout
    notified_download = False
    status = "unknown"

    while clock() < deadline:
        try:
            video = await client.get_video(video_id)
        except ApiError as exc:
            logger.info("watch %s: api error %s", video_id, exc.code)
            await _safe_send(
                bot,
                chat_id,
                f"⚠️ Could not check video `{video_id}`: {exc.user_message()}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return f"error:{exc.code}"
        except ApiNetworkError as exc:
            logger.info("watch %s: network error", video_id)
            await _safe_send(
                bot,
                chat_id,
                f"⚠️ Lost contact with the backend: {exc.user_message()}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return "error:network_error"

        status = str(video.get("status") or "unknown")

        if status in TERMINAL_STATUSES:
            await _notify_terminal(bot, chat_id, client, video_id, video, status, reply_markup=reply_markup)
            return status

        if deliver and status in DOWNLOAD_DONE_STATUSES and not notified_download:
            notified_download = True
            await _notify_downloaded(bot, chat_id, client, video_id, video, reply_markup=reply_markup)
            # instruction.txt #2: the finished file goes to the chat right away,
            # not only on request. A delivery failure must never kill the watch.
            try:
                await deliver_media(bot, chat_id, client, video_id, video=video, reply_markup=reply_markup)
            except Exception as exc:  # noqa: BLE001 - keep polling regardless
                logger.warning("could not deliver media for %s: %s", video_id, exc)

        await sleep(interval)

    await _safe_send(
        bot,
        chat_id,
        f"⏳ Video `{video_id}` is still processing (status: {status}). "
        f"Check back with /status {video_id}",
        parse_mode=ParseMode.MARKDOWN,
    )
    return "timeout"


async def _safe_send(bot: Any, chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as exc:  # noqa: BLE001 - never let a notify failure kill the task
        logger.warning("could not notify chat %s: %s", chat_id, exc)


async def _notify_downloaded(
    bot: Any,
    chat_id: int,
    client: ApiClient,
    video_id: str,
    video: Mapping[str, Any],
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    text = (
        "✅ *Download complete*\n"
        f"Video id: `{video_id}`\n"
        f"Transcript: {_transcript_summary(video)}\n\n"
        f"Use /media {video_id} to get the file, "
        f"/transcript {video_id} to read it, or /status {video_id} for details."
    )
    await _safe_send(bot, chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)


async def _notify_terminal(
    bot: Any,
    chat_id: int,
    client: ApiClient,
    video_id: str,
    video: Mapping[str, Any],
    status: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if status == "failed":
        text = (
            f"❌ *Processing failed* for video `{video_id}`\n"
            f"Error: {escape_markdown(video.get('error') or 'unknown error')}\n"
            "Ask an admin to retry the job with /retry."
        )
    else:
        text = (
            f"✅ *Transcript ready* for video `{video_id}`\n"
            f"Transcript: {_transcript_summary(video)}\n\n"
            f"Use /transcript {video_id} to read it, or /media {video_id} for the file."
        )
    await _safe_send(bot, chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)


def start_watch(
    context: ContextTypes.DEFAULT_TYPE,
    client: ApiClient,
    chat_id: int,
    video_id: str,
    *,
    deliver: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Register the background watch for ``video_id`` on the shared registry.

    Every flow that puts a video through the pipeline — a link, a file upload,
    an admin retry — starts its watch through here, so a video is never
    polled twice and the poll settings live in one place. ``deliver`` is
    passed through to ``watch_video``: False keeps a watch silent about the
    download step, for a direct upload whose file the user already has.
    ``reply_markup`` is the web-link keyboard computed by the starting flow,
    shown under each notification the watch sends. Returns False when a watch
    is already running for the video.
    """
    bot_data = context.bot_data or {}
    watchers: WatchRegistry = bot_data.get("watchers")
    if watchers is None:
        watchers = WatchRegistry()
        context.bot_data["watchers"] = watchers

    settings = bot_data.get("settings")
    started = watchers.start(
        video_id,
        watch_video(
            client,
            context.bot,
            chat_id,
            video_id,
            interval=getattr(settings, "poll_interval_seconds", 5.0),
            timeout=getattr(settings, "poll_timeout_seconds", 1200.0),
            deliver=deliver,
            reply_markup=reply_markup,
        ),
    )
    if not started:
        logger.info("already watching video %s — not starting a second watcher", video_id)
    return started


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a plain text message that may contain a video link."""
    message = update.effective_message
    if message is None:
        return
    text = message.text or message.caption or ""

    url = extract_url(text)
    if not url:
        await message.reply_text(
            "I did not find a link in that message. Send me a public video URL "
            "(http:// or https://) and I will queue it for download."
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    try:
        payload = await client.create_video(url, requested_by=actor_for(update))
    except ApiError as exc:
        if getattr(exc, "code", "") == "consent_required":
            # The platform needs the user's own session: point at /cookies
            # instead of dumping the raw error envelope at them.
            await message.reply_text(CONSENT_REQUIRED_HINT)
            return
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    video_id = _video_id_from(payload)
    if not video_id:
        await message.reply_text(
            "[unexpected response] The backend accepted the link but returned no video id."
        )
        return

    # The web-link buttons are computed once and travel with the watch, so
    # every later notification ("Download complete", "Transcript ready", the
    # delivered file) carries the same links as this reply.
    links = web_links(update, context, video=payload, markup=video_keyboard(video_id))
    await message.reply_text(
        f"Queued for download — video id `{video_id}`\n"
        f"I will notify you when it is ready. Use /status {video_id} any time.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=links,
    )

    start_watch(context, client, message.chat_id, video_id, reply_markup=links)
