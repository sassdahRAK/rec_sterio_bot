"""`/media <id>` — deliver the downloaded video to the Telegram chat.

The media endpoint is authenticated, so a plain URL handed to the user would
just return 401 in their browser. Instead the bot fetches the bytes through
its own service token and uploads them to the chat.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import format_bytes, get_client, is_uuid

logger = logging.getLogger(__name__)

__all__ = ["media_command", "deliver_media"]

USAGE = "Usage: /media <video_id>  (the id shown when you queued the link)"

# Telegram's Bot API upload limit for bots is 50 MB.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _filename(video: Mapping[str, Any], video_id: str) -> str:
    title = str(video.get("title") or "").strip()
    if not title:
        return f"{video_id}.mp4"
    # Keep the extension the backend recorded, else fall back to mp4.
    safe = "".join(ch for ch in title if ch.isalnum() or ch in " ._-")[:80].strip()
    return f"{safe or video_id}.mp4"


async def deliver_media(
    bot: Any, chat_id: int, client: Any, video_id: str, video: Mapping[str, Any] | None = None
) -> bool:
    """Fetch the media through the API and upload it to the chat.

    Returns True when the file was delivered.
    """
    if video is None:
        try:
            video = await client.get_video(video_id)
        except (ApiError, ApiNetworkError):
            video = {}

    size = video.get("file_size") if isinstance(video, Mapping) else None
    if isinstance(size, int) and size > MAX_UPLOAD_BYTES:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"The file is {format_bytes(size)} — too large for Telegram "
                f"(limit {format_bytes(MAX_UPLOAD_BYTES)}). "
                f"An admin can download it from the dashboard (video `{video_id}`)."
            ),
        )
        return False

    try:
        payload = await client.get_media(video_id)
    except ApiError as exc:
        if exc.status_code == 404:
            await bot.send_message(
                chat_id=chat_id,
                text=f"The media for `{video_id}` is not available yet.",
            )
            return False
        await bot.send_message(chat_id=chat_id, text=exc.user_message())
        return False
    except ApiNetworkError as exc:
        await bot.send_message(chat_id=chat_id, text=exc.user_message())
        return False

    if not payload:
        await bot.send_message(
            chat_id=chat_id, text=f"The media for `{video_id}` is empty."
        )
        return False

    filename = _filename(video, video_id) if isinstance(video, Mapping) else f"{video_id}.mp4"
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=payload,
            filename=filename,
            caption=f"Video `{video_id}` — {format_bytes(len(payload))}",
        )
    except Exception as exc:  # noqa: BLE001 - a failed upload must not crash the bot
        logger.warning("could not upload media for %s: %s", video_id, exc)
        await bot.send_message(
            chat_id=chat_id,
            text=f"Could not upload the file: {exc}",
        )
        return False
    return True


async def media_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the downloaded video to the chat."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(USAGE)
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(f"`{video_id}` is not a valid video id.\n{USAGE}")
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[not_ready] {exc}")
        return

    await message.reply_text(f"Fetching the media for `{video_id}`…")
    await deliver_media(context.bot, message.chat_id, client, video_id)
