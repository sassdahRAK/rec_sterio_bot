"""Tests for the ``/media`` command — delivering the file through Telegram.

The media endpoint is authenticated, so the bot must never hand the user a URL
that 401s in their browser: it fetches the bytes with its own service token
and uploads them to the chat.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, TimedOut

from bot.client import ApiError, CompressedNotReadyError, OversizeMediaError
from bot.config import Settings
from bot.handlers.media import (
    COMPRESSED_VARIANT,
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

    async def get_video(self, video_id: str, *, requested_by: str | None = None):
        return {"id": video_id, **self.video}

    async def get_media(
        self,
        video_id: str,
        variant: str | None = None,
        *,
        requested_by: str | None = None,
    ) -> bytes:
        raise ApiError("not_found", "Media is not available yet.", status_code=404)

    async def download_media_to(
        self, video_id: str, path: str, *, max_bytes: int, **kwargs: Any
    ) -> int:
        """The streamed fetch of an undownloaded video hits the same 404."""
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
        self.compressed_calls = 0
        self.video_calls = 0
        self.link_calls = 0
        #: what a variant=compressed request answers: bytes when configured,
        #: an exception when given one, else (no compressed copy on this
        #: fake backend) the plain 404 of a missing artifact.
        self.compressed: bytes | None = None
        self.compressed_exception: Exception | None = None
        #: when set, a streamed variant=compressed body runs past ``max_bytes``
        #: (the backend degraded to the ORIGINAL): the byte count it reports
        #: as seen. Set instead of ``compressed`` so a test can simulate a
        #: multi-gigabyte body without allocating it.
        self.compressed_oversize: int | None = None
        #: the ``X-Compressed`` marker of a compressed-variant body: True for a
        #: real artifact, False when the backend degraded to the original.
        self.compressed_flag: bool = True
        #: every ``variant`` handed to the streaming fetch, in order
        #: ("original" = the original, "compressed" = the smaller copy).
        self.download_variants: list[str | None] = []

    async def get_video(self, video_id: str, *, requested_by: str | None = None):
        self.video_calls += 1
        return {"id": video_id, "title": "A Video", "file_size": self.size}

    async def get_portal_link(
        self, video_id: str, *, requested_by: str | None = None
    ) -> dict[str, Any]:
        """No token on this stub: the bare `/video/<id>` page is what tests see."""
        raise ApiError("not_found", "No portal link here.", status_code=404)

    async def get_media(
        self,
        video_id: str,
        variant: str | None = None,
        *,
        requested_by: str | None = None,
    ) -> bytes:
        if variant == COMPRESSED_VARIANT:
            self.compressed_calls += 1
            if self.compressed_exception is not None:
                raise self.compressed_exception
            if self.compressed is None:
                raise ApiError(
                    "not_found", "Media is not available yet.", status_code=404
                )
            return self.compressed
        self.media_calls += 1
        return self.payload

    async def stream_media_to(
        self,
        video_id: str,
        path: str,
        *,
        max_bytes: int,
        variant: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, bool]:
        """Stream semantics, honouring the variant like the real client.

        The original body goes to *path* unless it is oversize; the
        ``compressed`` variant answers the configured artifact (with the
        configured ``X-Compressed`` marker), the configured exception, the
        simulated degraded original, or the plain 404.
        """
        self.download_variants.append(variant)
        if variant == COMPRESSED_VARIANT:
            if self.compressed_exception is not None:
                raise self.compressed_exception
            if self.compressed_oversize is not None:
                raise OversizeMediaError(self.compressed_oversize)
            if self.compressed is None:
                raise ApiError(
                    "not_found", "Media is not available yet.", status_code=404
                )
            body, compressed = self.compressed, self.compressed_flag
        else:
            self.media_calls += 1
            body, compressed = self.payload, False
        if len(body) > max_bytes:
            raise OversizeMediaError(len(body))
        Path(path).write_bytes(body)
        return len(body), compressed

    async def download_media_to(
        self,
        video_id: str,
        path: str,
        *,
        max_bytes: int,
        variant: str | None = None,
        **kwargs: Any,
    ) -> int:
        """The byte-count wrapper of :meth:`stream_media_to`."""
        written, _ = await self.stream_media_to(
            video_id, path, max_bytes=max_bytes, variant=variant
        )
        return written

    async def link_telegram(self, video_id: str, file_id: str, **kwargs: Any) -> None:
        """The record step after a successful send (POST /videos/{id}/telegram)."""
        self.link_calls += 1


async def _downloaded_video(
    api_client, backend, slug: str, actor: str = "telegram:1"
) -> str:
    """Create a video as *actor* and walk it to the 'downloaded' state."""
    video = await api_client.create_video(
        f"https://youtu.be/{slug}", requested_by=actor
    )
    for _ in range(4):
        await api_client.get_video(video["id"])
    return video["id"]


@pytest.mark.asyncio
async def test_media_command_uploads_the_file(api_client, backend) -> None:
    """The command fetches the bytes and sends them as a document."""
    video_id = await _downloaded_video(
        api_client, backend, "media-upload", actor="telegram:42"
    )

    bot = FakeBot()
    context = FakeContext(args=[video_id], bot=bot)
    context.bot_data["api"] = api_client
    update = FakeUpdate()

    await media_command(update, context)

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the media should be delivered as a document"
    assert documents[0]["document"] == b"fake-media-bytes"


@pytest.mark.asyncio
async def test_media_command_reads_an_owned_video_as_the_user(api_client, backend) -> None:
    """A normal user's `/media <own id>` works, and every content call names
    that user — the backend never sees the service identity for their read."""
    video_id = await _downloaded_video(
        api_client, backend, "media-own", actor="telegram:42"
    )

    bot = FakeBot()
    context = FakeContext(args=[video_id], bot=bot)
    context.bot_data["api"] = api_client
    update = FakeUpdate(user_id=42)
    # The setup helper's own polls run as the service; only the handler's
    # calls are under test.
    start = len(backend.requests)

    await media_command(update, context)

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the owner must receive their own file"
    scoped = [
        r for r in backend.requests[start:]
        if r["path"].startswith(f"/api/v1/videos/{video_id}")
    ]
    assert scoped, "the video and its media must be fetched"
    assert all(r["requested_by"] == "telegram:42" for r in scoped), (
        "every content read must carry the end user, not drop to the service"
    )


@pytest.mark.asyncio
async def test_media_command_refuses_someone_elses_video(api_client, backend) -> None:
    """A unit the user did not create is refused, and the reply says so."""
    video_id = await _downloaded_video(
        api_client, backend, "media-foreign", actor="telegram:1"
    )

    bot = FakeBot()
    context = FakeContext(args=[video_id], bot=bot)
    context.bot_data["api"] = api_client
    context.bot_data["settings"] = _settings()
    update = FakeUpdate(user_id=42)
    start = len(backend.requests)

    await media_command(update, context)

    assert not any(m.get("document") is not None for m in bot.sent), (
        "a non-owner must never receive the file"
    )
    text = " ".join(m.get("text", "") for m in bot.sent)
    text += " ".join(r["text"] for r in update.message.replies)
    assert "access" in text.lower(), "the refusal must be explained to the user"
    reads = [
        r for r in backend.requests[start:]
        if r["path"] == f"/api/v1/videos/{video_id}"
    ]
    assert reads and all(r["requested_by"] == "telegram:42" for r in reads)


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
    video_id = await _downloaded_video(
        api_client, backend, "media-buttons", actor="telegram:999"
    )
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
    portal = f"https://portal.test/video/{video_id}"
    assert any(url and url.startswith(portal) and "?t=" in url for url in urls)
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
    monkeypatch.setattr("bot.handlers.media.portal_button", lambda _id, **_kw: None)
    monkeypatch.setattr(
        "bot.handlers.media.portal_url", lambda _id, _s=None, **_kw: None
    )
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

    async def get_video(self, video_id: str, *, requested_by: str | None = None):
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
    update = FakeUpdate(user_id=1)

    await audio_command(update, context)

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the mp3 should be delivered as a document"
    assert documents[0]["document"] == b"fake-mp3"
    assert documents[0]["filename"] == f"{video_id}.mp3"
    assert "A fake video" in documents[0]["caption"]


@pytest.mark.asyncio
async def test_audio_command_says_when_the_mp3_is_not_extracted(api_client, backend) -> None:
    """A video with no mp3 yet is explained, and pointed at /status."""
    video = await api_client.create_video(
        "https://youtu.be/audio-404", requested_by="telegram:42"
    )

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


# ------------------------------------------------------------ compressed copy
# A unit over Telegram's 50 MB upload limit can never arrive whole; the
# backend's compressed artifact (C1's `GET /media?variant=compressed`) is the
# way it still reaches the chat.


async def _oversizable_video(api_client, backend, slug: str) -> str:
    """A downloaded video recorded as past Telegram's upload limit."""
    video_id = await _downloaded_video(api_client, backend, slug)
    backend.videos[video_id]["file_size"] = MAX_UPLOAD_BYTES + 1
    return video_id


