"""`/collect` — the TvNa collection wizard (instruction.txt #6).

The TvNa platform requires a fixed set of fields for every collected item.
Most of them the backend already knows after a download (platform, author,
post date, caption); only the place and the collector's judgement have to come
from a human. So the wizard asks for the minimum and shows the rest back as
auto-filled, rather than making someone type nine fields per clip.

Flow:  /collect → paste link → location tag → method (button) → notes → saved
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..client import ApiError, ApiNetworkError
from ..helpers import actor_for, escape_markdown, extract_url, get_client

logger = logging.getLogger(__name__)

__all__ = ["build_conversation", "record_command", "records_command"]

(
    ASK_URL,
    ASK_LOCATION,
    ASK_METHOD,
    ASK_NOTES,
) = range(4)

USAGE = (
    "Usage: `/collect` then paste the link.\n"
    "`/record <video id>` — show one item's TvNa record\n"
    "`/records` — the most recent records"
)

# A text choice rather than inline buttons: a CallbackQueryHandler inside a
# `per_message=False` conversation has documented state-tracking caveats, and
# this step is one keystroke either way.
METHOD_PROMPT = (
    "How was this collected?\n"
    "1 — Manual browse\n"
    "2 — Research API"
)

_METHOD_WORDS = {
    "1": "Manual browse",
    "manual": "Manual browse",
    "manual browse": "Manual browse",
    "browse": "Manual browse",
    "2": "Research API",
    "api": "Research API",
    "research": "Research API",
    "research api": "Research API",
}


def parse_method(text: str) -> str | None:
    """Map a reply onto one of the two collection methods, or None."""
    return _METHOD_WORDS.get(text.strip().lower())

SKIP_HINT = "Send `-` or /skip to leave it empty."


# ------------------------------------------------------------------ helpers


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """The in-progress record, per Telegram user."""
    data = context.user_data
    if data is None:  # pragma: no cover - user_data is always present in PTB
        return {}
    return data.setdefault("tvna_draft", {})


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop("tvna_draft", None)


def _format_record(video: Mapping[str, Any]) -> str:
    """Render the TvNa block exactly as the platform's schema lists it."""
    def field(name: str, value: Any) -> str:
        text = str(value).strip() if value not in (None, "") else "—"
        return f"{name}: {escape_markdown(text)}"

    lines = [
        f"*{video.get('tvna_id') or 'unrecorded'}*",
        field("ID", video.get("tvna_id")),
        field("Platform", video.get("platform")),
        field("Source Type", video.get("source_type")),
        field("Author/Handle", video.get("author_handle")),
        field("Date Posted", video.get("date_posted")),
        field("Caption", video.get("caption")),
        field("URL", video.get("source_url")),
        field("Location Tag", video.get("location_tag")),
        field("Collection Method", video.get("collection_method")),
        field("Collected By", video.get("collected_by")),
        field("Collection Date", video.get("collection_date")),
        field("Notes", video.get("notes")),
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------- wizard


async def start_collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: reset any draft and ask for the link."""
    message = update.effective_message
    if message is None:
        return ConversationHandler.END
    _clear(context)
    _state(context)  # create a fresh draft
    await message.reply_text(
        "New TvNa record.\n\n"
        "Paste the TikTok, Facebook or YouTube link for this item. "
        "/cancel stops here.",
    )
    return ASK_URL


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = (message.text if message else "") or ""
    if text.strip().lower() in {"/cancel", "cancel"}:
        return await cancel(update, context)

    url = extract_url(text)
    if not url:
        await message.reply_text(
            "That does not look like a link. Paste the full URL, starting with https://"
        )
        return ASK_URL

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return ConversationHandler.END

    try:
        video = await client.create_video(url, requested_by=actor_for(update))
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return ASK_URL
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    draft = _state(context)
    draft["video_id"] = str(video.get("id"))
    draft["video"] = video

    await message.reply_text(
        f"Queued `{video.get('id')}` ({video.get('platform', 'unknown')}).\n\n"
        "The download and transcription run in the background — the record "
        "saves now, and the metadata fills in when the fetch finishes.\n\n"
        "*Location tag?*\n"
        "The place name exactly as stated in the content, e.g. "
        "`Chew & Bash, Phnom Penh`.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_LOCATION


async def receive_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = ((message.text if message else "") or "").strip()
    if text.lower() in {"/cancel", "cancel"}:
        return await cancel(update, context)
    if not text:
        await message.reply_text("A location tag is required — type the place name.")
        return ASK_LOCATION

    draft = _state(context)
    draft["location_tag"] = text

    await message.reply_text(METHOD_PROMPT)
    return ASK_METHOD


async def receive_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Read the collection method as a reply."""
    message = update.effective_message
    text = ((message.text if message else "") or "").strip()
    if text.lower() in {"/cancel", "cancel"}:
        return await cancel(update, context)

    method = parse_method(text)
    if method is None:
        await message.reply_text(f"Reply 1 or 2.\n\n{METHOD_PROMPT}")
        return ASK_METHOD

    _state(context)["collection_method"] = method
    await message.reply_text(
        f"Collection method: {method}\n\n"
        "*Notes?*\n"
        "Relevance assessment, access issues, content warnings — or `-` to skip.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_NOTES


async def receive_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = ((message.text if message else "") or "").strip()
    if text.lower() in {"/cancel", "cancel"}:
        return await cancel(update, context)

    notes = None if text in {"-", "/skip", ""} else text
    return await _save(update, context, notes)


async def _save(
    update: Update, context: ContextTypes.DEFAULT_TYPE, notes: str | None
) -> int:
    message = update.effective_message
    draft = _state(context)
    video_id = draft.get("video_id")
    if not video_id:
        await message.reply_text("Something went wrong — the draft was lost. Start again with /collect.")
        _clear(context)
        return ConversationHandler.END

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        _clear(context)
        return ConversationHandler.END

    try:
        saved = await client.record_tvna(
            str(video_id),
            location_tag=draft.get("location_tag", ""),
            collection_method=draft.get("collection_method", "Manual browse"),
            collected_by=_collector_name(update),
            notes=notes,
            requested_by=actor_for(update),
        )
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        _clear(context)
        return ConversationHandler.END
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        _clear(context)
        return ConversationHandler.END

    _clear(context)
    await message.reply_text(
        "Recorded.\n\n" + _format_record(saved),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


def _collector_name(update: Update) -> str | None:
    """The Telegram display name, which is what a collector would type anyway."""
    user = getattr(update, "effective_user", None)
    if user is None:
        return None
    name = " ".join(
        part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part
    ).strip()
    return name or getattr(user, "username", None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    message = update.effective_message
    if message is not None:
        await message.reply_text("Cancelled. Nothing was recorded.")
    return ConversationHandler.END


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """`/skip` at the notes step, without losing the rest of the draft."""
    return await receive_notes(update, context)


def build_conversation() -> ConversationHandler:
    """The wizard as a ConversationHandler.

    Registered before the catch-all link handler, so a message inside the
    wizard is consumed here instead of being treated as a new download.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("collect", start_collect)],
        states={
            ASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)],
            ASK_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_location)],
            ASK_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_method)],
            ASK_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes),
                CommandHandler("skip", skip_command),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # Per-user state: two collectors can run the wizard at the same time.
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )


# ------------------------------------------------------------------ viewing


async def record_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/record <video id>` — show one item's TvNa record."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(USAGE, parse_mode=ParseMode.MARKDOWN)
        return

    video_id = args[0].strip()
    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
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
        _format_record(video),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def records_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/records` — the most recently recorded TvNa items."""
    message = update.effective_message
    if message is None:
        return
    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return

    try:
        payload = await client.list_videos({"page": 1, "page_size": 20, "sort": "-created_at"})
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    items = (payload or {}).get("items") or []
    recorded = [v for v in items if isinstance(v, Mapping) and v.get("tvna_id")]
    if not recorded:
        await message.reply_text(
            "No TvNa records yet. Start one with /collect.",
        )
        return

    lines = ["*Recent TvNa records*"]
    for video in recorded:
        lines.append(
            f"- `{video.get('tvna_id')}` · {escape_markdown(str(video.get('location_tag') or '—'))}"
            f" · {video.get('platform', '?')}"
        )
    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )
