"""Handler registration for the recsterio Telegram bot."""

from __future__ import annotations

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import (
    admin,
    dataset,
    errors,
    links,
    media,
    merge,
    start,
    status,
    transcript,
    uploads,
)

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

    # Dataset surface (v2). Admin-gated, like the job controls: the bot's
    # service token can see every folder, private ones included.
    app.add_handler(CommandHandler("folders", dataset.folders_command))
    app.add_handler(CommandHandler("folder", dataset.folder_command))
    app.add_handler(CommandHandler("cards", dataset.cards_command))
    app.add_handler(CommandHandler("cleaned", dataset.cleaned_command))
    app.add_handler(CommandHandler("mkdir", dataset.mkdir_command))
    app.add_handler(CommandHandler("share", dataset.share_command))
    app.add_handler(CommandHandler("passcode", dataset.passcode_command))
    app.add_handler(CommandHandler("zip", dataset.zip_command))

    # Inline buttons (merge apply / re-check / list).
    app.add_handler(CallbackQueryHandler(merge.on_callback))

    # Plain text messages that may contain a video link.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, links.handle_link))

    # A media file the user sends directly — ingestion without a platform.
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.AUDIO | filters.Document.ALL,
            uploads.handle_media_upload,
        )
    )

    # Global safety net.
    app.add_error_handler(errors.error_handler)
