"""One tap, one job: double-tapped inline buttons must not repeat the work.

Telegram sends a callback query for every tap, and PTB runs handlers
concurrently, so tapping "Media" twice used to fetch the same bytes from the
API and upload them to the same chat twice. `merge.on_callback` now claims the
action in `bot.inflight` first and releases it when done.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from bot.handlers import merge as merge_mod
from bot.inflight import InFlight, get_inflight
from tests.conftest import FakeContext, FakeUpdate


class _Query:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[dict] = []
        self.message = FakeUpdate().message

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append({"text": text, **kwargs})


class _CallbackUpdate(FakeUpdate):
    def __init__(self, data: str) -> None:
        super().__init__()
        self.callback_query = _Query(data)


def _ctx(client) -> FakeContext:
    return FakeContext(args=[], bot_data={"api": client})


# ------------------------------------------------------------------ the guard


@pytest.mark.asyncio
async def test_a_second_tap_while_the_first_runs_is_refused(api_client, backend):
    """The second callback answers "already working" and sends nothing."""
    video_id = str(uuid.uuid4())
    backend.videos[video_id] = {
        "id": video_id,
        "source_url": "https://example.com/v",
        "platform": "direct",
        "title": "t",
        "status": "downloaded",
        "progress": 100,
        "error": None,
        "file_size": 10,
        "duration_seconds": 5.0,
        "transcripts": [],
    }

    context = _ctx(api_client)
    guard = get_inflight(context)
    first = _CallbackUpdate(f"media:{video_id}")
    second = _CallbackUpdate(f"media:{video_id}")

    # Stand in for a first tap that is still uploading: take the key it would.
    key = f"media:{first.callback_query.message.chat_id}:{video_id}"
    assert await guard.claim(key) is True

    await merge_mod.on_callback(second, context)

    assert second.callback_query.answers, "the tap must always be answered"
    assert "Already working" in second.callback_query.answers[-1]["text"]
    # Nothing was fetched or uploaded by the refused tap.
    assert not [m for m in context.bot.sent if m.get("document") is not None]

    await guard.release(key)


@pytest.mark.asyncio
async def test_the_guard_is_released_so_a_later_tap_works(api_client):
    """A completed delivery must not block the next one.

    Uses the Audio button: the fake backend serves real mp3 bytes for it, so a
    delivery is observable. Release is what makes the second tap possible.
    """
    video = await api_client.create_video(
        "https://youtu.be/double-tap-audio", requested_by="telegram:1"
    )
    video_id = video["id"]
    for _ in range(4):
        await api_client.get_video(video_id)

    context = _ctx(api_client)
    for _ in range(2):
        update = _CallbackUpdate(f"audio:{video_id}")
        await merge_mod.on_callback(update, context)
        assert not any(
            "Already working" in (a.get("text") or "")
            for a in update.callback_query.answers
        ), "the guard leaked between taps"

    documents = [m for m in context.bot.sent if m.get("document") is not None]
    assert len(documents) == 2, "both sequential taps must deliver"


@pytest.mark.asyncio
async def test_the_guard_is_released_even_when_the_action_fails(api_client):
    """An error must not wedge the button forever."""
    context = _ctx(api_client)
    missing = str(uuid.uuid4())
    update = _CallbackUpdate(f"media:{missing}")
    await merge_mod.on_callback(update, context)

    guard = get_inflight(context)
    assert guard.busy(f"media:{update.callback_query.message.chat_id}:{missing}") is False


@pytest.mark.asyncio
async def test_the_guard_is_per_chat(api_client, backend):
    """Two users asking for the same video do not block each other."""
    video_id = str(uuid.uuid4())
    backend.videos[video_id] = {
        "id": video_id,
        "source_url": "https://example.com/v",
        "platform": "direct",
        "title": "t",
        "status": "downloaded",
        "progress": 100,
        "error": None,
        "file_size": 10,
        "duration_seconds": 5.0,
        "transcripts": [],
    }
    guard = InFlight()
    assert await guard.claim(f"media:1:{video_id}") is True
    assert await guard.claim(f"media:2:{video_id}") is True, (
        "a different chat is a different job"
    )
    assert await guard.claim(f"media:1:{video_id}") is False


@pytest.mark.asyncio
async def test_read_only_actions_are_not_guarded(api_client, backend):
    """`status:` is cheap; blocking a second tap would only annoy."""
    video_id = str(uuid.uuid4())
    backend.videos[video_id] = {
        "id": video_id,
        "source_url": "https://example.com/v",
        "platform": "direct",
        "title": "t",
        "status": "transcribed",
        "progress": 100,
        "error": None,
        "file_size": 10,
        "duration_seconds": 5.0,
        "transcripts": [],
    }
    assert "status" not in merge_mod.GUARDED_ACTIONS

    context = _ctx(api_client)
    for _ in range(2):
        update = _CallbackUpdate(f"status:{video_id}")
        await merge_mod.on_callback(update, context)
    assert not any(
        "Already working" in (a.get("text") or "")
        for a in update.callback_query.answers
    )
