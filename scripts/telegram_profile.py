"""Own the Telegram bot's profile text and command menu.

The bot's description, short description and command-menu descriptions are
stored on Telegram's servers, not in this repo. Without an owner they drift
from the code and a redeploy (or a new token) silently loses hand-made fixes.
This file is that owner: edit the constants below, then run the script.

Usage (from the bot repo root, where ``.env`` lives)::

    .venv/bin/python -m scripts.telegram_profile           # diff, then write
    .venv/bin/python -m scripts.telegram_profile --check   # diff only (CI)
    .venv/bin/python -m scripts.telegram_profile --dry-run # print, no network

The token is read from ``.env`` through :class:`bot.config.Settings` and is
never printed. Network calls use ``python-telegram-bot``'s ``Bot`` API; do not
swap this for ``urllib`` (TLS interception on some hosts breaks it).

The list below is checked against the handlers registered in
``bot.handlers``: a command listed here without a handler is a hard error,
while a registered command that is not listed (admin and dataset commands are
deliberately hidden) is only a warning.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Bot, BotCommand
from telegram.error import TelegramError

from bot.config import Settings

__all__ = [
    "COMMANDS",
    "DESCRIPTION",
    "SHORT_DESCRIPTION",
    "ProfileState",
    "ProfileValidationError",
    "compute_diff",
    "registered_command_names",
    "validate_profile",
]

# --- Source of truth -------------------------------------------------------
# Telegram limits: description <= 512 chars, short description <= 120 chars,
# at most 100 commands, each command description 3..256 chars.
#
# Visible copy must not use em dashes, en dashes, a spaced hyphen, or refer to
# the old TvNa / capstone / Group 10 framing. Those are standing requirements
# for user-visible text; validate_profile enforces them before any network call.

DESCRIPTION = (
    "Send me a video link or file. I save the video and its audio, transcribe "
    "the Khmer and English speech, and keep all three as one unit you can "
    "open, read and correct.\n\n"
    "Quick start:\n"
    "- Paste a media link. I download it and start transcribing.\n"
    "- /collect: save a clip with where it was filmed and why it matters\n"
    "- /cookies: one-time setup for TikTok and Facebook links\n"
    "- /media <id>: get the stored video or audio back in this chat\n"
    "- /help: every command\n\n"
    "Only public content you have the rights to."
)

# EDIT ME: proposed one-liner. Telegram shows it before the user taps Start.
# Keep it under 120 characters and free of the forbidden wording/dashes.
SHORT_DESCRIPTION = (
    "Save a public video, transcribe its Khmer and English speech, and keep "
    "video, audio and transcript together."
)

# Ordered exactly as Telegram will show it.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Welcome and a quick overview"),
    ("help", "Every command explained"),
    ("cookies", "One-time setup so TikTok/Facebook links work"),
    ("collect", "Save a clip with where it was filmed and why it matters"),
    ("record", "Show one video's collection record"),
    ("records", "The 20 most recent videos"),
    ("status", "Check a download or job"),
    ("transcript", "Read a video's transcript"),
    ("transcripts", "List a transcript's versions"),
    ("edit", "Correct a transcript (new version, never overwritten)"),
    ("cancel", "Cancel the current action"),
    ("media", "Send a video back into the chat"),
    ("audio", "The stored mp3, sent on request"),
    ("merge", "Queue a Whisper x Gemini merge"),
    ("merges", "List a video's merges"),
    ("apply", "Promote a ready merge to the current transcript"),
)

MAX_DESCRIPTION_LENGTH = 512
MAX_SHORT_DESCRIPTION_LENGTH = 120
MAX_COMMANDS = 100
MIN_COMMAND_DESCRIPTION_LENGTH = 3
MAX_COMMAND_DESCRIPTION_LENGTH = 256

# Forbidden visible copy. Dashes are checked as literals; words are checked
# case-insensitively so "Capstone" and "TV NA" are caught too.
FORBIDDEN_DASHES: tuple[tuple[str, str], ...] = (
    ("\u2014", "em dash"),
    ("\u2013", "en dash"),
    (" - ", "spaced hyphen"),
)
FORBIDDEN_WORDS: tuple[str, ...] = ("tvna", "tv na", "capstone", "group 10")

_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ProfileValidationError(ValueError):
    """Desired profile copy failed a length or wording rule."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


