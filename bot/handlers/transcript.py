"""`/transcript` and `/transcripts` handlers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import (
    BOT_NOT_READY,
    actor_for,
    escape_markdown,
    get_client,
    is_uuid,
    portal_token_for,
    web_links,
)
from ..keyboards import transcript_versions_keyboard

logger = logging.getLogger(__name__)

__all__ = [
    "PENDING_EDIT_KEY",
    "cancel_command",
    "handle_pending_edit",
    "transcript_command",
    "transcripts_command",
    "edit_command",
]

USAGE = "Usage: /transcript <video id>  (the id shown when you queued the link)"

# Where a half-finished edit lives. `context.user_data` is per-Telegram-user,
# so two people editing two different videos never see each other's draft.
PENDING_EDIT_KEY = "pending_edit_video_id"

EDIT_USAGE = (
    "Usage:\n"
    "`/edit <video id>`: send the corrected text in your next message\n"
    "`/edit <video id> <new text>`: save in one go\n"
    "`/cancel`: abandon a draft"
)


def _limit(context: ContextTypes.DEFAULT_TYPE) -> int:
    settings = (context.bot_data or {}).get("settings")
    return int(getattr(settings, "transcript_text_limit", 3000))


async def transcript_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the current transcript — inline for short texts, as a .txt file otherwise."""
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
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    # The transcript and the video are both read as this Telegram user, so the
    # backend can refuse a unit they have no right to before anything is shown.
    requested_by = actor_for(update)
    try:
        transcript = await client.get_transcript(video_id, requested_by=requested_by)
    except ApiError as exc:
        if exc.status_code == 404:
            await message.reply_text(
                f"No transcript yet for video `{video_id}`.\n"
                "Transcription runs in the background, so try again in a minute, or use "
                f"/status {video_id}.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    if not transcript:
        await message.reply_text(
            f"No transcript yet for video `{video_id}`.", parse_mode=ParseMode.MARKDOWN
        )
        return

    content = str(transcript.get("content") or "")
    version = transcript.get("version", "?")
    source = transcript.get("source", "?")
    status = transcript.get("status", "?")

    # The link buttons need the video's folder, which the transcript payload
    # does not carry — fetch the video tolerantly, once, for every reply below.
    # The same identity is reused for the token mint, so the backend attributes
    # the access check to this Telegram user rather than to the bot.
    try:
        video = await client.get_video(video_id, requested_by=requested_by)
    except (ApiError, ApiNetworkError):
        video = {}
    portal = await portal_token_for(
        client, video_id, requested_by=requested_by
    )
    links = web_links(
        update,
        context,
        video=video,
        video_id=video_id,
        token=portal.token,
        allow_portal=portal.linkable(confirmed=bool(video)),
    )

    if status != "ready":
        await message.reply_text(
            f"Transcript v{version} (source={escape_markdown(source)}) is `{status}`, so it is not ready yet.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=links,
        )
        return

    header = f"Transcript v{version} (source={source}): {len(content)} chars"
    if len(content) <= _limit(context):
        await message.reply_text(
            f"{header}\n\n{escape_markdown(content)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=links,
        )
        return

    filename = f"transcript_{video_id}_v{version}.txt"
    await message.reply_document(
        document=content.encode("utf-8"),
        filename=filename,
        caption=(
            f"{header}\nSent as a file because it is longer than "
            f"{_limit(context)} characters."
        ),
        reply_markup=links,
    )


async def transcripts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List every transcript version for a video, newest first."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text("Usage: /transcripts <video id>")
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(
            f"`{video_id}` is not a valid video id.", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    try:
        payload = await client.list_transcripts(
            video_id, requested_by=actor_for(update)
        )
    except ApiError as exc:
        if exc.status_code == 404:
            await message.reply_text(
                f"No transcripts for video `{video_id}`.", parse_mode=ParseMode.MARKDOWN
            )
            return
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    items = _items(payload)
    if not items:
        await message.reply_text(
                f"No transcripts for video `{video_id}`.", parse_mode=ParseMode.MARKDOWN
            )
        return

    lines = [f"*Transcript versions* for `{video_id}`"]
    for item in items:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            "- v{version} · {source} · {status} · {created} · {chars} chars".format(
                version=item.get("version", "?"),
                source=item.get("source", "?"),
                status=item.get("status", "?"),
                created=item.get("created_at", "?"),
                chars=len(str(item.get("content") or "")),
            )
        )
    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=transcript_versions_keyboard(video_id, items),
    )


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []


