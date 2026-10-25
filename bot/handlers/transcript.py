"""`/transcript` and `/transcripts` handlers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import escape_markdown, get_client, is_uuid
from ..keyboards import transcript_versions_keyboard

logger = logging.getLogger(__name__)

__all__ = ["transcript_command", "transcripts_command"]

USAGE = "Usage: /transcript <video_id>  (the id shown when you queued the link)"


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
        await message.reply_text(f"`{video_id}` is not a valid video id.\n{USAGE}")
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[not_ready] {exc}")
        return

    try:
        transcript = await client.get_transcript(video_id)
    except ApiError as exc:
        if exc.status_code == 404:
            await message.reply_text(
                f"No transcript yet for video `{video_id}`.\n"
                "Transcription runs in the background — try again in a minute, or use "
                f"/status {video_id}."
            )
            return
        await message.reply_text(exc.user_message())
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message())
        return

    if not transcript:
        await message.reply_text(f"No transcript yet for video `{video_id}`.")
        return

    content = str(transcript.get("content") or "")
    version = transcript.get("version", "?")
    source = transcript.get("source", "?")
    status = transcript.get("status", "?")

    if status != "ready":
        await message.reply_text(
            f"Transcript v{version} (source={source}) is `{status}` — not ready yet."
        )
        return

    header = f"Transcript v{version} (source={source}) — {len(content)} chars"
    if len(content) <= _limit(context):
        await message.reply_text(f"{header}\n\n{escape_markdown(content)}")
        return

    filename = f"transcript_{video_id}_v{version}.txt"
    await message.reply_document(
        document=content.encode("utf-8"),
        filename=filename,
        caption=(
            f"{header}\nSent as a file because it is longer than "
            f"{_limit(context)} characters."
        ),
    )


async def transcripts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List every transcript version for a video, newest first."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text("Usage: /transcripts <video_id>")
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(f"`{video_id}` is not a valid video id.")
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[not_ready] {exc}")
        return

    try:
        payload = await client.list_transcripts(video_id)
    except ApiError as exc:
        if exc.status_code == 404:
            await message.reply_text(f"No transcripts for video `{video_id}`.")
            return
        await message.reply_text(exc.user_message())
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message())
        return

    items = _items(payload)
    if not items:
        await message.reply_text(f"No transcripts for video `{video_id}`.")
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