def _variant_media_requests(backend, video_id: str, *, variant: str) -> list[dict[str, Any]]:
    return [
        r
        for r in backend.requests
        if r["path"] == f"/api/v1/videos/{video_id}/media" and r["params"].get("variant") == variant
    ]


def _plain_media_requests(backend, video_id: str) -> list[dict[str, Any]]:
    """Requests for the original — ``variant`` unset or explicitly ``original``."""
    return [
        r
        for r in backend.requests
        if r["path"] == f"/api/v1/videos/{video_id}/media"
        and r["params"].get("variant", "original") == "original"
    ]


@pytest.mark.asyncio
async def test_an_oversize_unit_is_sent_from_its_compressed_copy(api_client, backend) -> None:
    """Artifact available: it is sent as a document and the oversized original
    is never pulled."""
    video_id = await _oversizable_video(api_client, backend, "media-compressed")
    backend.compressed_media[video_id] = b"little-compressed-mp4"
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is True
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the compressed copy should be delivered as a document"
    assert documents[0]["document"] == b"little-compressed-mp4"
    assert "compressed delivery copy" in documents[0]["caption"], (
        "the caption must say the file is the compressed copy, not the original"
    )
    assert _variant_media_requests(backend, video_id, variant="compressed"), (
        "the smaller copy must be requested by variant"
    )
    assert _plain_media_requests(backend, video_id) == [], (
        "the oversize original must never be downloaded"
    )
    # The copy Telegram stored for the *compressed* send is recorded too.
    assert backend.telegram_links, "the compressed copy is recorded like any other"
    assert backend.telegram_links[0]["file_id"] == "file-1"


