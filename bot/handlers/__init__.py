"""Handler registration for the recsterio Telegram bot."""

from __future__ import annotations

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import admin, errors, links, media, merge, start, status, transcript

__all__ = ["errors", "register_handlers"]


def register_handlers(app: Application) -> None:
    """Register every command and message handler on ``app``."""
    app.add_handler(CommandHandler("start", start.start))
    app.add_handler(CommandHandler("help", start.help_command))
    app.add_handler(CommandHandler("status", status.status_command))
    app.add_handler(CommandHandler("transcript", transcript.transcript_command))
    app.add_handler(CommandHandler("transcripts", transcript.transcripts_command))
    app.add_handler(CommandHandler("media", media.media_command))
    app.add_handler(CommandHandler("merge", merge.merge_command))
    app.add_handler(CommandHandler("merges", merge.merges_command))
    app.add_handler(CommandHandler("apply", merge.apply_command))
    app.add_handler(CommandHandler("jobs", admin.jobs_command))
    app.add_handler(CommandHandler("retry", admin.retry_command))
    app.add_handler(CommandHandler("jobretry", admin.jobretry_command))

    # Inline buttons (merge apply / re-check / list).
    app.add_handler(CallbackQueryHandler(merge.on_callback))

    # Plain text messages that may contain a video link.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, links.handle_link))

    # Global safety net.
    app.add_error_handler(errors.error_handler)
