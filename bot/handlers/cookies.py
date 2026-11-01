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
    "consent_required_hint",
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

#: How a consent_required error is explained when the backend sends no
#: per-user detail (older deployments). Callers that have the error in hand
#: should use :func:`consent_required_hint` instead.
CONSENT_REQUIRED_HINT = (
    "This platform needs your own cookies first. Run /cookies in a private "
    "chat with me, tap I agree, then send your cookies.txt, and this link "
    "will download."
)

#: Details-aware variants of the hint. The backend may attach
#: ``{"consented": bool, "cookies_present": bool}`` to a consent_required
#: error so the bot can name the exact step the user is missing.
_HINT_NOT_CONSENTED = (
    "This platform needs your own cookies first. Run /cookies in a private "
    "chat with me and tap I agree."
)
_HINT_NO_FILE = (
    "You agreed to the cookies terms, but no cookies.txt file is on record "
    "yet. Consent alone is not enough: send the cookies.txt file as a "
    "document, then paste the link again."
)
_HINT_NOT_ABOUT_COOKIES = (
    "The server refused this download. Please try again in a moment."
)

_NOT_PRIVATE = "Cookies are personal, so message me privately to set them up."
_NOT_A_COOKIES_FILE = (
    "That is not a cookies.txt file. Send the file named cookies.txt, "
    "exported in Netscape format."
)
_GROUP_DOCUMENT = (
    "Cookies are personal. Run /cookies in a private chat with me and send "
    "the file there."
)
_AGREE_ACK = (
    "Agreement recorded. That alone is not enough: TikTok and Facebook links "
    "stay refused until your cookies.txt file is on record.\n\n"
    "Now send me the cookies.txt file as a document (paperclip, then File)."
)


def _consent_details(details: Any) -> Mapping[str, Any] | None:
    """The entry that carries ``consented`` / ``cookies_present``, if any.

    The backend serializes error details as a LIST of per-field entries
    (``[{"field": "source_url", "issue": "consent_required", ...}]``), but a
    bare mapping is accepted too. A string, None or anything else falls back.
    """
    candidates = details if isinstance(details, (list, tuple)) else [details]
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        if (
            item.get("issue") == "consent_required"
            or isinstance(item.get("consented"), bool)
            or isinstance(item.get("cookies_present"), bool)
        ):
            return item
    return None


def consent_required_hint(details: Any = None) -> str:
    """Word the consent hint from the backend's optional 403 details.

    Older backends send no details, in which case the generic hint stands.
    The details are read defensively: anything that is not a mapping (or a
    list carrying one) with two real booleans falls back to the generic text.
    """
    entry = _consent_details(details)
    if entry is not None:
        consented = entry.get("consented")
        cookies_present = entry.get("cookies_present")
        if isinstance(consented, bool) and isinstance(cookies_present, bool):
            if not consented:
                return _HINT_NOT_CONSENTED
            if not cookies_present:
                return _HINT_NO_FILE
            # Both are true: the refusal is not about cookies, so say nothing
            # about them rather than inventing a cause.
            return _HINT_NOT_ABOUT_COOKIES
    return CONSENT_REQUIRED_HINT


def _status_line(record: Mapping[str, Any] | None) -> str:
    """One block: the FILE state first, then the consent state.

    The two are separate steps, and users stop after the first one. Leading
    with the file makes the missing second step impossible to miss.
    """
    if record is not None and record.get("cookies_present"):
        when = record.get("cookies_updated_at") or "unknown"
        file_line = f"Status: cookies on file (updated {when})."
    else:
        file_line = "Status: No cookies file on record yet."
    consented = bool(record.get("consented")) if record is not None else False
    consent_line = (
        "Agreement status: consent recorded."
        if consented
        else "Agreement status: not agreed yet."
    )
    return f"{file_line}\n{consent_line}"


def _chat_type(update: Update) -> str:
    chat = getattr(update, "effective_chat", None)
    return str(getattr(chat, "type", "private") or "private")


def _document_mime(document: Any) -> str:
    return str(getattr(document, "mime_type", "") or "").lower()


def _is_media_document(filename: str, mime: str) -> bool:
    """Video or audio content, judged by mime first and extension second."""
    if mime.startswith(("video/", "audio/")):
        return True
    # Local import: uploads imports links, which imports this module.
    from . import uploads

    name = filename.strip().lower()
    return any(name.endswith(suffix) for suffix in uploads.ALLOWED_SUFFIXES)


def _is_cookie_candidate(filename: str, mime: str) -> bool:
    """A .txt name or a text mime: the only documents that can be cookies."""
    name = filename.strip().lower()
    return name.endswith(".txt") or mime.startswith("text/") or "cookie" in mime


def _awaiting_file(context: ContextTypes.DEFAULT_TYPE) -> bool:
    data = getattr(context, "user_data", None)
    return bool(isinstance(data, dict) and data.get("cookies_awaiting_file"))


def _set_awaiting_file(context: ContextTypes.DEFAULT_TYPE, awaiting: bool) -> None:
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return
    if awaiting:
        data["cookies_awaiting_file"] = True
    else:
        data.pop("cookies_awaiting_file", None)


