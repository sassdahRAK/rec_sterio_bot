"""A user sends a file to the chat — ingest it without any platform download.

TikTok and YouTube refuse most server-side requests, so a link is not always
enough. A file the user already has must go through the same pipeline.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.config import Settings
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
async def test_a_direct_upload_is_not_echoed_back(api_client, backend):
    """The upload watcher stays silent: no echoed file, no "download" wording.

    The user already has the file they sent, so the watch says nothing until
    the terminal "Transcript ready" notification.
    """
    bot = FakeBot()
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=5.0,
    )
    ctx = FakeContext(bot_data={"api": api_client, "settings": settings}, bot=bot)
    update = MediaMessage(video=FakeTelegramFile())

    await handle_media_upload(update, ctx)

    assert backend.uploads, "the file should have reached the backend"
    video_id = next(iter(backend.videos))
    watchers = ctx.bot_data["watchers"]

    # Let the background watch run to its terminal status.
    for _ in range(500):
        if not watchers.is_watching(video_id):
            break
        await asyncio.sleep(0.01)
    assert not watchers.is_watching(video_id), "the watch should settle"

    # No echo: the file is never sent back to the chat.
    assert not any(m.get("document") is not None for m in bot.sent)
    # And no "download"-worded notice — nothing was downloaded.
    texts = [m["text"] for m in bot.sent]
    assert not any("download" in t.lower() for t in texts)
    # The terminal notification is the one message a direct upload waits for.
    assert any("Transcript ready" in t for t in texts)


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
async def test_upload_reply_carries_the_console_link_for_an_admin(api_client, backend):
    bot = FakeBot()
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=5.0,
    )
    ctx = FakeContext(bot_data={"api": api_client, "settings": settings}, bot=bot)
    update = MediaMessage(video=FakeTelegramFile(), user_id=999)  # admin

    await handle_media_upload(update, ctx)

    video_id = next(iter(backend.videos))
    edits = update.message.edits
    markup = edits[-1]["reply_markup"]  # the "✅ Uploaded" notice
    urls = [b.url for row in markup.inline_keyboard for b in row]
    # A fresh upload is unfiled, so an admin sees the console button only.
    assert f"https://console.test/videos/{video_id}" in urls
    assert "Status" in [b.text for row in markup.inline_keyboard for b in row]
    await ctx.bot_data["watchers"].cancel_all()


@pytest.mark.asyncio
async def test_a_backend_error_is_reported_to_the_user(api_client, backend):
    backend.fail_next = (500, "internal_error", "An internal error occurred.")
    update = MediaMessage(video=FakeTelegramFile())
    ctx = FakeContext(bot_data={"api": api_client, "settings": None})

    await handle_media_upload(update, ctx)  # must not raise

    replies = " ".join(r["text"] for r in update.message.replies)
    assert replies, "the user must be told the upload failed"
