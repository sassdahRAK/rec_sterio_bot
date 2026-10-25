"""Global error handler — the bot must never die because of a bad request."""

from __future__ import annotations

import logging

from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError, escape_markdown

logger = logging.getLogger(__name__)

__all__ = ["error_handler"]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the exception and tell the user something went wrong."""
    error = context.error
    logger.error("unhandled error while processing update: %s", error, exc_info=error)

    text = "Something went wrong on my side — please try again."
    if isinstance(error, (ApiError, ApiNetworkError)):
        text = error.user_message()
    elif error is not None:
        text = f"`unexpected_error` {escape_markdown(error.__class__.__name__)}: {escape_markdown(error)}"

    try:
        message = getattr(update, "effective_message", None)
        if message is not None:
            await message.reply_text(text)
            return
        chat = getattr(update, "effective_chat", None)
        if chat is not None:
            await context.bot.send_message(chat_id=chat.id, text=text)
    except Exception:  # noqa: BLE001 - the error handler must not raise
        logger.warning("could not deliver the error message to the user")
