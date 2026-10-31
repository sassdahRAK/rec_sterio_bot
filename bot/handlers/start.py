"""`/start` and `/help` handlers."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import actor_for, format_video_status, get_client, is_uuid
from ..keyboards import help_keyboard
from . import identity

logger = logging.getLogger(__name__)

# `/start` is the front door: a short purpose-first page that says why the bot
# exists and gives the few commands a first-time user needs to begin.
WELCOME = (
    "🎬 *RecSterio*\n\n"
    "Turn a video you found into something you can actually use again.\n\n"
    "Paste a YouTube, TikTok, Facebook, Instagram or direct media link, or send "
    "a video file, and I will:\n"
    "1. save the video and its audio,\n"
    "2. transcribe the Khmer and English speech, and\n"
    "3. keep all three together as one unit you can open, read and correct.\n\n"
    "Why keep it here: a link is only as reliable as the feed it lives in. Once it "
    "scrolls away, the caption is gone, the audio cannot be quoted, and nothing "
    "can be searched. RecSterio keeps the original, writes down what was said, "
    "and lets you fix mistakes, so the clip stays useful long after the post "
    "disappears.\n\n"
    "*Quick start*\n"
    "• Paste a link: I download it and start transcribing\n"
    "• /collect: save a clip with where it was filmed, how it was found and why "
    "it matters\n"
    "• /cookies: one-time setup so TikTok and Facebook links work with your own "
    "session\n"
    "• /media <id>: get the stored video or audio back in this chat\n"
    "• /status <id>: progress, and whether the transcript is ready\n"
    "• /help: every command, including transcripts, merges and folders\n\n"
    "Only public content you have the rights to."
)

# `/help` keeps the full command reference: every command, its arguments and a
# one-line description, grouped by area. It is the complete version of the
# quick start above, which deliberately stops at "/help — everything else".
HELP = (
    "🎬 *recsterio bot*\n\n"
    "Send me a link to a public video (YouTube, TikTok, Facebook, Instagram or a "
    "direct media URL) and I will:\n"
    "1. queue it for download,\n"
    "2. notify you when the download finishes, and\n"
    "3. transcribe it so you can read / correct the transcript.\n\n"
    "*Commands*\n"
    "/status <video id>: progress, size, duration, transcript availability\n"
    "/whoami: your identity, links, and bot admin status\n"
    "/media <video id>: send the downloaded file to this chat\n"
    "/audio <video id>: the stored mp3, sent on request\n"
    "/transcript <video id>: the current transcript (text, or a .txt file when long)\n"
    "/transcripts <video id>: every transcript version\n"
    "/edit <video id>: correct the transcript; send the new text next\n"
    "/cancel: abandon a pending edit\n"
    "/merge <video id>: reconcile Whisper with a Gemini transcript for higher accuracy\n"
    "/merges <video id>: merge history, with an Apply button for ready ones\n"
    "/apply <merge id>: make a merged transcript the current version\n"
    "/cookies: one-time setup for TikTok/Facebook link downloads\n"
    "/help: this message\n\n"
    "*RecSterio records* (each record keeps its ID, platform, author and date)\n"
    "/collect: guided wizard. Paste the link, name the place, save the record\n"
    "/record <video id>: show one item's RecSterio record\n"
    "/records: the most recent records\n\n"
    "*Dataset* (admins only, because the bot's token can see private folders)\n"
    "/folders [parent id]: the folder tree, with visibility and video counts\n"
    "/folder <folder id>: one folder, its sub-folders and its role\n"
    "/cards [folder id] [search]: dataset cards, raw and cleaned\n"
    "/cleaned [folder id]: only the hand-corrected units\n"
    "/mkdir <name> [--public] [--parent <id>]: create a folder\n"
    "/share <folder id> <role> [days]: issue a share link\n"
    "/passcode <folder id> [label] [--uses N] [--devices N] [--one-time] [--days N]:\n"
    "            issue a door code (shown once; the server stores only a hash)\n"
    "/zip <folder id>: export the folder and its sub-folders as one archive\n\n"
    "Just paste a link and wait. Only public content you have the rights to."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message; also handles the `/start <video_id>` deep link."""
    message = update.effective_message
    if message is None:
        return

    args = [a for a in (context.args or []) if a]
    if args and args[0].startswith("link-"):
        # `/start link-<code>` — the console Settings page's deep link, which
        # links this Telegram to a console account (identity.py).
        await identity.handle_start_link(update, context, args[0][5:])
        return
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
    except Exception:  # noqa: BLE001 - defensive
        client = None
    if client is None:
        await message.reply_text(
            f"Video `{video_id}`: the bot is still starting up, so try again in a moment.", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        video = await client.get_video(video_id, requested_by=actor_for(update))
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
    """`/help` — the full command reference (`/start` is the short version)."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        HELP,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=help_keyboard(),
    )
