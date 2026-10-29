"""Dataset commands: folders, cards, sharing, passcodes and ZIP export.

Every command here is admin-gated, and that is a deliberate security decision
rather than an oversight. The bot authenticates with the single service token,
which the API treats as an *editor on every folder* (``app/access.py``), and
``visible_folders`` returns the whole tree for it — private folders included.
A command that listed folders for anyone holding the bot would therefore publish
the existence and ids of private folders. The public portal is the surface for
non-admin browsing; the bot is the operator's surface.

Nothing here invents behaviour: each command is a thin rendering of an endpoint
already frozen in ``docs/API_CONTRACT.md``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiError, ApiNetworkError
from ..helpers import (
    BOT_NOT_READY,
    actor_for,
    escape_markdown,
    format_bytes,
    format_duration,
    get_client,
    is_admin,
    is_uuid,
    web_links,
)

logger = logging.getLogger(__name__)

__all__ = [
    "cards_command",
    "cleaned_command",
    "folder_command",
    "folders_command",
    "mkdir_command",
    "passcode_command",
    "share_command",
    "zip_command",
]

NOT_AUTHORISED = "⛔ You are not authorised to use this command."

#: Telegram rejects a message longer than 4096 characters, and a folder tree or
#: card list can grow without bound. Rendering stops at this many lines and says
#: how much was left out, rather than failing the whole call.
MAX_LINES = 60

#: The Bot API caps an uploaded file at 50 MB. Stay under it so the request is
#: never rejected for size, and hand the user a link instead of a failed upload.
MAX_ZIP_BYTES = 45 * 1024 * 1024

#: A flag's value may not itself start with `--`, otherwise `--one-time --days 7`
#: would read `--days` as the value of `--one-time` and leave `7` as free text.
_FLAG_RE = re.compile(r"--([a-z][a-z-]*)(?:\s+(?!-)(\S+))?")


def _split_command(text: str) -> tuple[str, str]:
    """/mkdir a b --public -> ('/mkdir', 'a b --public')."""
    parts = (text or "").split(None, 1)
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _parse_flags(body: str) -> tuple[str, dict[str, str | None]]:
    """Split a command body into its free text and its ``--flags``.

    A flag with no value (``--public``) maps to ``None``; ``--parent <id>``
    maps to the id. The free text keeps its spacing, so a folder name may be
    any length and contain spaces — ``context.args`` would have split it.
    """
    flags: dict[str, str | None] = {}

    def _collect(match: re.Match[str]) -> str:
        flags[match.group(1)] = match.group(2)
        return " "

    remainder = _FLAG_RE.sub(_collect, body).strip()
    if len(remainder) >= 2 and remainder[0] == remainder[-1] and remainder[0] in "\"'":
        remainder = remainder[1:-1].strip()
    return remainder, flags


async def _guarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Admin gate shared by every command here. False means "already replied"."""
    message = update.effective_message
    if message is None:
        return False
    if not is_admin(update, context):
        await message.reply_text(NOT_AUTHORISED)
        return False
    return True


async def _call(update: Update, context: ContextTypes.DEFAULT_TYPE, awaitable):
    """Run an API call and render its failure the way every other handler does."""
    message = update.effective_message
    try:
        return await awaitable
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
    return None


def _clip(lines: list[str]) -> list[str]:
    """Keep the render under Telegram's message limit, saying what was cut."""
    if len(lines) <= MAX_LINES:
        return lines
    hidden = len(lines) - MAX_LINES + 1
    return lines[: MAX_LINES - 1] + [f"… and {hidden} more. Narrow the query or add a filter."]


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []


# ---------------------------------------------------------------- folders


def _folder_line(node: Mapping[str, Any], depth: int = 0) -> list[str]:
    """One folder as an indented line, recursing into its children."""
    name = escape_markdown(str(node.get("name") or "(untitled)"))
    visibility = node.get("visibility", "private")
    lock = "🌐" if visibility == "public" else "🔒"
    count = node.get("video_count", 0)
    role = escape_markdown(str(node.get("my_role") or "-"))
    lines = [
        f"{'  ' * depth}{lock} *{name}* — {count} video(s) · role `{role}`"
    ]
    lines.append(f"{'  ' * depth}  `{node.get('id', '?')}`")
    for child in node.get("children") or []:
        if isinstance(child, Mapping):
            lines.extend(_folder_line(child, depth + 1))
    return lines


