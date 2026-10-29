"""`/status <id>` handler."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import format_video_status, get_client, is_uuid, web_links
from ..keyboards import video_keyboard

logger = logging.getLogger(__name__)

__all__ = ["status_command"]

USAGE = "Usage: /status <video id>  (the id shown when you queued the link)"


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current status of a video."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(USAGE)
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(f"`{video_id}` is not a valid video id.\n{USAGE}", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return

    try:
        video = await client.get_video(video_id)
    except ApiError as exc:
        if exc.status_code == 404:
            await message.reply_text(f"No video with id `{video_id}`.", parse_mode=ParseMode.MARKDOWN)
            return
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    if not video:
        await message.reply_text(f"No video with id `{video_id}`.", parse_mode=ParseMode.MARKDOWN)
        return

    body = format_video_status(video)
    if isinstance(video, Mapping) and video.get("status") in {"downloaded", "transcribed"}:
        body += f"\nGet the file with /media {video_id}"
    await message.reply_text(
        body,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=web_links(update, context, video=video, markup=video_keyboard(video_id)),
    )
