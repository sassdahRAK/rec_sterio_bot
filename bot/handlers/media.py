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
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import escape_markdown, format_bytes, get_client, is_uuid

logger = logging.getLogger(__name__)

__all__ = ["media_command", "deliver_media"]

USAGE = "Usage: /media <video id>  (the id shown when you queued the link)"

# Telegram's Bot API upload limit for bots is 50 MB.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _filename(video: Mapping[str, Any], video_id: str) -> str:
    title = str(video.get("title") or "").strip()
    if not title:
        return f"{video_id}.mp4"
    # Keep the extension the backend recorded, else fall back to mp4.
    safe = "".join(ch for ch in title if ch.isalnum() or ch in " ._-")[:80].strip()
    return f"{safe or video_id}.mp4"


def _explain_missing_media(video: Mapping[str, Any], video_id: str) -> str:
    """Explain why there is no file yet, in terms the user can act on."""
    status = str(video.get("status") or "") if isinstance(video, Mapping) else ""

    if status in {"pending", "downloading"}:
        progress = video.get("progress")
        pct = f" ({progress}%)" if isinstance(progress, int) else ""
        return (
            f"⏳ The download is still in progress{pct} — the file is not on disk yet.\n"
            f"Video `{video_id}`. Try /media again in a moment, or /status {video_id}."
        )

    if status in {"failed", "quarantined"}:
        error = str(video.get("error") or "").strip()
        lines = [f"❌ The download failed for video `{video_id}`, so there is no file to send."]
        if error:
            lines.append(f"Reason: {escape_markdown(error[:400])}")
        if "tiktok" in error.lower() or "unexpected response" in error.lower():
            lines.append(
                "This is TikTok's bot check, not a problem with your link. "
                "TikTok now refuses most server-side requests."
            )
        elif "unavailable" in error.lower() or "private" in error.lower():
            lines.append(
                "The video looks unavailable, private, or region-locked. "
                "Only public content that you have the rights to can be fetched."
            )
        lines.append("Ask an admin to retry with /retry, or send a different public link.")
        return "\n\n".join(lines)

    if status == "transcribing":
        return (
            f"⏳ The media for `{video_id}` is being transcribed. The file itself "
            f"should already exist — try /media again, or /status {video_id}."
        )

    return (
        f"The media for `{video_id}` is not on disk"
        + (f" (status: `{status}`)" if status else "")
        + ". Check /status for details."
    )


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
            parse_mode=ParseMode.MARKDOWN,
        )
        return False

    try:
        payload = await client.get_media(video_id)
    except ApiError as exc:
        if exc.status_code == 404:
            # A 404 means there is no file on disk. Say *why*: still working on
            # it, or the download failed and here is the reason. "Not available
            # yet" alone leaves the user with no idea what to do next.
            await bot.send_message(
                chat_id=chat_id,
                text=_explain_missing_media(video, video_id),
                parse_mode=ParseMode.MARKDOWN,
            )
            return False
        await bot.send_message(
                chat_id=chat_id, text=exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
        return False
    except ApiNetworkError as exc:
        await bot.send_message(
                chat_id=chat_id, text=exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
        return False

    if not payload:
        await bot.send_message(
            chat_id=chat_id, text=f"The media for `{video_id}` is empty.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False

    filename = _filename(video, video_id) if isinstance(video, Mapping) else f"{video_id}.mp4"
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=payload,
            filename=filename,
            caption=f"Video `{video_id}` — {format_bytes(len(payload))}",
            parse_mode=ParseMode.MARKDOWN,
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
        await message.reply_text(
            f"`{video_id}` is not a valid video id.\n{USAGE}", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return

    await message.reply_text(f"Fetching the media for `{video_id}`…", parse_mode=ParseMode.MARKDOWN)
    await deliver_media(context.bot, message.chat_id, client, video_id)