def _upload_error_message(exc: ApiError) -> str:
    """A specific reason for a rejected upload, never the raw envelope alone."""
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "").lower()
    detail = str(getattr(exc, "message", "") or "").strip()
    if status == 413 or "too_large" in code or "too_big" in code or "oversize" in code:
        return (
            "That cookies file is too large for the server. Export a fresh "
            "cookies.txt and send it again."
        )
    if (
        status in (415, 422)
        or "format" in code
        or code in {"validation_failed", "invalid_file", "empty_file"}
        or "empty" in code
    ):
        return (
            "The server rejected that file as the wrong format. Export "
            "cookies.txt in Netscape format and send it again."
        )
    if status == 403 or code in {"forbidden", "not_private", "private_chat_required"}:
        return (
            "The server refused the upload. Cookies can only be accepted from "
            "a private chat, so run /cookies in a private chat and send the "
            "file there."
        )
    if status == 401:
        return "The server rejected the bot credentials, so the team needs to check the deployment."
    if status is not None and status >= 500:
        return "The server had an error while storing your cookies. Please try again in a moment."
    if detail:
        return f"The server refused the cookies file. Report this: {detail}"
    return "The server refused the cookies file. Please try again in a moment."


async def _cookies_present_after_upload(
    client: Any,
    uid: int,
    response: Any,
    requested_by: str,
) -> bool | None:
    """True/False from the server, or None when it could not be confirmed.

    The upload response is trusted when it carries the boolean. Otherwise a
    follow-up GET answers the same question. Success is never assumed.
    """
    if isinstance(response, Mapping):
        present = response.get("cookies_present")
        if isinstance(present, bool):
            return present
    try:
        record = await client.get_bot_user(uid, requested_by=requested_by)
    except (ApiError, ApiNetworkError) as exc:
        logger.warning("could not confirm cookies upload for %s: %s", uid, exc)
        return None
    if isinstance(record, Mapping):
        present = record.get("cookies_present")
        if isinstance(present, bool):
            return present
    return None


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

    # Track whether the user is in the two-step flow, so a private-chat
    # document that is not a .txt gets a useful reply instead of silence.
    _set_awaiting_file(context, record is None or not record.get("cookies_present"))

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
        # The agreement is only step one. Leave the file step pending so a
        # misplaced document in this chat can be answered specifically.
        _set_awaiting_file(context, True)
        await query.edit_message_text(_AGREE_ACK)
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
        _set_awaiting_file(context, False)
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

    document = getattr(message, "document", None)
    if document is None:
        return

    filename = getattr(document, "file_name", None) or ""
    mime = _document_mime(document)

    # Local import: uploads imports links, which imports this module. Calling
    # it directly keeps the flow single-shot: this handler already won the
    # dispatch for the update, so nothing after it will run.
    from . import uploads

    # Media by content intent, whatever the sender's cookies state: a video
    # sent as a document must never be mistaken for cookies.
    if _is_media_document(filename, mime):
        await uploads.handle_media_upload(update, context)
        return

    is_cookie_candidate = _is_cookie_candidate(filename, mime)

    # Cookies are personal, so a group is never a place to store one. Telling
    # the sender beats the generic "not supported" reply they would otherwise
    # get from the media catch-all.
    if _chat_type(update) != "private":
        if is_cookie_candidate:
            await message.reply_text(_GROUP_DOCUMENT)
        else:
            await uploads.handle_media_upload(update, context)
        return

    if not is_cookie_candidate:
        if _awaiting_file(context):
            await message.reply_text(_NOT_A_COOKIES_FILE)
        else:
            await uploads.handle_media_upload(update, context)
        return

    # A text document is a cookie candidate. Treat it as one while the user is
    # in the two-step flow, or when it is literally named cookies.txt.
    if not _awaiting_file(context) and filename.strip().lower() != "cookies.txt":
        await uploads.handle_media_upload(update, context)
        return

    user = update.effective_user
    uid = getattr(user, "id", None)
    if uid is None:
        await message.reply_text(
            "[unknown_user] I could not tell who sent this, so please try again."
        )
        return

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

    requested_by = actor_for(update)
    try:
        response = await client.upload_cookies(
            int(uid),
            bytes(payload),
            filename or "cookies.txt",
            requested_by=requested_by,
        )
    except ApiError as exc:
        logger.warning("cookies upload rejected for %s: %s", uid, exc)
        await notice.edit_text(_upload_error_message(exc))
        return
    except ApiNetworkError as exc:
        logger.warning("cookies upload failed on the network for %s: %s", uid, exc)
        await notice.edit_text(
            "A network error stopped the upload. Send the cookies.txt file "
            "again in a moment."
        )
        return

    # Only the server can confirm the file landed. A 2xx alone is not proof.
    confirmed = await _cookies_present_after_upload(client, int(uid), response, requested_by)
    if confirmed is True:
        _set_awaiting_file(context, False)
        # The upload does not record consent. Say the file landed and point at
        # the other step, without promising that agreement already exists.
        await notice.edit_text(
            "Cookies stored. Your cookies.txt file is on record. If you have "
            "not tapped I agree yet, run /cookies and tap it."
        )
        return
    if confirmed is False:
        await notice.edit_text(
            "The upload finished, but no cookies file is on record yet. Please "
            "send the cookies.txt file again as a document."
        )
        return
    await notice.edit_text(
        "The upload finished, but the server did not confirm that the file "
        "landed. Send the cookies.txt file again, then run /cookies to check."
    )