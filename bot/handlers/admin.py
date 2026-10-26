"""Admin-only commands: `/jobs`, `/retry <id>`, `/jobretry <id>`.

The admin check happens *before* any request is sent to the backend, so a
non-admin never causes a backend call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import escape_markdown, get_client, is_admin, is_uuid
from ..keyboards import jobs_keyboard

logger = logging.getLogger(__name__)

__all__ = ["jobretry_command", "jobs_command", "retry_command"]

NOT_AUTHORISED = "⛔ You are not authorised to use this command."


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent jobs (and failures) — admins only."""
    message = update.effective_message
    if message is None:
        return
    if not is_admin(update, context):
        await message.reply_text(NOT_AUTHORISED)
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return

    status_filter = None
    args = [a for a in (context.args or []) if a]
    if args and args[0].lower() in {"failed", "dead", "queued", "running", "succeeded"}:
        status_filter = args[0].lower()

    try:
        payload = await client.list_jobs(page_size=20, status=status_filter)
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    items = _items(payload)
    total = payload.get("total") if isinstance(payload, Mapping) else None
    if not items:
        await message.reply_text(
            "No jobs found." + (f" (total: {total})" if total is not None else ""),
            reply_markup=jobs_keyboard(),
        )
        return

    lines = ["*Recent jobs*" + (f" (total {total})" if total is not None else "")]
    for job in items[:20]:
        if not isinstance(job, Mapping):
            continue
        lines.append(
            "- `{id}` · {kind} · {status} · attempts {attempts}/{max_attempts}"
            " · video {video}".format(
                id=job.get("id", "?"),
                kind=escape_markdown(job.get("kind", "?")),
                status=escape_markdown(job.get("status", "?")),
                attempts=job.get("attempts", "?"),
                max_attempts=job.get("max_attempts", "?"),
                video=job.get("video_id") or "—",
            )
        )
        if job.get("last_error"):
            lines.append(f"  ↳ error: {escape_markdown(job['last_error'])}")
    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=jobs_keyboard(),
    )


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-enqueue the download job for a video — admins only."""
    message = update.effective_message
    if message is None:
        return
    if not is_admin(update, context):
        await message.reply_text(NOT_AUTHORISED)
        return

    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text("Usage: /retry <video id>")
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(
            f"`{video_id}` is not a valid video id.", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return

    try:
        job = await client.retry_video(video_id)
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    await message.reply_text(
        "♻️ Re-queued video `{vid}`.\nJob `{job}` · status `{status}`.".format(
            vid=video_id,
            job=job.get("id", "?") if isinstance(job, Mapping) else "?",
            status=job.get("status", "?") if isinstance(job, Mapping) else "?",
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def jobretry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset a failed/dead job to `queued` — admins only."""
    message = update.effective_message
    if message is None:
        return
    if not is_admin(update, context):
        await message.reply_text(NOT_AUTHORISED)
        return

    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text("Usage: /jobretry <job id>")
        return
    job_id = args[0].strip()
    if not is_uuid(job_id):
        await message.reply_text(
            f"`{job_id}` is not a valid job id.", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return

    try:
        job = await client.retry_job(job_id)
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    await message.reply_text(
        "♻️ Job `{job}` re-queued · status `{status}`.".format(
            job=job_id,
            status=job.get("status", "?") if isinstance(job, Mapping) else "?",
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []
