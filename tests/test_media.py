"""Tests for the ``/media`` command — delivering the file through Telegram.

The media endpoint is authenticated, so the bot must never hand the user a URL
that 401s in their browser: it fetches the bytes with its own service token
and uploads them to the chat.
"""

from __future__ import annotations

from typing import Any

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, TimedOut

from bot.config import Settings
from bot.handlers.media import (
    MAX_REFETCH_BYTES,
    MAX_UPLOAD_BYTES,
    _explain_missing_media,
    _filename,
    audio_command,
    deliver_media,
    media_command,
)

from .conftest import FakeBot, FakeContext, FakeUpdate


def _settings(admin_ids="999"):
    return Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids=admin_ids,
    )


class MissingMediaClient:
    """A client whose media endpoint answers 404, like an undownloaded video."""

    def __init__(self, video: dict) -> None:
        self.video = video

    async def get_video(self, video_id: str):
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
        self.link_calls = 0

    async def get_video(self, video_id: str):
        self.video_calls += 1
        return {"id": video_id, "title": "A Video", "file_size": self.size}

    async def get_media(self, video_id: str) -> bytes:
        self.media_calls += 1
        return self.payload

    async def link_telegram(self, video_id: str, file_id: str, **kwargs: Any) -> None:
        """The record step after a successful send (POST /videos/{id}/telegram)."""
        self.link_calls += 1


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
async def test_deliver_media_passes_the_links_to_the_document(api_client, backend) -> None:
    """The caller's web-link keyboard rides along on the delivered file."""
    video_id = await _downloaded_video(api_client, backend, "media-links")

    bot = FakeBot()
    links = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛠 Go to admin", url=f"https://console.test/videos/{video_id}")]]
    )
    delivered = await deliver_media(bot, 42, api_client, video_id, reply_markup=links)

    assert delivered is True
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents and documents[0]["reply_markup"] is links


@pytest.mark.asyncio
async def test_media_command_document_carries_the_web_links(api_client, backend) -> None:
    """/media fetches the video dict, so the file arrives with the buttons."""
    video_id = await _downloaded_video(api_client, backend, "media-buttons")
    folder = backend.add_folder("Fieldwork")
    backend.videos[video_id]["folder_id"] = folder["id"]

    bot = FakeBot()
    context = FakeContext(args=[video_id], bot=bot)
    context.bot_data["api"] = api_client
    context.bot_data["settings"] = _settings()
    update = FakeUpdate(user_id=999)  # admin: portal + console buttons

    await media_command(update, context)

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the media should be delivered as a document"
    urls = [b.url for row in documents[0]["reply_markup"].inline_keyboard for b in row]
    assert f"https://portal.test/video/{video_id}" in urls
    assert f"https://console.test/videos/{video_id}" in urls


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
async def test_an_oversized_file_reply_links_to_the_portal_page() -> None:
    """The unit cannot arrive as a file, so the portal page is the way in."""
    video_id = "33333333-3333-3333-3333-333333333333"
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    bot = FakeBot()

    await deliver_media(bot, 42, client, video_id)

    notice = bot.sent[-1]
    button = notice["reply_markup"].inline_keyboard[0][0]
    assert button.text == "🌐 Go to portal"
    assert button.url == f"https://portal.test/video/{video_id}"
    assert "open it in the portal" in notice["text"].lower()
    assert button.url in notice["text"], "the address is in the text too"


@pytest.mark.asyncio
async def test_an_unfiled_unit_tells_the_user_to_sign_in() -> None:
    """An unfiled unit is visible only to its creator, so the portal link alone
    would land on "isn't available" — the reply must say to sign in."""
    video_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bot = FakeBot()

    await deliver_media(
        bot,
        42,
        MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1),
        video_id,
        video={
            "id": video_id,
            "folder_id": None,
            "status": "transcribed",
            "file_size": MAX_UPLOAD_BYTES + 1,
        },
    )

    text = bot.sent[-1]["text"].lower()
    assert "sign in" in text
    assert "not in a folder" in text


@pytest.mark.asyncio
async def test_a_filed_unit_does_not_mention_signing_in() -> None:
    video_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    bot = FakeBot()

    await deliver_media(
        bot,
        42,
        MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1),
        video_id,
        video={
            "id": video_id,
            "folder_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "file_size": MAX_UPLOAD_BYTES + 1,
        },
    )

    assert "sign in" not in bot.sent[-1]["text"].lower()


@pytest.mark.asyncio
async def test_an_oversized_file_keeps_the_callers_keyboard() -> None:
    """The rich keyboard (status/transcript buttons) rides along, not replaces."""
    video_id = "44444444-4444-4444-4444-444444444444"
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    bot = FakeBot()
    links = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Status", callback_data=f"status:{video_id}")]]
    )

    await deliver_media(bot, 42, client, video_id, reply_markup=links)

    assert bot.sent[-1]["reply_markup"] is links


