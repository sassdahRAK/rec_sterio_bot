"""Accept a media file the user sends to the chat.

TikTok and YouTube refuse most server-side downloads, so a link is not always
enough. If the user already has the file, they can send it to the bot and get
the identical pipeline behind it: checksum, audio extraction, transcription.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import actor_for, escape_markdown, format_bytes, get_client
from ..keyboards import video_keyboard
from .links import WatchRegistry

logger = logging.getLogger(__name__)

__all__ = ["handle_media_upload"]

# Telegram's Bot API caps a bot's file download at 20 MB.
TELEGRAM_MAX_DOWNLOAD = 20 * 1024 * 1024

ALLOWED_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
}


def _pick_media(message) -> tuple[object, str] | None:
    """Return (telegram file object, filename) for a media/document message."""
    if message.video is not None:
        return message.video, message.video.file_name or "upload.mp4"
    if message.audio is not None:
        return message.audio, message.audio.file_name or "upload.mp3"
    if message.document is not None:
        name = message.document.file_name or "upload.bin"
        suffix = name[name.rfind("."):].lower() if "." in name else ""
        if suffix in ALLOWED_SUFFIXES:
            return message.document, name
        return None
    return None


async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload a file the user sent to the backend and watch its pipeline."""
    message = update.effective_message
    if message is None:
        return

    picked = _pick_media(message)
    if picked is None:
        await message.reply_text(
            "That file type is not supported. Send a video or audio file "
            "(mp4, mov, webm, mkv, mp3, m4a, wav, flac or ogg)."
        )
        return

    telegram_file, filename = picked

    declared = getattr(telegram_file, "file_size", None)
    if isinstance(declared, int) and declared > TELEGRAM_MAX_DOWNLOAD:
        await message.reply_text(
            f"That file is {format_bytes(declared)}. Telegram only lets a bot "
            f"download up to {format_bytes(TELEGRAM_MAX_DOWNLOAD)} — upload larger "
            "files from the admin dashboard instead."
        )
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[not_ready] {exc}")
        return

    notice = await message.reply_text(f"Uploading {filename}…")
    try:
        handle = await telegram_file.get_file()
        payload = await handle.download_as_bytearray()
    except Exception as exc:  # noqa: BLE001 - a failed download must not crash
        logger.warning("could not fetch the Telegram file: %s", exc)
        await notice.edit_text("I could not download that file from Telegram. Try again.")
        return

    try:
        video = await client.upload_video(
            bytes(payload),
            filename,
            title=getattr(telegram_file, "file_name", None),
            requested_by=actor_for(update),
            start_transcription=True,
        )
    except ApiError as exc:
        await notice.edit_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await notice.edit_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    video_id = str(video.get("id") or "")
    if not video_id:
        await notice.edit_text(
            "[unexpected_response] The backend accepted the file but returned no video id."
        )
        return

    await notice.edit_text(
        f"✅ Uploaded — video id `{video_id}`\n"
        f"Size: {format_bytes(video.get('file_size'))}  |  "
        f"Status: `{video.get('status')}`\n\n"
        f"I will notify you when the transcript is ready.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=video_keyboard(video_id),
    )

    watchers: WatchRegistry = (context.bot_data or {}).get("watchers")
    if watchers is None:
        watchers = WatchRegistry()
        context.bot_data["watchers"] = watchers

    from .links import watch_video

    settings = (context.bot_data or {}).get("settings")
    watchers.start(
        video_id,
        watch_video(
            client,
            context.bot,
            message.chat_id,
            video_id,
            interval=getattr(settings, "poll_interval_seconds", 5.0),
            timeout=getattr(settings, "poll_timeout_seconds", 1200.0),
        ),
    )