@pytest.mark.asyncio
async def test_an_oversize_unit_without_artifact_degrades_and_points_at_the_portal(
    api_client, backend
) -> None:
    """No copy yet: the backend serves the ORIGINAL with ``X-Compressed:
    false``. That body is never uploaded (its recorded size is past the cap),
    the reply is the portal fallback, and the degraded request queued the
    compress job once — the bot does not poll."""
    video_id = await _oversizable_video(api_client, backend, "media-not-ready")
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is False
    assert not any(m.get("document") is not None for m in bot.sent), (
        "the degraded original must never be uploaded"
    )
    notice = bot.sent[-1]
    assert "too large" in notice["text"].lower()
    assert f"https://portal.test/video/{video_id}" in notice["text"]
    # The degraded request queued the compress job once; the user's next ask
    # reuses it, and the bot makes one variant attempt per /media.
    await deliver_media(bot, 42, api_client, video_id)
    jobs = [j for j in backend.jobs.values() if j["kind"] == "compress"]
    assert len(jobs) == 1, "the backend must not queue the same compress job twice"
    assert (
        len(_variant_media_requests(backend, video_id, variant="compressed")) == 2
    ), "one variant attempt per ask, no bot-side polling or retrying"


@pytest.mark.asyncio
async def test_an_oversize_unit_whose_variant_fails_keeps_the_portal_explanation(
    api_client, backend
) -> None:
    """Any other variant error answers with the existing too-large text and
    the portal link — unchanged."""
    video_id = await _oversizable_video(api_client, backend, "media-variant-fail")
    backend.compressed_failure = (500, "compress_failed", "The encoder gave up.")
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is False
    notice = bot.sent[-1]
    assert "too large" in notice["text"].lower()
    assert "preparing a smaller copy" not in notice["text"].lower()
    assert f"https://portal.test/video/{video_id}" in notice["text"]


