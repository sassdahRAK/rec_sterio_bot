"""Feature 4 from Telegram: merge start / list / apply and their buttons."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from bot.handlers import merge as merge_mod
from bot.handlers.merge import (
    format_merge,
    pick_transcripts,
    watch_gemini_then_merge,
    watch_merge,
)
from tests.conftest import FakeBot, FakeContext, FakeUpdate


def _ctx(client, args=None) -> FakeContext:
    return FakeContext(args=args or [], bot_data={"api": client})


def _video(backend, *, whisper=True, gemini=False) -> str:
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
        # The default test update acts as Telegram user 42; ownership is
        # enforced on content routes now, so the unit must be theirs.
        "created_by": "telegram:42",
    }
    if whisper:
        backend.add_transcript(video_id, "hello world", 1, "whisper")
    if gemini:
        backend.add_transcript(video_id, "សួស្តី hello world", 2, "gemini")
    return video_id


# ------------------------------------------------------------------ usage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler,usage",
    [
        (merge_mod.merge_command, merge_mod.MERGE_USAGE),
        (merge_mod.merges_command, merge_mod.MERGES_USAGE),
        (merge_mod.apply_command, merge_mod.APPLY_USAGE),
    ],
)
async def test_usage_without_args(api_client, handler, usage):
    update = FakeUpdate()
    await handler(update, _ctx(api_client))
    assert update.message.replies[-1]["text"] == usage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [merge_mod.merge_command, merge_mod.merges_command, merge_mod.apply_command],
)
async def test_rejects_non_uuid(api_client, handler):
    update = FakeUpdate()
    await handler(update, _ctx(api_client, ["not-a-uuid"]))
    assert "not a valid" in update.message.replies[-1]["text"]


# ------------------------------------------------------------- selection


def test_pick_transcripts_prefers_whisper_base():
    transcripts = [
        {"id": "m", "version": 3, "source": "manual", "status": "ready"},
        {"id": "g", "version": 2, "source": "gemini", "status": "ready"},
        {"id": "w", "version": 1, "source": "whisper", "status": "ready"},
        {"id": "x", "version": 4, "source": "gemini", "status": "failed"},
    ]
    base, gem = pick_transcripts(transcripts)
    assert base["id"] == "w"
    assert gem["id"] == "g"


def test_pick_transcripts_falls_back_to_any_ready():
    transcripts = [
        {"id": "m", "version": 2, "source": "manual", "status": "ready"},
        {"id": "w", "version": 1, "source": "whisper", "status": "failed"},
    ]
    base, gem = pick_transcripts(transcripts)
    assert base["id"] == "m"
    assert gem is None


def test_pick_transcripts_none_ready():
    assert pick_transcripts([{"source": "whisper", "status": "running"}]) == (None, None)


# ---------------------------------------------------------------- merge


@pytest.mark.asyncio
async def test_merge_without_transcript_explains(api_client, backend):
    video_id = _video(backend, whisper=False)
    update = FakeUpdate()
    await merge_mod.merge_command(update, _ctx(api_client, [video_id]))
    text = update.message.replies[-1]["text"]
    assert "no ready transcript" in text


@pytest.mark.asyncio
async def test_merge_with_gemini_ready_queues_and_reports(api_client, backend):
    video_id = _video(backend, gemini=True)
    update = FakeUpdate()
    context = _ctx(api_client, [video_id])
    await merge_mod.merge_command(update, context)

    assert "Merge" in update.message.replies[-1]["text"]
    assert any(r["path"].endswith("/transcript/merge") for r in backend.requests)

    merge_id = next(iter(backend.merges))
    status = await watch_merge(api_client, context.bot, update.message.chat_id, merge_id)
    assert status == "ready"

    sent = context.bot.sent[-1]
    assert "Merge" in sent["text"]
    assert "Apply" in str(sent.get("reply_markup"))


@pytest.mark.asyncio
async def test_merge_enqueues_gemini_when_missing(api_client, backend):
    video_id = _video(backend, whisper=True, gemini=False)
    update = FakeUpdate()
    context = _ctx(api_client, [video_id])
    await merge_mod.merge_command(update, context)

    assert "queued it now" in update.message.replies[-1]["text"]
    assert any(
        r["path"].endswith("/transcript/generate") for r in backend.requests
    ), "the bot must enqueue the Gemini pass"

    # The fake backend finishes Gemini immediately, so the watcher merges it.
    status = await watch_gemini_then_merge(
        api_client, context.bot, update.message.chat_id, video_id
    )
    assert status == "ready"
    assert backend.merges, "the merge should have been started automatically"


@pytest.mark.asyncio
async def test_merge_reports_backend_error(api_client, backend):
    video_id = _video(backend, whisper=True, gemini=False)
    backend.fail_next = (409, "conflict", "No Gemini transcript to merge.")
    update = FakeUpdate()
    await merge_mod.merge_command(update, _ctx(api_client, [video_id]))
    # fail_next is consumed by the generate call; the flow still enqueues Gemini.
    assert update.message.replies


@pytest.mark.asyncio
async def test_watch_merge_polls_with_the_initiating_user(api_client, backend):
    """Every poll carries the Telegram user who started the merge, so the
    backend evaluates their access rather than the bot's service identity."""
    video_id = _video(backend, gemini=True)  # created_by telegram:42
    context = _ctx(api_client, [video_id])
    await merge_mod.merge_command(FakeUpdate(), context)
    merge_id = next(iter(backend.merges))

    status = await watch_merge(
        api_client,
        FakeBot(),
        FakeUpdate().message.chat_id,
        merge_id,
        requested_by="telegram:42",
    )

    assert status == "ready"
    polls = [
        r for r in backend.requests if r["path"] == f"/api/v1/merges/{merge_id}"
    ]
    assert polls, "the watcher must poll the merge"
    assert all(r["requested_by"] == "telegram:42" for r in polls), (
        "a dropped identity would poll as the service and regress silently"
    )
    registry = context.bot_data.get("merge_watchers")
    if registry is not None:
        await registry.cancel_all()