# ------------------------------------------------------------------ editing


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Correct a transcript from the chat (instruction.txt #3).

    Two shapes: ``/edit <id> <text>`` saves immediately, and ``/edit <id>``
    opens a draft — the next plain message from that user is the new text.
    Either way the backend stores a NEW version, so the ASR output survives.
    """
    message = update.effective_message
    if message is None:
        return
    args = list(context.args or [])
    if not args:
        await message.reply_text(EDIT_USAGE, parse_mode=ParseMode.MARKDOWN)
        return

    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(
            f"`{video_id}` is not a valid video id.\n{EDIT_USAGE}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    inline_text = " ".join(a for a in args[1:] if a).strip()
    if inline_text:
        await _save_edit(message, context, video_id, inline_text)
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    try:
        transcript = await client.get_transcript(
            video_id, requested_by=actor_for(update)
        )
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    current = str((transcript or {}).get("content") or "")
    version = (transcript or {}).get("version", "?")
    if not current.strip():
        await message.reply_text(
            f"Video `{video_id}` has no transcript text to edit yet. "
            f"Check `/status {video_id}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Stash the draft target so the next message from this user is the edit.
    user_data = context.user_data
    if user_data is not None:
        user_data[PENDING_EDIT_KEY] = video_id

    preview = current if len(current) <= 1500 else current[:1500] + "\n…(truncated)"
    await message.reply_text(
        f"*Editing v{version} of* `{video_id}`\n\n"
        f"{escape_markdown(preview)}\n\n"
        "Send the corrected transcript as your next message. "
        "`/cancel` abandons the draft.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_pending_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume a plain message as the body of a pending edit.

    Returns True when the message was an edit, so the caller can stop — the
    same message must not also be treated as a link to download.
    """
    user_data = context.user_data
    if not user_data:
        return False
    video_id = user_data.get(PENDING_EDIT_KEY)
    if not video_id:
        return False
    message = update.effective_message
    if message is None or not message.text:
        return False

    text = message.text
    if text.startswith("/"):
        # A command is not the edit body — leave the draft open so the user can
        # still use /cancel or /status without losing what they were doing.
        return False

    del user_data[PENDING_EDIT_KEY]
    await _save_edit(message, context, str(video_id), text)
    return True


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abandon a pending transcript edit."""
    message = update.effective_message
    user_data = context.user_data
    had = bool(user_data and user_data.pop(PENDING_EDIT_KEY, None))
    if message is None:
        return
    await message.reply_text("Edit cancelled." if had else "Nothing to cancel.")


async def _save_edit(
    message: Any, context: ContextTypes.DEFAULT_TYPE, video_id: str, text: str
) -> None:
    """Save `text` as a new transcript version and report the new version."""
    if not text.strip():
        await message.reply_text("The corrected transcript is empty, so nothing was saved.")
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    try:
        saved = await client.save_transcript(
            video_id,
            text,
            requested_by=actor_for_from_message(message),
        )
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    version = (saved or {}).get("version", "?")
    await message.reply_text(
        f"Saved as *v{version}* for `{video_id}`. The previous version is still "
        "available via `/transcripts`.",
        parse_mode=ParseMode.MARKDOWN,
    )


def actor_for_from_message(message: Any) -> str | None:
    """`telegram:<id>` for the user who sent `message`."""
    user = getattr(message, "from_user", None)
    uid = getattr(user, "id", None)
    return f"telegram:{uid}" if uid is not None else None