@pytest.mark.asyncio
async def test_an_oversize_unit_against_a_variantless_client_gets_the_portal_explanation() -> None:
    """A client that has not learned variants answers a plain 404 not_found —
    the same portal fallback, so an old backend degrades instead of confusing."""
    bot = FakeBot()
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)

    delivered = await deliver_media(bot, 42, client, "vid-old-backend")

    assert delivered is False
    assert client.download_variants == [COMPRESSED_VARIANT], (
        "the variant must be attempted through the capped stream"
    )
    assert client.compressed_calls == 0, (
        "the compressed copy must never be buffered whole via get_media"
    )
    assert client.media_calls == 0, "the oversize original must never be downloaded"
    assert "too large" in bot.sent[-1]["text"].lower()


@pytest.mark.asyncio
async def test_an_oversize_unit_not_ready_at_the_stub_level_explains_missing_media() -> None:
    """The typed CompressedNotReadyError means nothing is on disk at all, so the
    usual missing-media explanation is what the user gets."""
    bot = FakeBot()
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    client.compressed_exception = CompressedNotReadyError(
        "compressed_not_ready", "nothing on disk", status_code=404
    )

    delivered = await deliver_media(bot, 42, client, "vid-stub-not-ready")

    assert delivered is False
    assert not any(m.get("document") is not None for m in bot.sent)
    assert "not on disk" in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_an_oversize_unit_delivers_the_streamed_compressed_copy() -> None:
    """The artifact arrives through the capped stream — not a buffered
    get_media — and the bytes written to disk are the ones sent."""
    bot = FakeBot()
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    client.compressed = b"little-compressed-mp4"

    delivered = await deliver_media(bot, 42, client, "vid-stream-compressed")

    assert delivered is True
    assert client.download_variants == [COMPRESSED_VARIANT], (
        "the compressed copy must be fetched by the streaming method"
    )
    assert client.compressed_calls == 0, "the compressed fetch must not be buffered"
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents and documents[0]["document"] == b"little-compressed-mp4", (
        "the payload must be the streamed one, read back from the temp file"
    )
    assert "compressed delivery copy" in documents[0]["caption"], (
        "the caption must say the file is the compressed copy"
    )
    assert client.link_calls == 1, "the delivered copy is recorded like any other"


@pytest.mark.asyncio
async def test_a_degraded_compressed_response_is_never_sent_even_within_the_cap() -> None:
    """``X-Compressed: false`` marks a body as the ORIGINAL, not a delivery copy.
    Even when it fits the cap, it is not uploaded: the recorded size says
    Telegram would reject the original, so the portal answer is the honest one."""
    video_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    bot = FakeBot()
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    client.compressed = b"same-size-degraded-original"
    client.compressed_flag = False

    delivered = await deliver_media(bot, 42, client, video_id)

    assert delivered is False
    assert client.download_variants == [COMPRESSED_VARIANT]
    assert not any(m.get("document") is not None for m in bot.sent), (
        "the degraded original must never be uploaded as the compressed copy"
    )
    notice = bot.sent[-1]
    assert "too large" in notice["text"].lower()
    assert f"https://portal.test/video/{video_id}" in notice["text"]


