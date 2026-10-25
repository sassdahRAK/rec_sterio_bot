"""Link message -> video creation, and the background watcher."""

from __future__ import annotations

import asyncio

from bot.config import Settings
from bot.handlers.links import WatchRegistry, handle_link, watch_video
from tests.conftest import FakeBot, FakeContext, FakeUpdate


def _context(client, settings=None, bot=None):
    settings = settings or Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=5.0,
    )
    bot = bot or FakeBot()
    ctx = FakeContext(
        bot_data={"api": client, "settings": settings, "watchers": WatchRegistry()}, bot=bot
    )
    return ctx, bot


async def test_link_message_creates_video_and_replies_with_id(api_client, backend):
    update = FakeUpdate(text="https://www.youtube.com/watch?v=abc123", user_id=42)
    ctx, _ = _context(api_client)

    await handle_link(update, ctx)

    assert len(update.message.replies) == 1
    reply = update.message.replies[0]["text"]
    assert "Queued for download" in reply
    assert "video id" in reply

    # the id in the reply is the id of the video the backend created
    assert len(backend.videos) == 1
    video_id = next(iter(backend.videos))
    assert video_id in reply

    # the request carried the right actor header and the right body
    creates = [
        r for r in backend.requests if r["method"] == "POST" and r["path"] == "/api/v1/videos"
    ]
    assert len(creates) == 1
    assert creates[0]["requested_by"] == "telegram:42"
    assert creates[0]["authorization"] == "Bearer test-service-token"

    await ctx.bot_data["watchers"].cancel_all()


async def test_non_link_message_gets_a_helpful_reply(api_client, backend):
    update = FakeUpdate(text="hello there, no link")
    ctx, _ = _context(api_client)

    await handle_link(update, ctx)

    assert "did not find a link" in update.message.replies[0]["text"]
    assert not backend.videos


async def test_watch_notifies_when_download_completes(api_client, backend):
    bot = FakeBot()
    video = await api_client.create_video(
        "https://youtu.be/xyz", requested_by="telegram:7"
    )
    video_id = video["id"]

    status = await watch_video(
        api_client,
        bot,
        42,
        video_id,
        interval=0.0,
        timeout=5.0,
    )

    assert status in {"downloaded", "transcribed"}
    texts = [m["text"] for m in bot.sent]
    assert any("Download complete" in t for t in texts)
    # The media endpoint is authenticated, so the message must point at the
    # /media command (which uploads the file) rather than a raw API URL that
    # would return 401 in the user's browser.
    assert any("/media" in t for t in texts)
    assert not any("/api/v1/videos/" in t for t in texts)
    assert all(m["chat_id"] == 42 for m in bot.sent)


async def test_watch_ends_on_transcribed_and_reports_transcript(api_client, backend):
    bot = FakeBot()
    video = await api_client.create_video("https://youtu.be/xyz", requested_by="telegram:7")
    video_id = video["id"]

    status = await watch_video(
        api_client, bot, 42, video_id, interval=0.0, timeout=5.0
    )

    assert status == "transcribed"
    assert any("Transcript ready" in m["text"] for m in bot.sent)


async def test_watch_reports_failure(api_client, backend):
    backend.status_script = ["pending", "failed"]
    bot = FakeBot()
    video = await api_client.create_video("https://youtu.be/xyz", requested_by="telegram:7")

    status = await watch_video(
        api_client, bot, 42, video["id"], interval=0.0, timeout=5.0
    )

    assert status == "failed"
    assert any("Processing failed" in m["text"] for m in bot.sent)


async def test_registry_does_not_poll_the_same_video_twice():
    registry = WatchRegistry()
    started = asyncio.Event()

    async def never_ending() -> None:
        started.set()
        await asyncio.sleep(30)

    assert registry.start("vid-1", never_ending()) is True
    await started.wait()
    assert registry.is_watching("vid-1")
    duplicate = never_ending()
    assert registry.start("vid-1", duplicate) is False
    duplicate.close()
    await registry.cancel_all()
    assert not registry.is_watching("vid-1")