@dataclass(frozen=True)
class ProfileState:
    """The three profile fields we own, in one comparable value."""

    description: str
    short_description: str
    commands: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FieldDiff:
    """One field whose live value differs from the desired value."""

    name: str
    old: Any
    new: Any


DESIRED = ProfileState(
    description=DESCRIPTION,
    short_description=SHORT_DESCRIPTION,
    commands=COMMANDS,
)


# --- Pure validation -------------------------------------------------------


def validate_text(field: str, text: str, max_length: int, min_length: int = 0) -> None:
    """Check one free-text field's length and forbidden wording."""
    if len(text) < min_length:
        raise ProfileValidationError(
            field,
            f"must be at least {min_length} characters (got {len(text)})",
        )
    if len(text) > max_length:
        raise ProfileValidationError(
            field,
            f"must be at most {max_length} characters (got {len(text)})",
        )
    for needle, label in FORBIDDEN_DASHES:
        if needle in text:
            raise ProfileValidationError(
                field,
                f"contains a forbidden {label}; use plain wording without dashes",
            )
    lowered = text.lower()
    for word in FORBIDDEN_WORDS:
        if word in lowered:
            raise ProfileValidationError(
                field,
                f"contains forbidden wording {word!r}",
            )


def validate_commands(commands: tuple[tuple[str, str], ...]) -> None:
    """Check the command list's size, names and per-description limits."""
    if len(commands) > MAX_COMMANDS:
        raise ProfileValidationError(
            "commands",
            f"must contain at most {MAX_COMMANDS} entries (got {len(commands)})",
        )
    seen: set[str] = set()
    for name, description in commands:
        name_field = f"commands[{name or '?'}].name"
        if not _COMMAND_NAME_RE.match(name):
            raise ProfileValidationError(
                name_field,
                "must be 1..32 lowercase letters, digits or underscores",
            )
        if name in seen:
            raise ProfileValidationError(name_field, "is listed more than once")
        seen.add(name)
        validate_text(
            f"commands[{name}].description",
            description,
            MAX_COMMAND_DESCRIPTION_LENGTH,
            MIN_COMMAND_DESCRIPTION_LENGTH,
        )


def validate_profile(
    description: str,
    short_description: str,
    commands: tuple[tuple[str, str], ...],
) -> None:
    """Validate every desired field. Raises on the first problem."""
    validate_text("description", description, MAX_DESCRIPTION_LENGTH)
    validate_text(
        "short_description", short_description, MAX_SHORT_DESCRIPTION_LENGTH
    )
    validate_commands(commands)


# --- Pure diff and handler agreement --------------------------------------


def compute_diff(current: ProfileState, desired: ProfileState) -> list[FieldDiff]:
    """Return one :class:`FieldDiff` per field that actually differs."""
    diffs: list[FieldDiff] = []
    if current.description != desired.description:
        diffs.append(
            FieldDiff("description", current.description, desired.description)
        )
    if current.short_description != desired.short_description:
        diffs.append(
            FieldDiff(
                "short_description",
                current.short_description,
                desired.short_description,
            )
        )
    if current.commands != desired.commands:
        diffs.append(FieldDiff("commands", current.commands, desired.commands))
    return diffs