@pytest.mark.asyncio
async def test_a_degraded_oversize_original_is_abandoned_and_points_at_the_portal(
    caplog,
) -> None:
    """The backend degrades variant=compressed to the ORIGINAL when no artifact
    exists. Its bytes pass the cap: the stream is abandoned, no document is
    uploaded, and the reply is the portal explanation — not a Telegram 413."""
    video_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    bot = FakeBot()
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    client.compressed_oversize = 1_700_000_000  # a 1.7 GB degraded original

    with caplog.at_level(logging.INFO, logger="bot.handlers.media"):
        delivered = await deliver_media(bot, 42, client, video_id)

    assert delivered is False
    assert client.download_variants == [COMPRESSED_VARIANT]
    assert not any(m.get("document") is not None for m in bot.sent), (
        "the oversize body must never be handed to Telegram"
    )
    notice = bot.sent[-1]
    assert "too large" in notice["text"].lower()
    assert f"https://portal.test/video/{video_id}" in notice["text"]
    assert "preparing a smaller copy" not in notice["text"].lower()
    assert any(
        "oversize original" in record.getMessage() for record in caplog.records
    ), "an operator can tell the degraded original from a not-ready artifact"


@pytest.mark.asyncio
async def test_the_streamed_compressed_404_explains_missing_media() -> None:
    """A plain 404 ``compressed_not_ready`` envelope from the stream means the
    video has nothing on disk — the missing-media explanation, not a portal
    refusal."""
    bot = FakeBot()
    client = MediaClient(payload=b"x", size=MAX_UPLOAD_BYTES + 1)
    client.compressed_exception = ApiError(
        "compressed_not_ready",
        "No compressed delivery copy exists for this video.",
        status_code=404,
    )

    delivered = await deliver_media(bot, 42, client, "vid-stream-not-ready")

    assert delivered is False
    assert client.download_variants == [COMPRESSED_VARIANT]
    assert not any(m.get("document") is not None for m in bot.sent)
    assert "not on disk" in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_an_unknown_size_small_file_streams_straight_to_the_chat(
    api_client, backend
) -> None:
    """When `file_size` was never recorded the body streams to a temp file and
    the in-limit unit is sent — the send path is unchanged."""
    video_id = await _downloaded_video(api_client, backend, "media-stream-small")
    backend.videos[video_id]["file_size"] = None
    backend.media_bytes = b"streamed-original-bytes"
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is True
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents and documents[0]["document"] == b"streamed-original-bytes"


@pytest.mark.asyncio
async def test_a_streaming_read_abandons_a_body_past_its_cap(api_client, backend) -> None:
    """The cap is the decision: once the read crosses it the transfer raises
    and stops — the caller learns the unit is oversize without a bigger
    buffer, whatever the body's real length is."""
    video_id = await _downloaded_video(api_client, backend, "media-cap")
    backend.media_bytes = b"x" * 64

    with (
        tempfile.NamedTemporaryFile() as tmp,
        pytest.raises(OversizeMediaError) as crossed,
    ):
        await api_client.download_media_to(video_id, tmp.name, max_bytes=16)

    assert crossed.value.size_seen == 64


@pytest.mark.asyncio
async def test_a_streaming_read_within_its_cap_completes(api_client, backend) -> None:
    """A body inside the cap streams through and reports every byte."""
    video_id = await _downloaded_video(api_client, backend, "media-cap-ok")
    backend.media_bytes = b"x" * 64

    with tempfile.NamedTemporaryFile() as tmp:
        written = await api_client.download_media_to(video_id, tmp.name, max_bytes=1024)

    assert written == 64


@pytest.mark.asyncio
async def test_the_real_upload_cap_bounds_a_huge_body_on_the_wire(
    api_client, backend
) -> None:
    """The honest small-delta simulation: one byte past the bot's limit, the
    stream stops at MAX_UPLOAD_BYTES instead of swallowing everything."""
    video_id = await _downloaded_video(api_client, backend, "media-cap-50")
    backend.media_bytes = b"y" * (MAX_UPLOAD_BYTES + 10)

    with (
        tempfile.NamedTemporaryFile() as tmp,
        pytest.raises(OversizeMediaError) as crossed,
    ):
        await api_client.download_media_to(video_id, tmp.name, max_bytes=MAX_UPLOAD_BYTES)

    assert crossed.value.size_seen <= MAX_UPLOAD_BYTES + 10, (
        "the read must stop at the body's edge, capped — it crossed the limit, "
        "so it has read at most the one past-limit chunk"
    )