@pytest.mark.asyncio
async def test_the_too_large_notice_does_not_promise_a_hidden_link(monkeypatch) -> None:
    """With a local portal URL the button is dropped, so the text must not
    tell the user to open a link that is not there."""
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="t",
        portal_frontend_url="http://localhost:5174",
        admin_frontend_url="http://localhost:5173",
    )
    monkeypatch.setattr("bot.handlers.media.portal_button", lambda _id: None)
    monkeypatch.setattr("bot.handlers.media.portal_url", lambda _id, _s=None: None)
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    bot = FakeBot()

    await deliver_media(bot, 42, client, "55555555-5555-5555-5555-555555555555")

    notice = bot.sent[-1]
    assert notice.get("reply_markup") is None
    assert "open it in the portal" not in notice["text"].lower()
    assert "admin" in notice["text"].lower()


@pytest.mark.asyncio
async def test_the_too_large_notice_spells_out_the_portal_address() -> None:
    """The link must survive a client that will not render the button, and it
    has to be copy-pasteable, so the address is in the text as well."""
    video_id = "99999999-9999-9999-9999-999999999999"
    bot = FakeBot()

    await deliver_media(
        bot, 42, MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1), video_id
    )

    text = bot.sent[-1]["text"]
    assert f"https://portal.test/video/{video_id}" in text


@pytest.mark.asyncio
async def test_a_size_telegram_rejects_gets_the_same_explanation() -> None:
    """`file_size` is not always recorded; Telegram's own 413 must not surface
    as a bare error string."""
    video_id = "66666666-6666-6666-6666-666666666666"

    class RejectingBot(FakeBot):
        async def send_document(self, chat_id, document=None, **kwargs):  # type: ignore[override]
            raise BadRequest("Request Entity Too Large: file is too large")

    bot = RejectingBot()
    client = MediaClient(payload=b"x", size=None)

    delivered = await deliver_media(bot, 42, client, video_id)

    assert delivered is False
    notice = bot.sent[-1]
    assert "too large" in notice["text"].lower()
    assert notice["reply_markup"].inline_keyboard[0][0].url.endswith(f"/video/{video_id}")


@pytest.mark.asyncio
async def test_a_timed_out_upload_offers_the_portal_instead_of_a_bare_error() -> None:
    """The measured case: a 13.5 MB clip takes 172 s to reach Telegram from this
    network, past the write budget. The unit is fine, so the reply must say what
    failed and link the portal page."""
    video_id = "77777777-7777-7777-7777-777777777777"

    class SlowBot(FakeBot):
        async def send_document(self, chat_id, document=None, **kwargs):  # type: ignore[override]
            raise TimedOut("Timed out")

    bot = SlowBot()
    client = MediaClient(payload=b"x" * 1000, size=13_470_066)

    delivered = await deliver_media(bot, 42, client, video_id)

    assert delivered is False
    notice = bot.sent[-1]
    assert "timed out" in notice["text"].lower()
    assert "video itself is fine" in notice["text"].lower()
    assert notice["reply_markup"].inline_keyboard[0][0].url.endswith(f"/video/{video_id}")


@pytest.mark.asyncio
async def test_any_other_upload_failure_also_links_the_portal() -> None:
    video_id = "88888888-8888-8888-8888-888888888888"

    class BrokenBot(FakeBot):
        async def send_document(self, chat_id, document=None, **kwargs):  # type: ignore[override]
            raise NetworkError("Connection reset by peer")

    bot = BrokenBot()

    await deliver_media(bot, 42, MediaClient(payload=b"x" * 1000), video_id)

    notice = bot.sent[-1]
    assert "telegram refused the upload" in notice["text"].lower()
    assert notice["reply_markup"].inline_keyboard[0][0].url.endswith(f"/video/{video_id}")


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


# ----------------------------------------------------------------- filename


def test_filename_does_not_stack_a_second_extension() -> None:
    """A title that already ends with the recorded extension keeps it as is."""
    video = {"title": "clip.mp4", "local_path": "media.mp4"}
    assert _filename(video, "vid-fn-1") == "clip.mp4"


def test_filename_uses_the_recorded_extension() -> None:
    """The basename's suffix is the real extension, not a hard-coded mp4."""
    video = {"title": "My clip", "local_path": "media.webm"}
    assert _filename(video, "vid-fn-2") == "My clip.webm"


def test_filename_falls_back_to_mp4_without_a_local_path() -> None:
    assert _filename({"title": "My clip"}, "vid-fn-3") == "My clip.mp4"


# -------------------------------------------------------- the Telegram copy


@pytest.mark.asyncio
async def test_deliver_media_records_the_telegram_copy(api_client, backend) -> None:
    """instruction.txt #4: a delivered file is linked back to the catalogue.

    The fake bot's send_document must hand back a document file_id, and the
    bot must POST it to /videos/{id}/telegram.
    """
    video_id = await _downloaded_video(api_client, backend, "media-record")

    bot = FakeBot()
    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is True
    link_posts = [
        r
        for r in backend.requests
        if r["method"] == "POST" and r["path"] == f"/api/v1/videos/{video_id}/telegram"
    ]
    assert link_posts, "the telegram copy must be recorded in the catalogue"
    link = backend.telegram_links[-1]
    assert link["file_id"] == "file-1", "the recorded file_id is the copy Telegram stored"
    assert link["message_id"] == 1


