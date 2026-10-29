"""Console account linking: ``/start link-<code>``, its buttons, and ``/whoami``.

The console's Settings page shows a one-time link code; opening the bot with
``/start link-<code>`` redeems it in two phases — a dry-run that names the
console account the code points at, then a Confirm tap that makes the link.
``/whoami`` reports the linked identity, the cookies/consent state, and
whether this Telegram user has bot admin rights (and from which source).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..config import get_settings
from ..helpers import _admin_cache_ids, actor_for, get_client

logger = logging.getLogger(__name__)

__all__ = ["handle_start_link", "on_callback", "whoami_command"]

#: 401 from the dry-run: the code is wrong, expired or already used.
INVALID_CODE_MESSAGE = (
    "That link code is not valid (wrong, expired, or already used). "
    "Generate a fresh one from the console Settings page."
)

#: 401 at confirm time: the code was valid when shown, but is not any more.
STALE_CODE_MESSAGE = (
    "That code is no longer valid — generate a fresh one from the console."
)


def _code_span(value: Any) -> str:
    """Render a value inside a legacy-Markdown `` ` `` code span, safely.

    Nothing can be escaped inside a code span — a stray backtick in the value
    would break Telegram's entity parsing — so backticks are neutralised
    instead (``escape_markdown`` is pointless here and would show literal
    backslashes inside the span).
    """
    text = str(value) if value not in (None, "") else "?"
    return text.replace("`", "'")


def _account_field(account: Any, key: str) -> Any:
    return account.get(key) if isinstance(account, Mapping) else None


async def handle_start_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, code: str
) -> None:
    """``/start link-<code>`` — dry-run the link, then offer Confirm / Cancel."""
    message = update.effective_message
    user = update.effective_user
    if message is None:
        return
    uid = getattr(user, "id", None)
    if uid is None:
        await message.reply_text(
            "[unknown_user] I could not tell who is asking — please try again."
        )
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await message.reply_text(f"[bot not ready] {exc}")
        return

    try:
        # Phase 1 — the dry-run: the backend only names the target account.
        target = await client.redeem_link(
            int(uid), code, confirm=False, requested_by=actor_for(update)
        )
    except ApiError as exc:
        if exc.status_code == 401 or exc.code == "unauthorized":
            await message.reply_text(INVALID_CODE_MESSAGE)
        else:
            # 409 already_linked (and anything else) carries its own message.
            await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    await message.reply_text(
        "This will link this Telegram to the console account "
        f"`{_code_span(_account_field(target, 'account_email'))}`.\n\n"
        "Confirm to continue.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm", callback_data=f"link:confirm:{code}"),
                    InlineKeyboardButton("Cancel", callback_data=f"link:cancel:{code}"),
                ]
            ]
        ),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The link dialog's two buttons: Confirm makes the link, Cancel abandons it."""
    query = update.callback_query
    if query is None:
        return
    data = str(query.data or "")
    # "link:confirm:<code>" / "link:cancel:<code>" — split on the first two
    # colons only, so the code is always the whole remainder (a single token).
    parts = data.split(":", 2)
    if len(parts) < 3 or parts[0] != "link":
        return

    # Answer first so the button never spins, whatever happens next.
    await query.answer()

    user = getattr(query, "from_user", None)
    uid = getattr(user, "id", None)
    if uid is None:  # pragma: no cover - PTB always sets from_user
        return

    verb, code = parts[1], parts[2]
    if verb == "cancel":
        await query.edit_message_text("Not linked.")
        return
    if verb != "confirm":
        return

    try:
        client = get_client(context)
    except RuntimeError as exc:
        await query.answer(text=str(exc)[:180], show_alert=True)
        return

    try:
        # Phase 2 — the confirming call that actually makes the link.
        account = await client.redeem_link(
            int(uid), code, confirm=True, requested_by=actor_for(update)
        )
    except ApiError as exc:
        if exc.status_code == 401 or exc.code == "unauthorized":
            await query.edit_message_text(STALE_CODE_MESSAGE)
        else:
            # 409 already_linked (and anything else) carries its own message.
            await query.edit_message_text(
                exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
        return
    except ApiNetworkError as exc:
        await query.edit_message_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    await query.edit_message_text(
        "Linked — this Telegram is now "
        f"`{_code_span(_account_field(account, 'account_name'))}` "
        f"(`{_code_span(_account_field(account, 'account_email'))}`), "
        f"role `{_code_span(_account_field(account, 'account_role'))}`.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # A fresh link can grant admin rights, so refresh the cache now instead
    # of waiting for the next background sweep. Tolerate an absent cache
    # (e.g. a test harness, or post_shutdown already ran).
    cache = (context.bot_data or {}).get("admin_ids_cache")
    if cache is not None:
        await cache.refresh(client)


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/whoami`` — this Telegram's identity, links, and bot admin status."""
    message = update.effective_message
    user = update.effective_user
    if message is None:
        return
    if user is None:
        await message.reply_text(
            "[unknown_user] I could not tell who is asking — please try again."
        )
        return

    uid = getattr(user, "id", None)
    # Plain text, no parse_mode: nothing interpolated needs escaping.
    lines = [f"Telegram id: {uid}"]
    username = getattr(user, "username", None)
    if username:
        lines.append(f"@{username}")

    try:
        client = get_client(context)
    except RuntimeError as exc:
        lines.append(f"[bot not ready] {exc}")
        await message.reply_text("\n".join(lines))
        return

    try:
        record = await client.get_bot_user(int(uid), requested_by=actor_for(update))
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    # No record at all (404) means the same thing as an empty one: nothing
    # linked, nothing consented.
    record = record if isinstance(record, Mapping) else None
    linked_email = (record or {}).get("linked_email")
    if linked_email:
        lines.append(
            "Linked console account: {name} ({email}), role {role}".format(
                name=(record or {}).get("linked_name") or "(no name)",
                email=linked_email,
                role=(record or {}).get("linked_role") or "unknown",
            )
        )
    else:
        lines.append(
            "No linked console account — link one from the console Settings page."
        )

    if record is not None and record.get("consented") and record.get("cookies_present"):
        lines.append("TikTok/Facebook downloads: consented + cookies on file")
    else:
        lines.append("TikTok/Facebook downloads: not set up — run /cookies")

    # Admin status — env allow-list first, then the linked-console cache, so
    # the source named in the reply is the one that actually granted rights.
    settings = (context.bot_data or {}).get("settings") or get_settings()
    if settings.is_admin(uid):
        lines.append("Bot admin: yes (operator allow-list)")
    elif uid in _admin_cache_ids(context):
        lines.append("Bot admin: yes (linked console account)")
    else:
        lines.append("Bot admin: no")

    await message.reply_text("\n".join(lines))