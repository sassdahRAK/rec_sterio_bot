"""Global error handler — the bot must never die because of a bad request."""

from __future__ import annotations

import logging

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError

logger = logging.getLogger(__name__)

__all__ = ["error_handler"]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the exception and tell the user something went wrong."""
    error = context.error
    logger.error("unhandled error while processing update: %s", error, exc_info=error)

    text = "Something went wrong on our side — please try again."
    parse_mode = None
    if isinstance(error, (ApiError, ApiNetworkError)):
        # ``user_message`` is already Markdown-escaped; render it as such.
        text = error.user_message()
        parse_mode = ParseMode.MARKDOWN
    elif error is not None:
        # An arbitrary exception's text is never handed to the entity parser.
        text = "Something went wrong on our side — please try again in a moment."

    try:
        message = getattr(update, "effective_message", None)
        if message is not None:
            await message.reply_text(text, parse_mode=parse_mode)
            return
        chat = getattr(update, "effective_chat", None)
        if chat is not None:
            await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=parse_mode)
    except Exception:  # noqa: BLE001 - the error handler must not raise
        logger.warning("could not deliver the error message to the user")
