"""Link message -> video creation, and the background watcher."""

from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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
    assert "Video id" in reply

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
    # The auto-delivered file also lands in bot.sent; only its notice is text.
    texts = [m["text"] for m in bot.sent if "text" in m]
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
    texts = [m["text"] for m in bot.sent if "text" in m]
    assert any("Transcript ready" in t for t in texts)


async def test_watch_reports_failure(api_client, backend):
    backend.status_script = ["pending", "failed"]
    bot = FakeBot()
    video = await api_client.create_video("https://youtu.be/xyz", requested_by="telegram:7")

    status = await watch_video(
        api_client, bot, 42, video["id"], interval=0.0, timeout=5.0
    )

    assert status == "failed"
    assert any("Processing failed" in m["text"] for m in bot.sent)


async def test_watch_auto_delivers_the_media_at_downloaded(api_client, backend):
    """instruction.txt #2: the finished file is sent to the chat automatically."""
    bot = FakeBot()
    video = await api_client.create_video("https://youtu.be/xyz", requested_by="telegram:7")
    video_id = video["id"]

    status = await watch_video(api_client, bot, 42, video_id, interval=0.0, timeout=5.0)

    assert status == "transcribed"
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the media should be delivered without being asked"
    assert documents[0]["document"] == b"fake-media-bytes"
    assert documents[0]["chat_id"] == 42
    # The completion notice still arrives alongside the file itself.
    assert any("Download complete" in m["text"] for m in bot.sent)


async def test_queued_reply_carries_the_console_link_for_an_admin(api_client, backend):
    update = FakeUpdate(text="https://youtu.be/admin-link", user_id=999)  # admin
    ctx, _ = _context(api_client)

    await handle_link(update, ctx)

    markup = update.message.replies[0]["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    texts = [b.text for b in buttons]
    # A fresh link-created video is unfiled, so the admin sees the console
    # button only — and the video_keyboard buttons survive the merge.
    video_id = next(iter(backend.videos))
    assert "🛠 Go to admin" in texts
    assert f"https://console.test/videos/{video_id}" in [b.url for b in buttons]
    assert "Status" in texts
    await ctx.bot_data["watchers"].cancel_all()


async def test_queued_reply_carries_the_portal_link_for_a_filed_video(api_client, backend):
    """A duplicate URL returns the *existing* video — one that can be filed."""
    video = await api_client.create_video("https://youtu.be/filed", requested_by="telegram:7")
    folder = backend.add_folder("Fieldwork")
    backend.videos[video["id"]]["folder_id"] = folder["id"]

    update = FakeUpdate(text="https://youtu.be/filed", user_id=42)
    ctx, _ = _context(api_client)

    await handle_link(update, ctx)

    markup = update.message.replies[0]["reply_markup"]
    urls = [b.url for row in markup.inline_keyboard for b in row]
    # A video reply links to the unit's own portal page, folder or not; the
    # backend-minted ?t= token makes an unfiled unit reachable without login.
    portal = f"https://portal.test/video/{video['id']}"
    assert any(url and url.startswith(portal) and "?t=" in url for url in urls)
    # user 42 is not an admin, so no console button anywhere.
    assert all("/videos/" not in (url or "") for url in urls)
    await ctx.bot_data["watchers"].cancel_all()


async def test_watch_notifies_with_the_threaded_links(api_client, backend):
    """The reply_markup threaded into the watch lands on every notification."""
    links = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🌐 Go to portal", url="https://portal.test/video/x")]]
    )
    bot = FakeBot()
    video = await api_client.create_video("https://youtu.be/threaded", requested_by="telegram:7")

    status = await watch_video(
        api_client, bot, 42, video["id"], interval=0.0, timeout=5.0, reply_markup=links
    )

    assert status == "transcribed"
    ready = [m for m in bot.sent if "Transcript ready" in m.get("text", "")]
    assert ready, "the watch must reach its terminal notification"
    assert ready[0]["reply_markup"] is links
    downloaded = [m for m in bot.sent if "Download complete" in m.get("text", "")]
    assert downloaded and downloaded[0]["reply_markup"] is links
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents and documents[0]["reply_markup"] is links


class BrokenMediaWatchClient:
    """A client whose media endpoint 404s while the video itself settles."""

    def __init__(self) -> None:
        self._statuses = iter(["downloaded", "transcribed"])

    async def get_video(self, video_id: str, *, requested_by: str | None = None):
        return {
            "id": video_id,
            "status": next(self._statuses, "transcribed"),
            "transcripts": [],
        }

    async def get_media(
        self,
        video_id: str,
        variant: str | None = None,
        *,
        requested_by: str | None = None,
    ) -> bytes:
        from bot.client import ApiError

        raise ApiError("not_found", "Media is not available yet.", status_code=404)

    async def download_media_to(
        self, video_id: str, path: str, *, max_bytes: int, **kwargs
    ) -> int:
        """The streamed fetch of an undownloaded video hits the same 404."""
        from bot.client import ApiError

        raise ApiError("not_found", "Media is not available yet.", status_code=404)


async def test_watch_survives_a_delivery_failure():
    """A failed auto-delivery explains itself and never stops the polling."""
    bot = FakeBot()
    client = BrokenMediaWatchClient()

    status = await watch_video(
        client, bot, 42, "11111111-1111-1111-1111-111111111111", interval=0.0, timeout=5.0
    )

    assert status == "transcribed", "the watcher must keep polling to the end"
    texts = [m["text"] for m in bot.sent]
    assert any("Download complete" in t for t in texts)
    assert any("not on disk" in t for t in texts), "the user learns why no file arrived"
    assert any("Transcript ready" in t for t in texts), "polling continued past the failure"
    assert not any(m.get("document") is not None for m in bot.sent)


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
