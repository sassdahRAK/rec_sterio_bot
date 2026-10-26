"""Every message the bot sends must be parseable by Telegram.

Regression for the reported crash::

    RecSterio: unexpected_error BadRequest: Can't parse entities:
    can't find end of the entity starting at byte offset 21

Byte 21 was the ``_`` in ``Usage: /status <video_id>`` — the bot set a global
``parse_mode``, so *every* message was parsed and an unbalanced ``_`` from a
usage string (or a video title, or an error code) aborted the send.

These tests reproduce Telegram's legacy-Markdown rules locally so a message
that would be rejected can never merge again.
"""
from __future__ import annotations

import pytest

from bot.client import ApiError, ApiNetworkError, escape_markdown
from bot.config import Settings
from bot.handlers.admin import jobretry_command, jobs_command, retry_command
from bot.handlers.media import media_command
from bot.handlers.merge import apply_command, merge_command, merges_command
from bot.handlers.start import WELCOME, help_command, start
from bot.handlers.status import status_command
from bot.handlers.transcript import transcript_command, transcripts_command
from bot.helpers import format_video_status
from tests.conftest import FakeContext, FakeUpdate

MARKDOWN = "Markdown"


def parse_legacy_markdown(text: str) -> None:
    """Raise ``AssertionError`` if Telegram's legacy Markdown would reject ``text``."""
    stack: list[tuple[str, int]] = []
    in_code = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and not in_code:
            i += 2  # escaped character, never an entity marker
            continue
        if in_code:
            # Inside a `code` span only the closing backtick is special.
            if ch == "`":
                in_code = False
                stack.pop()
            i += 1
            continue
        if ch == "`":
            in_code = True
            stack.append(("`", i))
        elif ch in ("*", "_"):
            if stack and stack[-1][0] == ch:
                stack.pop()
            else:
                stack.append((ch, i))
        elif ch == "[":
            stack.append(("[", i))
        elif ch == "]" and stack and stack[-1][0] == "[":
            stack.pop()
        i += 1
    if stack:
        marker, offset = stack[0]
        raise AssertionError(
            f"unclosed {marker!r} entity at byte offset {offset} in: {text!r}"
        )


def assert_replies_parse(replies: list[dict]) -> None:
    for reply in replies:
        if reply.get("parse_mode") == MARKDOWN:
            parse_legacy_markdown(reply["text"])


def _context(client, args=None, user_id: int = 42):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
    )
    return FakeContext(args=list(args or []), bot_data={"api": client, "settings": settings})


# ---------------------------------------------------------------- escaper


def test_escaper_covers_every_legacy_markdown_control_character():
    assert escape_markdown("a_b*c`d[e]") == "a\\_b\\*c\\`d\\[e]"


def test_escaper_does_not_add_backslashes_that_v1_shows_to_the_user():
    # '.' '-' '(' '!' etc. are not escaped in legacy Markdown; escaping them
    # makes the user see a literal backslash.
    plain = "(no title) - 2026.10.25!"
    assert escape_markdown(plain) == plain


def test_escaper_matches_telegrams_own_helper():
    from telegram.helpers import escape_markdown as ptb_escape

    sample = "Café _study_ *tips* [Phnom Penh] `2026.v2 - ok!"
    assert escape_markdown(sample) == ptb_escape(sample, version=1)


# ---------------------------------------------------------------- messages


def test_usage_strings_and_welcome_are_parseable():
    from bot.handlers.media import USAGE as MEDIA_USAGE
    from bot.handlers.merge import APPLY_USAGE, MERGE_USAGE, MERGES_USAGE
    from bot.handlers.status import USAGE as STATUS_USAGE
    from bot.handlers.transcript import USAGE as TRANSCRIPT_USAGE

    for text in (
        WELCOME,
        STATUS_USAGE,
        MEDIA_USAGE,
        TRANSCRIPT_USAGE,
        "Usage: /transcripts <video id>",
        MERGE_USAGE,
        MERGES_USAGE,
        APPLY_USAGE,
        "Usage: /retry <video id>",
        "Usage: /jobretry <job id>",
    ):
        parse_legacy_markdown(text)


def test_error_messages_are_parseable_even_when_codes_contain_underscores():
    error = ApiError(
        "validation_failed",
        "source_url must be a valid http(s) URL.",
        details=[{"field": "source_url", "issue": "invalid_url"}],
    )
    parse_legacy_markdown(error.user_message())
    parse_legacy_markdown(ApiNetworkError("offline").user_message())


def test_video_status_survives_a_hostile_title():
    video = {
        "id": "b86a215b-fe3a-49a8-a28a-1b4bba4c63f0",
        "status": "failed",
        "progress": 0,
        "title": "Café _study_ *tips* [Phnom Penh] `2026",
        "error": "download_error: [Errno 2] no_such_file",
        "transcripts": [],
    }
    parse_legacy_markdown(format_video_status(video))


# ---------------------------------------------------------------- handlers


COMMANDS = [
    ("start", start),
    ("help", help_command),
    ("status", status_command),
    ("transcript", transcript_command),
    ("transcripts", transcripts_command),
    ("media", media_command),
    ("merge", merge_command),
    ("merges", merges_command),
    ("apply", apply_command),
    ("jobs", jobs_command),
    ("retry", retry_command),
    ("jobretry", jobretry_command),
]


@pytest.mark.parametrize("name,handler", COMMANDS)
async def test_command_without_arguments_is_parseable(api_client, name, handler):
    update = FakeUpdate(user_id=999)  # admin so /jobs and /retry run
    ctx = _context(api_client, user_id=999)

    await handler(update, ctx)

    assert update.message.replies, f"/{name} sent nothing"
    assert_replies_parse(update.message.replies)


@pytest.mark.parametrize("name,handler", COMMANDS)
async def test_command_with_an_invalid_id_is_parseable(api_client, name, handler):
    if name in {"start", "help"}:
        pytest.skip("no id argument")
    update = FakeUpdate(user_id=999)
    ctx = _context(api_client, args=["not-a-uuid"], user_id=999)

    await handler(update, ctx)

    assert update.message.replies, f"/{name} sent nothing"
    assert_replies_parse(update.message.replies)


async def test_status_on_a_hostile_video_is_parseable(api_client, backend):
    video = await api_client.create_video("https://youtu.be/md", requested_by="telegram:1")
    backend.videos[video["id"]].update(
        {
            "title": "Café _study_ *tips* [Phnom Penh]",
            "error": "download_error: no_such_file",
        }
    )

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[video["id"]], user_id=1)

    await status_command(update, ctx)

    assert_replies_parse(update.message.replies)


async def test_missing_client_path_is_parseable():
    """``[bot not ready]`` used to be ``[not_ready]`` — the 'unclosed _' crash."""
    update = FakeUpdate()
    ctx = FakeContext()  # no "api" in bot_data

    await status_command(update, ctx)

    parse_legacy_markdown(update.message.replies[0]["text"])
