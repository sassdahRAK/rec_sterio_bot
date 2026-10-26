"""A user sends a file to the chat — ingest it without any platform download.

TikTok and YouTube refuse most server-side requests, so a link is not always
enough. A file the user already has must go through the same pipeline.
"""
from __future__ import annotations

import pytest

from bot.handlers.uploads import TELEGRAM_MAX_DOWNLOAD, handle_media_upload

from .conftest import FakeBot, FakeContext, FakeUpdate


class FakeTelegramFile:
    """Stands in for telegram.Video / Audio / Document."""

    def __init__(
        self, file_name: str = "clip.mp4", file_size: int = 4096, payload: bytes = b"x" * 4096
    ) -> None:
        self.file_name = file_name
        self.file_size = file_size
        self._payload = payload
        self.downloaded = False

    async def get_file(self) -> FakeTelegramFile:
        return self

    async def download_as_bytearray(self) -> bytearray:
        self.downloaded = True
        return bytearray(self._payload)


class MediaMessage(FakeUpdate):
    """An update whose message carries a video/audio/document attachment."""

    def __init__(self, *, video=None, audio=None, document=None, user_id: int = 7) -> None:
        super().__init__(user_id=user_id)
        self.message.video = video
        self.message.audio = audio
        self.message.document = document


@pytest.mark.asyncio
async def test_a_sent_video_is_uploaded_and_queued(api_client, backend):
    telegram_file = FakeTelegramFile()
    update = MediaMessage(video=telegram_file)
    ctx = FakeContext(bot_data={"api": api_client, "settings": None})

    await handle_media_upload(update, ctx)

    assert backend.uploads, "the file should have been POSTed to the backend"
    sent = backend.uploads[0]
    # The recorded size is the whole multipart body, so compare against a floor.
    assert sent["bytes"] > 4096
    assert sent["content_type"].startswith("multipart/form-data")
    assert sent["requested_by"] == "telegram:7", "attribution must survive the upload"
    assert telegram_file.downloaded, "the bot must fetch the bytes from Telegram"

    replies = " ".join(r["text"] for r in update.message.replies)
    assert "Uploaded" in replies


@pytest.mark.asyncio
async def test_a_sent_audio_file_is_accepted(api_client, backend):
    update = MediaMessage(audio=FakeTelegramFile("voice.mp3", 2048, b"a" * 2048))
    ctx = FakeContext(bot_data={"api": api_client, "settings": None})

    await handle_media_upload(update, ctx)

    assert backend.uploads and backend.uploads[0]["bytes"] > 2048


@pytest.mark.asyncio
async def test_a_document_without_a_media_suffix_is_rejected(api_client, backend):
    update = MediaMessage(document=FakeTelegramFile("report.pdf", 900))
    ctx = FakeContext(bot_data={"api": api_client, "settings": None})

    await handle_media_upload(update, ctx)

    assert backend.uploads == [], "an unsupported file must not reach the backend"
    replies = " ".join(r["text"] for r in update.message.replies)
    assert "not supported" in replies


@pytest.mark.asyncio
async def test_an_oversized_file_is_refused_before_download(api_client, backend):
    telegram_file = FakeTelegramFile(file_size=TELEGRAM_MAX_DOWNLOAD + 1)
    update = MediaMessage(video=telegram_file)
    ctx = FakeContext(bot_data={"api": api_client, "settings": None})

    await handle_media_upload(update, ctx)

    assert backend.uploads == []
    assert not telegram_file.downloaded, "an oversized file must not be fetched"
    replies = " ".join(r["text"] for r in update.message.replies)
    assert "Telegram" in replies


@pytest.mark.asyncio
async def test_a_backend_error_is_reported_to_the_user(api_client, backend):
    backend.fail_next = (500, "internal_error", "An internal error occurred.")
    update = MediaMessage(video=FakeTelegramFile())
    ctx = FakeContext(bot_data={"api": api_client, "settings": None})

    await handle_media_upload(update, ctx)  # must not raise

    replies = " ".join(r["text"] for r in update.message.replies)
    assert replies, "the user must be told the upload failed"