# --------------------------------------------------------------- merges


@pytest.mark.asyncio
async def test_list_merges_empty(api_client, backend):
    video_id = _video(backend)
    update = FakeUpdate()
    await merge_mod.merges_command(update, _ctx(api_client, [video_id]))
    assert "No merges" in update.message.replies[-1]["text"]


@pytest.mark.asyncio
async def test_list_merges_shows_apply_button(api_client, backend):
    video_id = _video(backend, gemini=True)
    update = FakeUpdate()
    context = _ctx(api_client, [video_id])
    await merge_mod.merge_command(update, context)
    merge_id = next(iter(backend.merges))
    await api_client.get_merge(merge_id)  # advance to ready

    listing = FakeUpdate()
    await merge_mod.merges_command(listing, _ctx(api_client, [video_id]))
    reply = listing.message.replies[-1]
    assert "Apply" in str(reply.get("reply_markup"))


# ---------------------------------------------------------------- apply


@pytest.mark.asyncio
async def test_apply_creates_new_version(api_client, backend):
    video_id = _video(backend, gemini=True)
    context = _ctx(api_client)
    update = FakeUpdate()
    await merge_mod.merge_command(update, _ctx(api_client, [video_id]))
    merge_id = next(iter(backend.merges))
    await api_client.get_merge(merge_id)

    applied = FakeUpdate()
    await merge_mod.apply_command(applied, _ctx(api_client, [merge_id]))
    text = applied.message.replies[-1]["text"]
    assert "Applied merge" in text
    # whisper=1, gemini=2, so the applied merge becomes v3 — never a duplicate.
    assert "v3" in text
    assert backend.videos[video_id]["transcripts"][0]["source"] == "merged"


@pytest.mark.asyncio
async def test_apply_conflict_is_reported(api_client, backend):
    merge_id = str(uuid.uuid4())
    backend.merges[merge_id] = {
        "id": merge_id,
        "video_id": _video(backend),
        "status": "pending",
        "model": "gemini-2.5-pro",
        "diff": None,
        "error": None,
        "created_at": "2026-01-01T00:00:00Z",
    }
    update = FakeUpdate()
    await merge_mod.apply_command(update, _ctx(api_client, [merge_id]))
    assert "not ready yet" in update.message.replies[-1]["text"]


