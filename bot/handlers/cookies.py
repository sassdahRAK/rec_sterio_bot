"""`/cookies` — per-user cookies + consent gate for TikTok/Facebook downloads.

TikTok and Facebook refuse most server-side downloads, so a link alone is
often not enough: the fetch has to ride on a logged-in browser session. The
session the bot may use is the user's own — and only with their explicit
agreement. Both the consent record and the cookies.txt live behind the
backend's ``/bot-users/{telegram_id}`` endpoints, one record per user.
"""
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
    format_bytes,
    get_client,
    get_settings_or_default,
)
from ..keyboards import cookies_keyboard

logger = logging.getLogger(__name__)

__all__ = [
    "CONSENT_REQUIRED_HINT",
    "CONSENT_TEXT",
    "CONSENT_VERSION",
    "cookies_command",
    "handle_cookies_document",
    "on_callback",
]

#: Bumped whenever the consent text changes meaning. The backend records the
#: version agreed to and rejects an "I agree" against a stale text with a
#: ``version_mismatch`` error.
CONSENT_VERSION = "2026-10-28.1"

#: Plain text on purpose — the bot has no global parse_mode, and a consent
#: text must never be mangled by Markdown entity parsing.
CONSENT_TEXT = (
    "COOKIES CONSENT\n"
    "\n"
    "Your cookies file is used ONLY for your own downloads, never for anyone else's.\n"
    "\n"
    "By tapping \"I agree\" you confirm that:\n"
    "1. The browser account the cookies come from is yours.\n"
    "2. You allow its session to be used for the downloads you request.\n"
    "3. You take responsibility for what you download, including complying "
    "with TikTok's and Facebook's terms of service.\n"
    "Using a dedicated collection account for this is recommended.\n"
    "\n"
    "The file passes through Telegram, is stored on the team server with 600 "
    "(owner-only) permissions, is never committed to any repository and is "
    "never shown to anyone.\n"
    "\n"
    "Withdraw any time via /cookies → \"Remove my data\". The file and your "
    "consent record are deleted immediately."
)

#: How a consent_required error is explained wherever a download is requested
#: (a plain link message, or the /collect wizard's URL step).
CONSENT_REQUIRED_HINT = (
    "This platform needs your own cookies first. Run /cookies in a private "
    "chat with me, tap I agree, then send your cookies.txt, and this link "
    "will download."
)

_NOT_PRIVATE = "Cookies are personal, so message me privately to set them up."


def _status_line(record: Mapping[str, Any] | None) -> str:
    """One short line: where the user stands, consent- and cookies-wise."""
    if record is None:
        return "Status: you have not agreed yet, and no cookies file is on record."
    consent = "consent recorded" if record.get("consented") else "no consent yet"
    if record.get("cookies_present"):
        when = record.get("cookies_updated_at") or "unknown"
        cookies = f"cookies on file (updated {when})"
    else:
        cookies = "no cookies file on record"
    return f"Status: {consent}; {cookies}."


def _chat_type(update: Update) -> str:
    chat = getattr(update, "effective_chat", None)
    return str(getattr(chat, "type", "private") or "private")


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/cookies` — show the user's status plus the consent text, with buttons."""
    message = update.effective_message
    user = update.effective_user
    if message is None:
        return
    if user is None:
        await message.reply_text(
            "[unknown_user] I could not tell who is asking, so please try again."
        )
        return

    # Cookies are personal: showing consent state in a group, let alone
    # accepting a cookies file there, leaks one member's session to the rest.
    if _chat_type(update) != "private":
        await message.reply_text(_NOT_PRIVATE)
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    try:
        record = await client.get_bot_user(user.id, requested_by=actor_for(update))
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    # Plain text, no parse_mode: CONSENT_TEXT carries no Markdown (see above).
    await message.reply_text(
        _status_line(record) + "\n\n" + CONSENT_TEXT,
        reply_markup=cookies_keyboard(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The two /cookies buttons: agree to the consent text, remove the data."""
    query = update.callback_query
    if query is None:
        return
    data = str(query.data or "")
    # "cookies:agree" / "cookies:remove" — the same partition convention as
    # the merge buttons, only here the verb is the part after the prefix.
    prefix, _, action = data.partition(":")
    if prefix != "cookies":  # defensive: the handler is pattern-scoped anyway
        return

    # Answer first so the button never spins, whatever happens next.
    await query.answer()

    user = getattr(query, "from_user", None)
    uid = getattr(user, "id", None)
    if uid is None:  # pragma: no cover - PTB always sets from_user
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await query.answer(text=BOT_NOT_READY, show_alert=True)
        return

    if action == "agree":
        try:
            await client.create_consent(
                int(uid), CONSENT_VERSION, requested_by=actor_for(update)
            )
        except ApiError as exc:
            if exc.code == "version_mismatch":
                await query.edit_message_text(
                    "The consent text has changed. Run /cookies again."
                )
            else:
                await query.edit_message_text(
                    exc.user_message(), parse_mode=ParseMode.MARKDOWN
                )
            return
        except ApiNetworkError as exc:
            await query.edit_message_text(
                exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
            return
        await query.edit_message_text(
            "Recorded. Now send your cookies.txt file as a document."
        )
        return

    if action == "remove":
        try:
            await client.delete_bot_user(int(uid), requested_by=actor_for(update))
        except ApiError as exc:
            await query.edit_message_text(
                exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
            return
        except ApiNetworkError as exc:
            await query.edit_message_text(
                exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
            return
        await query.edit_message_text(
            "Your cookies and consent record were deleted."
        )
        return

    # Unknown cookies action — already answered above, nothing left to do.


async def handle_cookies_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """A cookies.txt sent as a document — store it behind the consent record."""
    message = update.effective_message
    if message is None:
        return

    # The registration filter already restricts this to private chats; the
    # guard keeps a group message from being stored if that ever changes.
    if _chat_type(update) != "private":
        logger.warning("a cookies document arrived outside a private chat — ignored")
        return

    document = getattr(message, "document", None)
    if document is None:
        return

    user = update.effective_user
    uid = getattr(user, "id", None)
    if uid is None:
        await message.reply_text(
            "[unknown_user] I could not tell who sent this, so please try again."
        )
        return

    filename = getattr(document, "file_name", None) or "cookies.txt"

    settings = (context.bot_data or {}).get("settings") or get_settings_or_default()
    max_bytes = settings.cookies_max_bytes

    declared = getattr(document, "file_size", None)
    if isinstance(declared, int) and declared > max_bytes:
        await message.reply_text(
            f"That file is too big to be a cookies file "
            f"(limit {format_bytes(max_bytes)})."
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    notice = await message.reply_text("Receiving your cookies file…")
    try:
        handle = await document.get_file()
        payload = await handle.download_as_bytearray()
    except Exception as exc:  # noqa: BLE001 - a failed download must not crash
        logger.warning("could not fetch the Telegram file: %s", exc)
        await notice.edit_text("I could not download that file from Telegram. Try again.")
        return

    try:
        await client.upload_cookies(
            int(uid), bytes(payload), filename, requested_by=actor_for(update)
        )
    except ApiError as exc:
        await notice.edit_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await notice.edit_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    await notice.edit_text(
        "Cookies stored. Your TikTok/Facebook downloads will now use your own session."
    )