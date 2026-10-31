"""`/status <id>` handler."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import (
    BOT_NOT_READY,
    actor_for,
    format_video_status,
    get_client,
    is_uuid,
    portal_token_for,
    web_links,
)
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
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    # The card is fetched as this Telegram user, not as the service token, so
    # the backend can refuse a unit they have no right to — and any 403 below
    # stops the reply before a portal link is ever minted for it.
    requested_by = actor_for(update)
    try:
        video = await client.get_video(video_id, requested_by=requested_by)
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
    # The ?t= token is minted once here and shared by this reply's button —
    # an unfiled unit's page is otherwise empty for a browser visitor. The
    # same identity is sent along; a 403 means no portal button at all.
    portal = await portal_token_for(
        client, video_id, requested_by=requested_by
    )
    await message.reply_text(
        body,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=web_links(
            update,
            context,
            video=video,
            markup=video_keyboard(video_id),
            token=portal.token,
            allow_portal=portal.linkable(confirmed=bool(video)),
        ),
    )