async def folders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the folder tree — admins only."""
    message = update.effective_message
    if message is None or not await _guarded(update, context):
        return

    args = [a for a in (context.args or []) if a]
    parent_id = args[0].strip() if args else None
    if parent_id and not is_uuid(parent_id):
        await message.reply_text(
            f"`{parent_id}` is not a valid folder id.", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    payload = await _call(
        update,
        context,
        client.list_folders(parent_id=parent_id),
    )
    if payload is None:
        return

    items = _items(payload)
    if not items:
        await message.reply_text("No folders found.")
        return

    lines = ["*Folders*"] + [line for node in items for line in _folder_line(node)]
    await message.reply_text(
        "\n".join(_clip(lines)),
        parse_mode=ParseMode.MARKDOWN,
    )


async def folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One folder with its metadata and immediate children — admins only."""
    message = update.effective_message
    if message is None or not await _guarded(update, context):
        return

    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text("Usage: /folder <folder id>")
        return
    folder_id = args[0].strip()
    if not is_uuid(folder_id):
        await message.reply_text(
            f"`{folder_id}` is not a valid folder id.", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    node = await _call(update, context, client.get_folder(folder_id))
    if node is None:
        return

    lock = "🌐 public" if node.get("visibility") == "public" else "🔒 private"
    owner = escape_markdown(str(node.get("owner_email") or "—"))
    name = escape_markdown(str(node.get("name") or "(untitled)"))
    description = node.get("description")
    lines = [
        f"*{name}*",
        f"`{node.get('id', '?')}`",
        f"{lock} · owner {owner} · {node.get('video_count', 0)} video(s)",
        f"role `{escape_markdown(str(node.get('my_role') or '-'))}`",
    ]
    if description:
        lines.append(f"_{escape_markdown(str(description))}_")

    children = [c for c in (node.get("children") or []) if isinstance(c, Mapping)]
    if children:
        lines.append("")
        lines.append("*Sub-folders*")
        for child in children:
            child_lock = "🌐" if child.get("visibility") == "public" else "🔒"
            child_name = escape_markdown(str(child.get("name") or "(untitled)"))
            lines.append(
                f"{child_lock} *{child_name}* — {child.get('video_count', 0)} video(s)"
            )
            lines.append(f"  `{child.get('id', '?')}`")

    # Portal link only, even for admins: the console has no per-folder route
    # (just /dataset), so there is no console URL this button could open.
    await message.reply_text(
        "\n".join(_clip(lines)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=web_links(update, context, folder_id=folder_id),
    )


# ---------------------------------------------------------------- cards


def _card_lines(cards: list[Any]) -> list[str]:
    lines: list[str] = []
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        title = escape_markdown(str(card.get("title") or "(no title)"))
        badge = "✅ cleaned" if card.get("badge") == "cleaned" else "📄 raw"
        duration = format_duration(card.get("duration_seconds") or 0)
        lines.append(f"• *{title}* · {badge} · {duration}")
        folder_name = card.get("folder_name")
        if folder_name:
            lines.append(f"  _{escape_markdown(str(folder_name))}_")
        preview = card.get("transcript_preview")
        if preview:
            lines.append(f"  _{escape_markdown(str(preview))}_")
        lines.append(f"  `{card.get('video_id', '?')}`")
    return lines


def _parse_card_args(args: list[str]) -> tuple[str | None, str | None]:
    """`/cards <folder id> <query…>` — the folder id is optional and positional."""
    if args and is_uuid(args[0]):
        return args[0], " ".join(args[1:]).strip() or None
    return None, " ".join(args).strip() or None


async def _list_cards(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    cleaned: bool,
) -> None:
    message = update.effective_message
    if message is None or not await _guarded(update, context):
        return

    args = [a for a in (context.args or []) if a]
    folder_id, query = _parse_card_args(args)

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    # `cleaned` is a *filter* on the API, not a default: omitting it returns
    # every unit, while `cleaned=false` means "raw only" and would silently
    # drop the hand-corrected units from the plain /cards listing.
    payload = await _call(
        update,
        context,
        client.list_cards(
            folder_id=folder_id,
            cleaned=True if cleaned else None,
            q=query,
            page_size=10,
        ),
    )
    if payload is None:
        return

    items = _items(payload)
    total = payload.get("total") if isinstance(payload, Mapping) else len(items)
    heading = "Cleaned units" if cleaned else "Units"
    if not items:
        await message.reply_text(f"No {heading.lower()} found.")
        return

    lines = [f"*{heading}* (total {total})"] + _card_lines(items)
    # A folder-scoped listing opens that folder in the portal (see the
    # /folder comment for why admins get no console button here); a listing
    # across all folders has no single page to open, so no button either.
    await message.reply_text(
        "\n".join(_clip(lines)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=web_links(update, context, folder_id=folder_id),
    )


async def cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dataset cards, optionally narrowed to a folder and/or a search term."""
    await _list_cards(update, context, cleaned=False)


async def cleaned_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only the hand-corrected units (instruction.txt #8)."""
    await _list_cards(update, context, cleaned=True)


# ---------------------------------------------------------------- management


async def mkdir_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a folder — admins only.

    ``/mkdir <name> [--public] [--parent <id>]``
    """
    message = update.effective_message
    if message is None or not await _guarded(update, context):
        return

    _, body = _split_command(message.text)
    name, flags = _parse_flags(body)
    if not name:
        await message.reply_text("Usage: /mkdir <name> [--public] [--parent <id>]")
        return

    parent_id = flags.get("parent")
    if parent_id and not is_uuid(parent_id):
        await message.reply_text(
            f"`{parent_id}` is not a valid parent folder id.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    visibility = "public" if "public" in flags else "private"

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    node = await _call(
        update,
        context,
        client.create_folder(
            name,
            visibility=visibility,
            parent_id=parent_id,
            requested_by=actor_for(update),
        ),
    )
    if node is None:
        return

    lock = "🌐 public" if node.get("visibility") == "public" else "🔒 private"
    await message.reply_text(
        "Created {} *{}*\n`{}`".format(
            lock,
            escape_markdown(str(node.get("name") or name)),
            node.get("id", "?"),
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a share link — admins only.

    ``/share <folder id> <role> [days]`` — role is viewer, commenter or editor.
    """
    message = update.effective_message
    if message is None or not await _guarded(update, context):
        return

    args = [a for a in (context.args or []) if a]
    if len(args) < 2:
        await message.reply_text("Usage: /share <folder id> <role> [days]")
        return
    folder_id, role = args[0].strip(), args[1].strip().lower()
    if not is_uuid(folder_id):
        await message.reply_text(
            f"`{folder_id}` is not a valid folder id.", parse_mode=ParseMode.MARKDOWN
        )
        return
    if role not in {"viewer", "commenter", "editor"}:
        await message.reply_text("Role must be viewer, commenter or editor.")
        return

    days = None
    if len(args) >= 3:
        try:
            days = int(args[2])
        except ValueError:
            await message.reply_text("`days` must be a whole number of days.")
            return
        if days <= 0:
            await message.reply_text("`days` must be greater than zero.")
            return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    share = await _call(
        update,
        context,
        client.create_share(
            folder_id,
            role,
            expires_at=_days_from_now(days),
            requested_by=actor_for(update),
        ),
    )
    if share is None:
        return

    url = str(share.get("url") or "(no url returned)")
    expiry = (
        f" · expires {share.get('expires_at')}"
        if share.get("expires_at")
        else " · never expires"
    )
    await message.reply_text(
        f"🔗 Share link · role *{escape_markdown(role)}*{escape_markdown(expiry)}\n"
        f"`{escape_markdown(url)}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def passcode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Issue a door code for a private folder — admins only.

    ``/passcode <folder id> [label] [--uses N] [--devices N] [--one-time] [--days N]``

    The code is generated by the server and shown **once**, here. The API stores
    only a hash, so it cannot be recovered later.
    """
    message = update.effective_message
    if message is None or not await _guarded(update, context):
        return

    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(
            "Usage: /passcode <folder id> [label] [--uses N] [--devices N] "
            "[--one-time] [--days N]"
        )
        return
    folder_id = args[0].strip()
    if not is_uuid(folder_id):
        await message.reply_text(
            f"`{folder_id}` is not a valid folder id.", parse_mode=ParseMode.MARKDOWN
        )
        return

    # The folder id is positional and already consumed; the rest is label + flags.
    rest = _after_first_token(message.text)
    _, flags = _parse_flags(rest)
    label = _first_positional(rest)

    def _int_flag(name: str) -> int | None:
        raw = flags.get(name)
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return -1
        return value if value > 0 else -1

    max_uses = _int_flag("uses")
    max_devices = _int_flag("devices")
    for name, value in (("--uses", max_uses), ("--devices", max_devices)):
        if value == -1:
            await message.reply_text(f"`{name}` must be a positive whole number.")
            return

    days = _int_flag("days")
    if days == -1:
        await message.reply_text("`--days` must be a positive whole number.")
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    passcode = await _call(
        update,
        context,
        client.create_passcode(
            folder_id,
            label=label,
            expires_at=_days_from_now(days),
            max_uses=max_uses,
            max_devices=max_devices,
            one_time="one-time" in flags or "one_time" in flags,
            requested_by=actor_for(update),
        ),
    )
    if passcode is None:
        return

    code = passcode.get("code")
    if not code:
        # Only a caller-supplied code is not echoed back by the API.
        code = "(set by you — the server does not echo it back)"

    limits = []
    if max_uses:
        limits.append(f"{max_uses} use(s)")
    if max_devices:
        limits.append(f"{max_devices} device(s)")
    if "one-time" in flags or "one_time" in flags:
        limits.append("one-time")
    if days:
        limits.append(f"{days} day(s)")
    suffix = f" · {', '.join(limits)}" if limits else ""

    await message.reply_text(
        f"🔑 Passcode for `{folder_id}`\n"
        f"`{escape_markdown(str(code))}`\n"
        f"Save it now — it is shown once{suffix}.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def zip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export a folder and its subtree as one ZIP — admins only.

    Sends the archive when it fits a Telegram upload, otherwise falls back to the
    portal link, because the export guard rails permit archives far larger than
    the Bot API's 50 MB upload cap.
    """
    message = update.effective_message
    if message is None or not await _guarded(update, context):
        return

    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text("Usage: /zip <folder id>")
        return
    folder_id = args[0].strip()
    if not is_uuid(folder_id):
        await message.reply_text(
            f"`{folder_id}` is not a valid folder id.", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    archive = await _call(update, context, client.download_folder_zip(folder_id))
    if archive is None:
        return

    if not archive:
        await message.reply_text(
            "This folder has no downloadable media yet."
        )
        return

    name = f"recsterio-folder-{folder_id[:8]}.zip"
    size = len(archive)
    if size <= MAX_ZIP_BYTES:
        await message.reply_document(
            document=archive,
            filename=name,
            caption=f"📦 {name} ({format_bytes(size)})",
        )
        return

    await message.reply_text(
        f"📦 The archive is {format_bytes(size)}, which is over Telegram's "
        f"{format_bytes(MAX_ZIP_BYTES)} upload limit.\n"
        f"Export it from the console instead: `/folder {folder_id}` for its id, "
        "then use the portal download.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------- helpers


def _after_first_token(text: str) -> str:
    """/passcode <id> label --uses 3 -> 'label --uses 3'."""
    body = _split_command(text)[1]
    parts = body.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def _first_positional(body: str) -> str | None:
    remainder, _ = _parse_flags(body)
    return remainder.strip() or None


def _days_from_now(days: int | None) -> str | None:
    """An ISO-8601 UTC timestamp `days` from now, or None for "never"."""
    if not days:
        return None
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