@pytest.mark.asyncio
async def test_deliver_media_hints_when_the_copy_cannot_be_refetched() -> None:
    """A file past Telegram's 20 MB `getFile` ceiling gets an honest caption."""
    client = MediaClient(payload=b"video-bytes", size=MAX_REFETCH_BYTES + 1)
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, client, "vid-hint")

    assert delivered is True
    assert client.link_calls == 1, "the copy is still recorded — re-sends work"
    documents = [m for m in bot.sent if m.get("document") is not None]
    caption = documents[0]["caption"]
    assert "re-send" in caption
    assert "cannot fetch it back (20 MB limit)" in caption


@pytest.mark.asyncio
async def test_deliver_media_resends_the_telegram_copy_when_disk_is_empty() -> None:
    """A 404 with a recorded Telegram copy re-sends it by file_id."""
    client = MissingMediaClient({"status": "downloaded", "telegram_file_id": "tg-copy-1"})
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, client, "vid-copy")

    assert delivered is True
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the recorded copy should be re-sent"
    assert documents[-1]["document"] == "tg-copy-1"
    assert "re-sent from the Telegram copy" in documents[-1]["caption"]


class FailingSendBot(FakeBot):
    """A bot whose send_document always fails, like a re-send Telegram rejects."""

    async def send_document(self, chat_id: int, document: Any = None, **kwargs: Any) -> None:
        raise RuntimeError("telegram refused the re-send")


@pytest.mark.asyncio
async def test_deliver_media_explains_when_the_resend_fails() -> None:
    """A failed re-send falls back to an explanation that names the copy."""
    client = MissingMediaClient({"status": "downloaded", "telegram_file_id": "tg-copy-2"})
    bot = FailingSendBot()

    delivered = await deliver_media(bot, 42, client, "vid-copy-2")

    assert delivered is False
    assert bot.sent, "the user must still be told something"
    body = bot.sent[0]["text"]
    assert "not on disk" in body
    assert "The Telegram copy exists but re-sending it failed." in body


# ------------------------------------------------------------------- /audio


class OfflineAudioClient:
    """A client whose audio endpoint cannot be reached, like a dead backend."""

    async def get_video(self, video_id: str):
        return {"id": video_id, "title": "A Video"}

    async def get_audio(self, video_id: str, **kwargs: Any) -> bytes:
        from bot.client import ApiNetworkError

        raise ApiNetworkError("connection refused")


@pytest.mark.asyncio
async def test_audio_command_sends_the_stored_mp3(api_client, backend) -> None:
    """/audio delivers the mp3 as a document, captioned with the video title."""
    video_id = await _downloaded_video(api_client, backend, "audio-upload")

    bot = FakeBot()
    context = FakeContext(args=[video_id], bot=bot)
    context.bot_data["api"] = api_client
    update = FakeUpdate()

    await audio_command(update, context)

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the mp3 should be delivered as a document"
    assert documents[0]["document"] == b"fake-mp3"
    assert documents[0]["filename"] == f"{video_id}.mp3"
    assert "A fake video" in documents[0]["caption"]


@pytest.mark.asyncio
async def test_audio_command_says_when_the_mp3_is_not_extracted(api_client, backend) -> None:
    """A video with no mp3 yet is explained, and pointed at /status."""
    video = await api_client.create_video("https://youtu.be/audio-404")

    bot = FakeBot()
    context = FakeContext(args=[video["id"]], bot=bot)
    context.bot_data["api"] = api_client
    update = FakeUpdate()

    await audio_command(update, context)

    assert not any(m.get("document") is not None for m in bot.sent)
    assert any(
        "has not been extracted" in r["text"] for r in update.message.replies
    ), "the 404 must be explained in terms of extraction, not a bare error code"


@pytest.mark.asyncio
async def test_audio_command_rejects_a_bad_id(api_client, backend) -> None:
    """A non-uuid argument is rejected before any request is made."""
    bot = FakeBot()
    context = FakeContext(args=["not-a-uuid"], bot=bot)
    context.bot_data["api"] = api_client
    update = FakeUpdate()

    await audio_command(update, context)

    assert bot.sent == [], "no upload should be attempted"
    assert backend.requests == [], "validation happens before any backend call"
    assert any("not a valid video id" in r["text"] for r in update.message.replies)


@pytest.mark.asyncio
async def test_audio_command_reports_a_network_error() -> None:
    """An unreachable backend surfaces the friendly network message, not a traceback."""
    bot = FakeBot()
    context = FakeContext(args=["11111111-1111-1111-1111-111111111111"], bot=bot)
    context.bot_data["api"] = OfflineAudioClient()
    update = FakeUpdate()

    await audio_command(update, context)

    assert not any(m.get("document") is not None for m in bot.sent)
    assert any("Could not reach" in r["text"] for r in update.message.replies)
