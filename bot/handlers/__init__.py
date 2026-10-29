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
    collect,
    cookies,
    dataset,
    errors,
    identity,
    links,
    media,
    merge,
    start,
    status,
    transcript,
    uploads,
)

__all__ = ["errors", "register_handlers"]


async def _text_router(update, context) -> None:
    """Route a plain message to a pending transcript edit, else treat it as a link.

    One handler for both because a plain message means "the thing I was asked
    for" first, and "a link to download" only when nothing is pending.
    """
    consumed = await transcript.handle_pending_edit(update, context)
    if consumed:
        return
    await links.handle_link(update, context)


def register_handlers(app: Application) -> None:
    """Register every command and message handler on ``app``."""
    app.add_handler(CommandHandler("start", start.start))
    app.add_handler(CommandHandler("help", start.help_command))
    app.add_handler(CommandHandler("status", status.status_command))
    app.add_handler(CommandHandler("whoami", identity.whoami_command))
    app.add_handler(CommandHandler("transcript", transcript.transcript_command))
    app.add_handler(CommandHandler("transcripts", transcript.transcripts_command))
    # Editing from the chat (instruction.txt #3).
    app.add_handler(CommandHandler("edit", transcript.edit_command))
    app.add_handler(CommandHandler("cancel", transcript.cancel_command))
    app.add_handler(CommandHandler("media", media.media_command))
    # The stored mp3 — request-only, never auto-delivered.
    app.add_handler(CommandHandler("audio", media.audio_command))
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

    # TvNa collection schema (instruction.txt #6).
    app.add_handler(CommandHandler("record", collect.record_command))
    app.add_handler(CommandHandler("records", collect.records_command))

    # Per-user cookies: one-time setup so TikTok/Facebook link downloads can
    # use the user's own session — consent first, file second.
    app.add_handler(CommandHandler("cookies", cookies.cookies_command))

    # Inline buttons (merge apply / re-check / list). The cookies and link
    # handlers are pattern-scoped and registered first because merge's handler
    # matches every callback — only the first matching handler in a group runs.
    app.add_handler(CallbackQueryHandler(cookies.on_callback, pattern=r"^cookies:"))
    app.add_handler(CallbackQueryHandler(identity.on_callback, pattern=r"^link:"))
    app.add_handler(CallbackQueryHandler(merge.on_callback))

    # The /collect wizard. Registered before the catch-all text handler so a
    # message sent inside the wizard is consumed by it, not queued as a new
    # download.
    app.add_handler(collect.build_conversation())

    # A plain message may be the body of a pending transcript edit, so that
    # check runs before the link handler and stops it when it consumed the text.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _text_router)
    )

    # A cookies.txt in a private chat (the /cookies flow's second step).
    # Registered BEFORE the media catch-all: a group runs only the first
    # matching handler, and a .txt is not in uploads' ALLOWED_SUFFIXES, so
    # this must win the race for it.
    app.add_handler(
        MessageHandler(
            filters.Document.FileExtension("txt") & filters.ChatType.PRIVATE,
            cookies.handle_cookies_document,
        )
    )

    # A media file the user sends directly — ingestion without a platform.
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.AUDIO | filters.Document.ALL,
            uploads.handle_media_upload,
        )
    )

    # Global safety net.
    app.add_error_handler(errors.error_handler)