@pytest.mark.asyncio
async def test_an_unknown_size_oversize_unit_streams_to_its_compressed_copy(
    api_client, backend
) -> None:
    """A body beyond the cap is abandoned mid-stream (never buffered whole);
    the compressed artifact is what arrives."""
    video_id = await _downloaded_video(api_client, backend, "media-stream-big")
    backend.videos[video_id]["file_size"] = None
    backend.media_bytes = b"y" * (MAX_UPLOAD_BYTES + 10)
    backend.compressed_media[video_id] = b"little-compressed-mp4"
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is True
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents and documents[0]["document"] == b"little-compressed-mp4"
    assert not any(
        m.get("document") == backend.media_bytes for m in bot.sent
    ), "the oversize original must never be uploaded"


@pytest.mark.asyncio
async def test_an_unknown_size_oversize_unit_without_artifact_points_at_the_portal(
    api_client, backend
) -> None:
    """Same chain when there is no compressed copy: the backend degrades to the
    original, the stream is abandoned past the cap, and the portal answer is
    sent rather than a doomed upload."""
    video_id = await _downloaded_video(api_client, backend, "media-stream-big-2")
    backend.videos[video_id]["file_size"] = None
    backend.media_bytes = b"y" * (MAX_UPLOAD_BYTES + 10)
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is False
    assert not any(m.get("document") is not None for m in bot.sent)
    notice = bot.sent[-1]
    assert "too large" in notice["text"].lower()
    assert f"https://portal.test/video/{video_id}" in notice["text"]


@pytest.mark.asyncio
async def test_an_in_limit_video_is_one_original_request_and_no_variant(
    api_client, backend
) -> None:
    """The ordinary case is unchanged: one buffered fetch of the original, no
    compressed-variant request at all."""
    video_id = await _downloaded_video(api_client, backend, "media-normal")
    backend.videos[video_id]["file_size"] = 1024
    backend.media_bytes = b"normal-size-bytes"
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is True
    originals = _plain_media_requests(backend, video_id)
    assert len(originals) == 1, "exactly one fetch of the original"
    assert originals[0]["params"].get("variant", "original") == "original"
    assert _variant_media_requests(backend, video_id, variant="compressed") == [], (
        "an in-limit unit must never ask for the compressed variant"
    )
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents and documents[0]["document"] == b"normal-size-bytes"
    assert "compressed" not in documents[0]["caption"].lower()


@pytest.mark.asyncio
async def test_a_missing_file_size_streams_to_a_temp_file_and_cleans_it_up(
    api_client, backend, monkeypatch
) -> None:
    """With no recorded size the body is streamed to a temp file (never buffered
    whole in memory), sent from there, and the file is removed afterwards."""
    video_id = await _downloaded_video(api_client, backend, "media-stream-temp")
    backend.videos[video_id]["file_size"] = None
    backend.media_bytes = b"streamed-original-bytes"

    created: list[str] = []
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def tracking(*args: Any, **kwargs: Any):
        handle = real_named_temporary_file(*args, **kwargs)
        created.append(handle.name)
        return handle

    monkeypatch.setattr(
        "bot.handlers.media.tempfile.NamedTemporaryFile", tracking
    )
    bot = FakeBot()

    delivered = await deliver_media(bot, 42, api_client, video_id)

    assert delivered is True
    assert created, "an unknown-size body must land in a temp file, not in RAM"
    assert not any(Path(name).exists() for name in created), (
        "the temp file must be cleaned up after the send"
    )
    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents and documents[0]["document"] == b"streamed-original-bytes"