class _HandlerRecorder:
    """Stand-in for Application that records handlers instead of storing them."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def add_handler(self, handler: Any, *args: Any, **kwargs: Any) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler: Any, *args: Any, **kwargs: Any) -> None:
        return None


def _iter_command_handlers(handler: Any) -> list[Any]:
    """Flatten CommandHandlers, including those inside ConversationHandlers."""
    from telegram.ext import CommandHandler, ConversationHandler

    found: list[Any] = []
    if isinstance(handler, CommandHandler):
        found.append(handler)
    elif isinstance(handler, ConversationHandler):
        found.extend(_flatten(handler.entry_points))
        found.extend(_flatten(handler.fallbacks))
        for state_handlers in handler.states.values():
            found.extend(_flatten(state_handlers))
    return found


def _flatten(handlers: Any) -> list[Any]:
    found: list[Any] = []
    for handler in handlers or ():
        found.extend(_iter_command_handlers(handler))
    return found


def registered_command_names() -> tuple[str, ...]:
    """Every command name wired into ``bot.handlers.register_handlers``."""
    from bot.handlers import register_handlers

    recorder = _HandlerRecorder()
    register_handlers(recorder)  # type: ignore[arg-type]
    names: set[str] = set()
    for handler in recorder.handlers:
        for command_handler in _iter_command_handlers(handler):
            names.update(command_handler.commands)
    return tuple(sorted(names))


def compare_commands(
    desired: tuple[tuple[str, str], ...],
    registered: tuple[str, ...] | set[str],
) -> tuple[list[str], list[str]]:
    """Return (missing_handlers, unlisted_registered).

    ``missing_handlers`` is a hard error: a listed command that no handler
    serves. ``unlisted_registered`` is a warning: a registered command that is
    deliberately absent from the public menu (admin and dataset commands).
    """
    desired_names = {name for name, _ in desired}
    registered_names = set(registered)
    return (
        sorted(desired_names - registered_names),
        sorted(registered_names - desired_names),
    )


# --- Rendering -------------------------------------------------------------


def _render(name: str, value: Any) -> str:
    if name == "commands":
        commands = value or ()
        if not commands:
            return "    (no commands)"
        return "\n".join(f"    /{command}: {text}" for command, text in commands)
    text = value or ""
    if not text:
        return "    (empty)"
    return "\n".join("    " + line for line in text.splitlines())


def print_diffs(diffs: list[FieldDiff]) -> None:
    for diff in diffs:
        print(f"\n{diff.name}:")
        print("  old:")
        print(_render(diff.name, diff.old))
        print("  new:")
        print(_render(diff.name, diff.new))


def print_plan() -> None:
    print("Would send this profile to Telegram:")
    print("\ndescription:")
    print(_render("description", DESCRIPTION))
    print("\nshort_description:")
    print(_render("short_description", SHORT_DESCRIPTION))
    print("\ncommands:")
    print(_render("commands", COMMANDS))


# --- Network ---------------------------------------------------------------


async def fetch_current(bot: Bot) -> ProfileState:
    """Read the live profile from Telegram."""
    description = await bot.get_my_description()
    short_description = await bot.get_my_short_description()
    commands = await bot.get_my_commands()
    return ProfileState(
        description=description.description or "",
        short_description=short_description.short_description or "",
        commands=tuple((item.command, item.description) for item in commands),
    )


async def apply_diffs(bot: Bot, diffs: list[FieldDiff]) -> None:
    """Write only the fields that differ."""
    for diff in diffs:
        if diff.name == "description":
            await bot.set_my_description(description=diff.new)
        elif diff.name == "short_description":
            await bot.set_my_short_description(short_description=diff.new)
        elif diff.name == "commands":
            await bot.set_my_commands(
                [
                    BotCommand(command=command, description=description)
                    for command, description in diff.new
                ]
            )


async def _sync(check_only: bool) -> int:
    settings = Settings(_env_file=_ENV_FILE)
    token = settings.telegram_bot_token
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN is not set in the bot's .env file.",
            file=sys.stderr,
        )
        return 2
    try:
        async with Bot(token=token) as bot:
            current = await fetch_current(bot)
            diffs = compute_diff(current, DESIRED)
            if not diffs:
                if check_only:
                    print(
                        "No drift: Telegram matches scripts/telegram_profile.py."
                    )
                else:
                    print("Nothing to change.")
                return 0
            print_diffs(diffs)
            if check_only:
                print(
                    "\nDrift detected. Run without --check to update Telegram."
                )
                return 1
            await apply_diffs(bot, diffs)
            print(
                "\nApplied. Telegram now matches scripts/telegram_profile.py."
            )
            return 0
    except TelegramError as exc:
        detail = (
            getattr(exc, "description", None)
            or getattr(exc, "message", None)
            or str(exc)
        )
        print(f"Telegram API error: {detail}", file=sys.stderr)
        return 2


# --- CLI -------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync the Telegram bot profile with this file.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="show the diff and exit 1 on drift; never writes",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent, without contacting Telegram",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Validate before any network call, so bad copy never reaches Telegram.
    try:
        validate_profile(DESCRIPTION, SHORT_DESCRIPTION, COMMANDS)
    except ProfileValidationError as exc:
        print(f"Profile validation failed: {exc}", file=sys.stderr)
        return 2

    missing, unlisted = compare_commands(COMMANDS, registered_command_names())
    if missing:
        print(
            "Commands listed here have no registered handler: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    if unlisted:
        print(
            "Warning: registered commands not in the public menu (hidden on "
            "purpose): " + ", ".join(unlisted),
            file=sys.stderr,
        )

    if args.dry_run:
        print_plan()
        return 0

    return asyncio.run(_sync(check_only=args.check))


if __name__ == "__main__":
    raise SystemExit(main())
