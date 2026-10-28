"""`/start` and `/help` handlers."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import format_video_status, get_client, is_uuid
from ..keyboards import help_keyboard

logger = logging.getLogger(__name__)

WELCOME = (
    "🎬 *recsterio bot*\n\n"
    "Send me a link to a public video (YouTube, TikTok, Facebook, Instagram or a "
    "direct media URL) and I will:\n"
    "1. queue it for download,\n"
    "2. notify you when the download finishes, and\n"
    "3. transcribe it so you can read / correct the transcript.\n\n"
    "*Commands*\n"
    "/status <video id> — progress, size, duration, transcript availability\n"
    "/media <video id> — send the downloaded file to this chat\n"
    "/transcript <video id> — the current transcript (text, or a .txt file when long)\n"
    "/transcripts <video id> — every transcript version\n"
    "/edit <video id> — correct the transcript; send the new text next\n"
    "/cancel — abandon a pending edit\n"
    "/merge <video id> — reconcile Whisper with a Gemini transcript for higher accuracy\n"
    "/merges <video id> — merge history, with an Apply button for ready ones\n"
    "/apply <merge id> — make a merged transcript the current version\n"
    "/help — this message\n\n"
    "*TvNa records* (instruction schema: ID, platform, author, caption, place…)\n"
    "/collect — guided wizard: paste the link, name the place, save the record\n"
    "/record <video id> — show one item's TvNa record\n"
    "/records — the most recent records\n\n"
    "*Dataset* (admins only — the bot's token can see private folders)\n"
    "/folders [parent id] — the folder tree, with visibility and video counts\n"
    "/folder <folder id> — one folder, its sub-folders and its role\n"
    "/cards [folder id] [search] — dataset cards, raw and cleaned\n"
    "/cleaned [folder id] — only the hand-corrected units\n"
    "/mkdir <name> [--public] [--parent <id>] — create a folder\n"
    "/share <folder id> <role> [days] — issue a share link\n"
    "/passcode <folder id> [label] [--uses N] [--devices N] [--one-time] [--days N]\n"
    "            — issue a door code (shown once; the server stores only a hash)\n"
    "/zip <folder id> — export the folder and its sub-folders as one archive\n\n"
    "Just paste a link and wait. Only public content you have the rights to."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message; also handles the `/start <video_id>` deep link."""
    message = update.effective_message
    if message is None:
        return

    args = [a for a in (context.args or []) if a]
    if args and is_uuid(args[0]):
        await _reply_video(update, context, args[0].strip())
        return

    await message.reply_text(
        WELCOME,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=help_keyboard(),
    )


async def _reply_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str) -> None:
    message = update.effective_message
    try:
        client = get_client(context)
    except Exception:  # pragma: no cover - defensive
        client = None
    if client is None:
        await message.reply_text(
            f"Video `{video_id}` — (backend unavailable)", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        video = await client.get_video(video_id)
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    await message.reply_text(
        format_video_status(video),
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/help` — the same guidance as `/start`."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        WELCOME,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=help_keyboard(),
    )
