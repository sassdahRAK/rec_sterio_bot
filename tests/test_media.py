"""Tests for the ``/media`` command — delivering the file through Telegram.

The media endpoint is authenticated, so the bot must never hand the user a URL
that 401s in their browser: it fetches the bytes with its own service token
and uploads them to the chat.
"""

from __future__ import annotations

import pytest

from bot.handlers.media import (
    MAX_UPLOAD_BYTES,
    _explain_missing_media,
    deliver_media,
    media_command,
)

from .conftest import FakeBot, FakeContext, FakeUpdate


class MissingMediaClient:
    """A client whose media endpoint answers 404, like an undownloaded video."""

    def __init__(self, video: dict) -> None:
        self.video = video

    async def get_video(self, video_id: str):  # noqa: ANN202
        return {"id": video_id, **self.video}

    async def get_media(self, video_id: str) -> bytes:
        from bot.client import ApiError

        raise ApiError("not_found", "Media is not available yet.", status_code=404)


def test_explain_missing_media_reports_a_failed_download_with_reason():
    """A failed download must not be reported as 'not available yet'."""
    text = _explain_missing_media(
        {
            "status": "failed",
            "error": "ERROR: [TikTok] 123: Unexpected response from webpage request",
        },
        "vid-1",
    )
    assert "failed" in text.lower()
    assert "TikTok" in text, "the reason should be surfaced"
    assert "not available yet" not in text.lower()


def test_explain_missing_media_reports_progress_while_downloading():
    text = _explain_missing_media({"status": "downloading", "progress": 42}, "vid-2")
    assert "in progress" in text
    assert "42%" in text


@pytest.mark.asyncio
async def test_deliver_media_404_explains_the_failure_instead_of_a_vague_message():
    bot = FakeBot()
    client = MissingMediaClient(
        {"status": "failed", "error": "ERROR: [TikTok] 9: Unexpected response"}
    )

    delivered = await deliver_media(bot, 42, client, "vid-3")

    assert delivered is False
    assert bot.sent, "the user must be told something"
    body = bot.sent[0]["text"]
    assert "TikTok" in body
    assert "not available yet" not in body.lower()


class MediaClient:
    """Stub client that mimics the API client's media behaviour."""

    def __init__(self, payload: bytes = b"video-bytes", size: int | None = None) -> None:
        self.payload = payload
        self.size = size if size is not None else len(payload)
        self.media_calls = 0
        self.video_calls = 0

    async def get_video(self, video_id: str):  # noqa: ANN202
        self.video_calls += 1
        return {"id": video_id, "title": "A Video", "file_size": self.size}

    async def get_media(self, video_id: str) -> bytes:
        self.media_calls += 1
        return self.payload


async def _downloaded_video(api_client, backend, slug: str) -> str:
    """Create a video and walk it to the 'downloaded' state."""
    video = await api_client.create_video(
        f"https://youtu.be/{slug}", requested_by="telegram:1"
    )
    for _ in range(4):
        await api_client.get_video(video["id"])
    return video["id"]


@pytest.mark.asyncio
async def test_media_command_uploads_the_file(api_client, backend) -> None:
    """The command fetches the bytes and sends them as a document."""
    video_id = await _downloaded_video(api_client, backend, "media-upload")

    bot = FakeBot()
    context = FakeContext(args=[video_id], bot=bot)
    context.bot_data["api"] = api_client
    update = FakeUpdate()

    await media_command(update, context)

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the media should be delivered as a document"
    assert documents[0]["document"] == b"fake-media-bytes"


@pytest.mark.asyncio
async def test_deliver_media_uses_the_service_token(api_client, backend) -> None:
    """Media is fetched through the authenticated API, not a bare URL."""
    video_id = await _downloaded_video(api_client, backend, "media-token")

    bot = FakeBot()
    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is True
    assert any(m.get("document") is not None for m in bot.sent)


@pytest.mark.asyncio
async def test_deliver_media_rejects_oversized_files() -> None:
    """A file above Telegram's limit is not uploaded; the user is told why."""
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, client, "abc")

    assert delivered is False
    assert client.media_calls == 0, "an oversized file must not be downloaded"
    assert any("too large" in m["text"] for m in bot.sent)


@pytest.mark.asyncio
async def test_media_command_rejects_a_bad_id(api_client) -> None:
    """A non-uuid argument is rejected before any request is made."""
    bot = FakeBot()
    context = FakeContext(args=["not-a-uuid"], bot=bot)
    context.bot_data["api"] = api_client
    update = FakeUpdate()

    await media_command(update, context)

    assert bot.sent == [], "no upload should be attempted"
    assert any("not a valid video id" in r["text"] for r in update.message.replies)


@pytest.mark.asyncio
async def test_media_command_reports_a_missing_video(api_client) -> None:
    """A 404 from the backend is reported to the user, not raised."""
    bot = FakeBot()
    context = FakeContext(
        args=["11111111-1111-1111-1111-111111111111"], bot=bot
    )
    context.bot_data["api"] = api_client
    update = FakeUpdate()

    await media_command(update, context)

    assert bot.sent, "the user must be told something happened"
    assert not any(m.get("document") is not None for m in bot.sent)
