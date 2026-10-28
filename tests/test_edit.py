"""Editing a transcript from the chat (instruction.txt #3)."""
from __future__ import annotations

import pytest

from bot.handlers import transcript
from tests.conftest import FakeContext, FakeUpdate

VIDEO_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def wired(api_client, backend):
    """The API client plus the fake backend it talks to (same instance)."""
    backend.videos[VIDEO_ID] = {
        "id": VIDEO_ID,
        "source_url": "https://example.com/a.mp4",
        "platform": "direct",
        "title": "Clip",
        "status": "transcribed",
        "progress": 100,
        "transcripts": [
            {
                "id": "tr-1",
                "video_id": VIDEO_ID,
                "version": 1,
                "source": "whisper",
                "content": "original raw text",
                "status": "ready",
                "cleaned": False,
            }
        ],
    }
    return api_client, backend


def make_context(api_client, *, args=None, user_data=None):
    return FakeContext(args=args, bot_data={"api": api_client}, user_data=user_data)


async def test_edit_with_inline_text_saves_a_new_version(wired):
    api_client, backend = wired
    update = FakeUpdate("/edit " + VIDEO_ID + " corrected text")
    context = make_context(api_client, args=[VIDEO_ID, "corrected", "text"])

    await transcript.edit_command(update, context)

    assert len(backend.saved_transcripts) == 1
    saved = backend.saved_transcripts[0]
    assert saved["content"] == "corrected text"
    assert saved["version"] == 2  # v1 preserved
    assert saved["cleaned"] is True
    assert "v2" in update.message.replies[-1]["text"]


async def test_edit_without_text_opens_a_draft(wired):
    api_client, backend = wired
    update = FakeUpdate("/edit " + VIDEO_ID)
    context = make_context(api_client, args=[VIDEO_ID])

    await transcript.edit_command(update, context)

    # No write yet — the bot is waiting for the next message.
    assert backend.saved_transcripts == []
    assert context.user_data[transcript.PENDING_EDIT_KEY] == VIDEO_ID
    assert "original raw text" in update.message.replies[-1]["text"]


async def test_the_next_message_becomes_the_edit(wired):
    api_client, backend = wired
    context = make_context(api_client, args=[VIDEO_ID])
    await transcript.edit_command(FakeUpdate("/edit " + VIDEO_ID), context)

    follow_up = FakeUpdate("the corrected transcript body")
    consumed = await transcript.handle_pending_edit(follow_up, context)

    assert consumed is True
    assert backend.saved_transcripts[0]["content"] == "the corrected transcript body"
    # The draft is closed, so a second message is not another edit.
    assert transcript.PENDING_EDIT_KEY not in context.user_data


async def test_a_command_does_not_consume_the_draft(wired):
    api_client, _ = wired
    context = make_context(api_client, args=[VIDEO_ID])
    await transcript.edit_command(FakeUpdate("/edit " + VIDEO_ID), context)

    # /status must not be swallowed as the edit body, and must not drop it.
    consumed = await transcript.handle_pending_edit(FakeUpdate("/status"), context)

    assert consumed is False
    assert context.user_data[transcript.PENDING_EDIT_KEY] == VIDEO_ID


async def test_no_draft_means_the_message_is_not_an_edit(wired):
    api_client, _ = wired
    context = make_context(api_client)

    consumed = await transcript.handle_pending_edit(FakeUpdate("just a link"), context)

    assert consumed is False


async def test_cancel_clears_the_draft(wired):
    api_client, backend = wired
    context = make_context(api_client, args=[VIDEO_ID])
    await transcript.edit_command(FakeUpdate("/edit " + VIDEO_ID), context)

    update = FakeUpdate("/cancel")
    await transcript.cancel_command(update, context)

    assert transcript.PENDING_EDIT_KEY not in context.user_data
    assert backend.saved_transcripts == []
    assert "cancelled" in update.message.replies[-1]["text"].lower()


async def test_edit_without_arguments_shows_usage(wired):
    api_client, _ = wired
    update = FakeUpdate("/edit")
    await transcript.edit_command(update, make_context(api_client))

    assert "Usage" in update.message.replies[-1]["text"]


async def test_edit_rejects_a_non_uuid(wired):
    api_client, _ = wired
    update = FakeUpdate("/edit not-a-uuid")
    await transcript.edit_command(update, make_context(api_client, args=["not-a-uuid"]))

    assert "not a valid video id" in update.message.replies[-1]["text"]


async def test_edit_of_an_empty_transcript_is_refused(wired):
    api_client, backend = wired
    backend.videos[VIDEO_ID]["transcripts"][0]["content"] = "   "
    update = FakeUpdate("/edit " + VIDEO_ID)

    await transcript.edit_command(update, make_context(api_client, args=[VIDEO_ID]))

    assert backend.saved_transcripts == []
    assert "no transcript text" in update.message.replies[-1]["text"].lower()