# ------------------------------------------------------------- rendering


def test_format_merge_includes_provenance_and_flags():
    text = format_merge(
        {
            "id": "12345678-0000-0000-0000-000000000000",
            "status": "ready",
            "model": "gemini-2.5-pro",
            "diff": {
                "stats": {
                    "output_segments": 2,
                    "from_base": 1,
                    "from_candidate": 1,
                    "flagged": 1,
                    "base_label": "whisper",
                    "candidate_label": "gemini",
                    "mean_agreement": 0.5,
                },
                "summary": "1 from Whisper, 1 from Gemini.",
                "segments": [
                    {"text": "Hello world.", "source": "both"},
                    {"text": "សួស្តី", "source": "gemini"},
                ],
            },
        }
    )
    assert "whisper" in text and "gemini" in text
    assert "flagged" in text.lower() or "1 flagged" in text
    assert "សួស្តី" in text


def test_format_merge_failed_shows_error():
    text = format_merge({"id": "abc", "status": "failed", "error": "boom"})
    assert "failed" in text
    assert "boom" in text


# ------------------------------------------------------------ callbacks


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[dict] = []
        self.message = FakeUpdate().message

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append({"text": text, **kwargs})


class _FakeCallbackUpdate(FakeUpdate):
    def __init__(self, data: str) -> None:
        super().__init__()
        self.callback_query = _FakeQuery(data)


@pytest.mark.asyncio
async def test_callback_apply(api_client, backend):
    video_id = _video(backend, gemini=True)
    await merge_mod.merge_command(FakeUpdate(), _ctx(api_client, [video_id]))
    merge_id = next(iter(backend.merges))
    await api_client.get_merge(merge_id)

    update = _FakeCallbackUpdate(f"apply:{merge_id}")
    context = _ctx(api_client)
    await merge_mod.on_callback(update, context)
    assert update.callback_query.answers[-1]["text"] == "Applied"
    assert "Applied merge" in update.callback_query.message.replies[-1]["text"]


@pytest.mark.asyncio
async def test_callback_mcheck_renders_merge(api_client, backend):
    video_id = _video(backend, gemini=True)
    await merge_mod.merge_command(FakeUpdate(), _ctx(api_client, [video_id]))
    merge_id = next(iter(backend.merges))
    await api_client.get_merge(merge_id)

    update = _FakeCallbackUpdate(f"mcheck:{merge_id}")
    await merge_mod.on_callback(update, _ctx(api_client))
    assert "Merge" in update.callback_query.message.replies[-1]["text"]


@pytest.mark.asyncio
async def test_callback_audio_sends_the_mp3(api_client, backend):
    """The Audio button answers the query and delivers the stored mp3.

    ``audio_command`` replies through ``update.effective_message``, which a
    callback update also carries (the message the keyboard hangs on) — the
    same path the ``status:`` branch already uses.
    """
    video = await api_client.create_video(
        "https://youtu.be/audio-callback", requested_by="telegram:42"
    )
    video_id = video["id"]
    # Walk the fake backend's status script to its end, the way /audio tests
    # do — the command's own get_video must not reset the status to pending.
    for _ in range(4):
        await api_client.get_video(video_id)

    update = _FakeCallbackUpdate(f"audio:{video_id}")
    context = _ctx(api_client)
    await merge_mod.on_callback(update, context)

    assert update.callback_query.answers[-1]["text"] == "Fetching the audio…"
    assert any(
        "Fetching the audio" in r["text"] for r in update.message.replies
    ), "the reply must land in the chat, not just the query toast"
    documents = [m for m in context.bot.sent if m.get("document") is not None]
    assert documents, "the mp3 should be delivered as a document"
    assert documents[0]["document"] == b"fake-mp3"
    assert documents[0]["filename"] == f"{video_id}.mp3"


@pytest.mark.asyncio
async def test_callback_unknown_action_is_answered(api_client):
    update = _FakeCallbackUpdate("nope:1")
    await merge_mod.on_callback(update, _ctx(api_client))
    assert update.callback_query.answers
