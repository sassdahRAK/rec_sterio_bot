"""The per-user cookies + consent gate (TikTok/Facebook link downloads).

Flow: /cookies (private chat) → status + consent text → I agree → the user
sends cookies.txt as a document → their downloads ride their own session.
Any time: /cookies → Remove my data deletes the file and the record.
"""
from __future__ import annotations

import pytest
from telegram import Update
from telegram.ext import Application

from bot.config import Settings
from bot.handlers import collect, register_handlers
from bot.handlers import cookies as cookies_mod
from bot.handlers import merge as merge_mod
from bot.handlers import uploads as uploads_mod
from bot.handlers.cookies import (
    CONSENT_TEXT,
    CONSENT_VERSION,
    cookies_command,
    handle_cookies_document,
    on_callback,
)
from bot.handlers.links import WatchRegistry, handle_link
from tests.conftest import FakeContext, FakeUpdate, FakeUser

UID = 7
TIKTOK_URL = "https://www.tiktok.com/@chewbash/video/1"


def _settings(**overrides) -> Settings:
    base = {
        "telegram_bot_token": "t",
        "bot_api_token": "test-service-token",
        "bot_api_base_url": "http://testserver/api/v1",
        "admin_telegram_ids": "999",
        "poll_interval_seconds": 0.01,
        "poll_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return Settings(**base)


class FakeCallbackQuery:
    """Stands in for telegram.CallbackQuery (answer / edit_message_text)."""

    def __init__(self, data: str, user_id: int = UID) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append({"text": text, **kwargs})

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append({"text": text, **kwargs})


class CallbackUpdate(FakeUpdate):
    def __init__(self, data: str, user_id: int = UID) -> None:
        super().__init__(user_id=user_id)
        self.callback_query = FakeCallbackQuery(data, user_id)


class FakeCookiesDocument:
    """Stands in for telegram.Document carrying a cookies.txt."""

    def __init__(
        self,
        file_name: str = "cookies.txt",
        file_size: int = 512,
        payload: bytes = b"# Netscape HTTP Cookie File\n# a session for testing\n",
        mime_type: str = "text/plain",
    ) -> None:
        self.file_name = file_name
        self.file_size = file_size
        self.mime_type = mime_type
        self._payload = payload
        self.get_file_calls = 0
        self.downloaded = False

    async def get_file(self) -> FakeCookiesDocument:
        self.get_file_calls += 1
        return self

    async def download_as_bytearray(self) -> bytearray:
        self.downloaded = True
        return bytearray(self._payload)


class DocumentUpdate(FakeUpdate):
    def __init__(
        self,
        document: FakeCookiesDocument,
        user_id: int = UID,
        chat_type: str = "private",
    ) -> None:
        super().__init__(user_id=user_id, chat_type=chat_type)
        self.message.document = document


def _real_document_update(
    *,
    file_name: str,
    mime_type: str,
    chat_type: str = "private",
    user_id: int = UID,
):
    """A real telegram.Update carrying a document, for filter matching."""
    app = Application.builder().token("123456:unit-test").build()
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1710000000,
                "chat": {"id": user_id, "type": chat_type},
                "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                "document": {
                    "file_id": "file-1",
                    "file_unique_id": "uniq-1",
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "file_size": 512,
                },
            },
        },
        app.bot,
    )


def _first_matching_handler(app, update):
    """The handler PTB would run: the FIRST matching one in group 0."""
    for handler in app.handlers[0]:
        if handler.check_update(update):
            return handler
    raise AssertionError("no handler matched the update")


def _registered_app():
    app = Application.builder().token("123456:unit-test").build()
    register_handlers(app)
    return app


def _requests(backend, method: str, path: str) -> list[dict]:
    return [
        r for r in backend.requests if r["method"] == method and r["path"] == path
    ]


async def _consent(api_client, uid: int = UID) -> None:
    await api_client.create_consent(
        uid, CONSENT_VERSION, requested_by=f"telegram:{uid}"
    )


async def _upload_cookies(api_client, content: bytes = b"# cookies\n", uid: int = UID) -> None:
    await api_client.upload_cookies(uid, content, requested_by=f"telegram:{uid}")


# ------------------------------------------------------------ /cookies chat


async def test_group_chat_is_refused_without_a_backend_call(api_client, backend):
    update = FakeUpdate("/cookies", user_id=UID, chat_type="group")
    await cookies_command(update, FakeContext(bot_data={"api": api_client}))

    assert "message me privately" in update.message.replies[-1]["text"]
    assert backend.requests == [], "no status may leak into a group chat"


async def test_private_command_without_record_shows_consent_and_buttons(
    api_client, backend
):
    update = FakeUpdate("/cookies", user_id=UID)
    await cookies_command(update, FakeContext(bot_data={"api": api_client}))

    reply = update.message.replies[-1]
    assert CONSENT_TEXT in reply["text"]
    assert "not agreed" in reply["text"]
    markup = str(reply.get("reply_markup"))
    assert "cookies:agree" in markup
    assert "cookies:remove" in markup

    gets = _requests(backend, "GET", "/api/v1/bot-users/7")
    assert len(gets) == 1
    assert gets[0]["requested_by"] == "telegram:7"


async def test_private_command_with_record_reports_status(api_client, backend):
    backend.bot_users[UID] = {
        "telegram_id": UID,
        "consented": True,
        "consented_at": "2026-10-28T00:00:00Z",
        "consent_version": CONSENT_VERSION,
        "cookies_present": True,
        "cookies_updated_at": "2026-10-28T00:00:00Z",
    }
    update = FakeUpdate("/cookies", user_id=UID)
    await cookies_command(update, FakeContext(bot_data={"api": api_client}))

    text = update.message.replies[-1]["text"]
    assert "consent recorded" in text
    assert "cookies on file" in text


async def test_status_shows_the_file_state_before_the_consent_state(
    api_client, backend
):
    # Consented but no file: exactly the users who fell into the bug.
    backend.bot_users[UID] = {
        "telegram_id": UID,
        "consented": True,
        "consented_at": "2026-10-28T00:00:00Z",
        "consent_version": CONSENT_VERSION,
        "cookies_present": False,
        "cookies_updated_at": None,
    }
    update = FakeUpdate("/cookies", user_id=UID)
    await cookies_command(update, FakeContext(bot_data={"api": api_client}))

    first_line = update.message.replies[-1]["text"].split("\n", 1)[0]
    assert "No cookies file on record yet" in first_line
    assert "consent" not in first_line.lower()
    assert "consent recorded" in update.message.replies[-1]["text"]


async def test_status_shows_the_file_first_when_it_is_present(api_client, backend):
    backend.bot_users[UID] = {
        "telegram_id": UID,
        "consented": True,
        "consented_at": "2026-10-28T00:00:00Z",
        "consent_version": CONSENT_VERSION,
        "cookies_present": True,
        "cookies_updated_at": "2026-10-28T00:00:00Z",
    }
    update = FakeUpdate("/cookies", user_id=UID)
    await cookies_command(update, FakeContext(bot_data={"api": api_client}))

    first_line = update.message.replies[-1]["text"].split("\n", 1)[0]
    assert "cookies on file" in first_line
    assert "consent" not in first_line.lower()


# ------------------------------------------------------------------ buttons


async def test_agree_button_records_consent_with_version(api_client, backend):
    update = CallbackUpdate("cookies:agree")
    await on_callback(update, FakeContext(bot_data={"api": api_client}))

    assert update.callback_query.answers, "the button must be answered"
    consents = _requests(backend, "POST", "/api/v1/bot-users/7/consent")
    assert len(consents) == 1
    assert consents[0]["requested_by"] == "telegram:7"

    record = backend.bot_users[UID]
    assert record["consented"] is True
    assert record["consent_version"] == "2026-10-28.1"

    assert "cookies.txt" in update.callback_query.edits[-1]["text"]


async def test_agree_reply_says_consent_alone_is_not_enough(api_client, backend):
    update = CallbackUpdate("cookies:agree")
    ctx = FakeContext(bot_data={"api": api_client})
    await on_callback(update, ctx)

    text = update.callback_query.edits[-1]["text"]
    assert "not enough" in text, "the reply must not read as completion"
    assert "cookies.txt" in text, "the file step must be named"
    assert "refused" in text, "the consequence must be stated"
    assert "TikTok" in text and "Facebook" in text
    assert ctx.user_data.get("cookies_awaiting_file") is True


async def test_agree_button_maps_version_mismatch(api_client, backend):
    backend.fail_next = (
        422,
        "version_mismatch",
        "The consent text has changed. Run /cookies again.",
    )
    update = CallbackUpdate("cookies:agree")
    await on_callback(update, FakeContext(bot_data={"api": api_client}))

    assert "Run /cookies again" in update.callback_query.edits[-1]["text"]


async def test_agree_button_gets_mismatch_from_a_changed_backend_version(
    api_client, backend
):
    backend.consent_version = "2026-11-01.1"  # the backend serves a newer text
    update = CallbackUpdate("cookies:agree")
    await on_callback(update, FakeContext(bot_data={"api": api_client}))

    assert "Run /cookies again" in update.callback_query.edits[-1]["text"]
    assert backend.bot_users == {}, "a stale agreement must not be recorded"


async def test_remove_button_deletes_the_record(api_client, backend):
    await _consent(api_client)
    await _upload_cookies(api_client)
    assert backend.bot_users[UID]["cookies_present"] is True

    update = CallbackUpdate("cookies:remove")
    await on_callback(update, FakeContext(bot_data={"api": api_client}))

    deletes = _requests(backend, "DELETE", "/api/v1/bot-users/7")
    assert len(deletes) == 1
    assert deletes[0]["requested_by"] == "telegram:7"
    assert UID not in backend.bot_users
    assert "deleted" in update.callback_query.edits[-1]["text"]


# ------------------------------------------------------------- the document


async def test_a_cookies_document_is_stored_on_the_users_record(api_client, backend):
    await _consent(api_client)
    document = FakeCookiesDocument()
    update = DocumentUpdate(document)
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    assert backend.cookie_uploads, "the file should have been POSTed"
    sent = backend.cookie_uploads[0]
    assert sent["requested_by"] == "telegram:7", "attribution must survive"
    assert sent["content_type"].startswith("multipart/form-data")
    assert sent["bytes"] > len(document._payload)
    assert document.downloaded, "the bot must fetch the bytes from Telegram"

    assert backend.bot_users[UID]["cookies_present"] is True
    replies = " ".join(r["text"] for r in update.message.replies)
    assert "Cookies stored" in replies
    assert "file is on record" in replies
    assert "will now use your own session" not in replies, (
        "the upload does not record consent, so the copy must not claim it"
    )


async def test_an_oversized_document_is_refused_before_download(api_client, backend):
    await _consent(api_client)
    document = FakeCookiesDocument(file_size=2048)
    update = DocumentUpdate(document)
    ctx = FakeContext(
        bot_data={"api": api_client, "settings": _settings(cookies_max_bytes=1024)}
    )

    await handle_cookies_document(update, ctx)

    assert backend.cookie_uploads == []
    assert document.get_file_calls == 0, "the size check runs before the fetch"
    assert document.downloaded is False
    replies = " ".join(r["text"] for r in update.message.replies)
    assert "too big" in replies
    assert "1.0 KB" in replies


async def test_a_2xx_without_cookies_present_is_not_reported_as_success(
    api_client, backend
):
    backend.upload_cookies_present = False
    await _consent(api_client)
    document = FakeCookiesDocument()
    update = DocumentUpdate(document)
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    assert backend.cookie_uploads, "the file was still POSTed"
    replies = " ".join(r["text"] for r in update.message.replies)
    assert "Cookies stored" not in replies
    assert "no cookies file is on record yet" in replies
    assert "again" in replies, "the user must be told to retry"
    assert backend.bot_users[UID]["cookies_present"] is False


async def test_a_2xx_without_the_field_asks_the_server_before_claiming_success(
    api_client, backend
):
    # An older backend may omit cookies_present from the upload response; the
    # follow-up GET is the source of truth.
    await _consent(api_client)
    await _upload_cookies(api_client)
    assert backend.bot_users[UID]["cookies_present"] is True

    async def _silent_upload(*args, **kwargs):
        return {}

    api_client.upload_cookies = _silent_upload
    document = FakeCookiesDocument()
    update = DocumentUpdate(document)
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    replies = " ".join(r["text"] for r in update.message.replies)
    assert "Cookies stored" in replies
    gets = _requests(backend, "GET", "/api/v1/bot-users/7")
    assert gets, "the upload response was inconclusive, so a GET must confirm"


async def test_a_non_txt_document_in_the_cookies_flow_gets_a_specific_reply(
    api_client, backend
):
    await _consent(api_client)
    document = FakeCookiesDocument(
        file_name="notes.pdf", mime_type="application/pdf"
    )
    update = DocumentUpdate(document)
    ctx = FakeContext(
        bot_data={"api": api_client, "settings": _settings()},
        user_data={"cookies_awaiting_file": True},
    )

    await handle_cookies_document(update, ctx)

    replies = " ".join(r["text"] for r in update.message.replies)
    assert "not a cookies.txt" in replies
    assert "not supported" not in replies, "the generic reply must not win"
    assert backend.cookie_uploads == []
    assert document.get_file_calls == 0


@pytest.mark.parametrize(
    ("file_name", "mime_type"),
    [
        ("clip.mp4", "video/mp4"),  # mime says video
        ("clip.mp4", ""),  # no mime, extension still says video
        ("song.mp3", "audio/mpeg"),
    ],
)
async def test_media_sent_after_cookies_still_reaches_the_media_path(
    monkeypatch, api_client, backend, file_name, mime_type
):
    """The bug: a pending cookies flow must never swallow a video upload."""
    from bot.handlers import uploads as uploads_mod

    calls = []

    async def _spy(update, context):
        calls.append((update, context))

    monkeypatch.setattr(uploads_mod, "handle_media_upload", _spy)

    document = FakeCookiesDocument(file_name=file_name, mime_type=mime_type)
    update = DocumentUpdate(document)
    ctx = FakeContext(
        bot_data={"api": api_client, "settings": _settings()},
        user_data={"cookies_awaiting_file": True},
    )

    await handle_cookies_document(update, ctx)

    assert len(calls) == 1, "the media handler must be the one that ran"
    assert calls[0][0] is update and calls[0][1] is ctx
    assert backend.cookie_uploads == []
    assert document.get_file_calls == 0
    replies = " ".join(r["text"] for r in update.message.replies)
    assert "not a cookies.txt" not in replies
    assert "Receiving your cookies file" not in replies


async def test_a_group_document_gets_the_private_chat_hint(api_client, backend):
    document = FakeCookiesDocument()
    update = DocumentUpdate(document, chat_type="group")
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    replies = " ".join(r["text"] for r in update.message.replies)
    assert "/cookies" in replies
    assert "private" in replies
    assert backend.cookie_uploads == []
    assert document.get_file_calls == 0


async def test_a_plain_txt_outside_the_flow_goes_to_the_media_path(
    monkeypatch, api_client, backend
):
    from bot.handlers import uploads as uploads_mod

    calls = []

    async def _spy(update, context):
        calls.append((update, context))

    monkeypatch.setattr(uploads_mod, "handle_media_upload", _spy)

    document = FakeCookiesDocument(file_name="notes.txt")
    update = DocumentUpdate(document)
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    assert len(calls) == 1, "a text file outside the flow is not a cookies upload"
    assert backend.cookie_uploads == []


async def test_upload_413_says_the_file_is_too_large(api_client, backend):
    await _consent(api_client)
    backend.fail_next = (413, "payload_too_large", "File too big.")
    update = DocumentUpdate(FakeCookiesDocument())
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    replies = " ".join(r["text"] for r in update.message.replies)
    assert "too large" in replies


async def test_upload_422_says_wrong_format(api_client, backend):
    await _consent(api_client)
    backend.fail_next = (422, "validation_failed", "The cookies file is empty.")
    update = DocumentUpdate(FakeCookiesDocument())
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    replies = " ".join(r["text"] for r in update.message.replies)
    assert "wrong format" in replies


async def test_upload_network_failure_is_reported(api_client, backend):
    from bot.client import ApiNetworkError

    await _consent(api_client)

    async def _offline(*args, **kwargs):
        raise ApiNetworkError("connection refused")

    api_client.upload_cookies = _offline
    update = DocumentUpdate(FakeCookiesDocument())
    ctx = FakeContext(bot_data={"api": api_client, "settings": _settings()})

    await handle_cookies_document(update, ctx)

    replies = " ".join(r["text"] for r in update.message.replies)
    assert "network" in replies.lower()


# ------------------------------------------------------- the download gates


async def test_link_gate_blocks_tiktok_without_consent(api_client, backend):
    backend.enforce_consent_gate = True
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    await handle_link(update, FakeContext(bot_data={"api": api_client}))

    assert "/cookies" in update.message.replies[-1]["text"]
    # The fake records every incoming request, gate-rejected included — the
    # load-bearing assertion is that nothing was queued.
    assert not backend.videos, "no video may be created behind the gate"


async def test_link_gate_lets_a_consented_user_through(api_client, backend):
    backend.enforce_consent_gate = True
    await _consent(api_client)
    await _upload_cookies(api_client)

    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    ctx = FakeContext(
        bot_data={
            "api": api_client,
            "settings": _settings(),
            "watchers": WatchRegistry(),
        }
    )
    await handle_link(update, ctx)
    await ctx.bot_data["watchers"].cancel_all()

    assert "Queued for download" in update.message.replies[-1]["text"]
    assert len(backend.videos) == 1
    assert len(_requests(backend, "POST", "/api/v1/videos")) == 1


async def test_collect_url_step_maps_consent_required(api_client, backend):
    backend.fail_next = (
        403,
        "consent_required",
        "Run /cookies in the bot to allow downloads with your own session.",
    )
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    ctx = FakeContext(bot_data={"api": api_client}, user_data={})

    state = await collect.receive_url(update, ctx)

    assert state == collect.ASK_URL, "the wizard waits for /cookies, then the link"
    assert "/cookies" in update.message.replies[-1]["text"]


async def test_collect_url_step_hits_the_real_gate(api_client, backend):
    backend.enforce_consent_gate = True
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    ctx = FakeContext(bot_data={"api": api_client}, user_data={})

    state = await collect.receive_url(update, ctx)

    assert state == collect.ASK_URL
    assert "/cookies" in update.message.replies[-1]["text"]
    assert not backend.videos, "no video may be created behind the gate"


def _consent_error(consented: bool, cookies_present: bool) -> list[dict]:
    """The exact details shape the backend emits: a LIST of field entries."""
    return [
        {
            "field": "source_url",
            "issue": "consent_required",
            "consented": consented,
            "cookies_present": cookies_present,
        }
    ]


@pytest.mark.parametrize(
    ("details", "must_contain", "must_not_contain"),
    [
        (_consent_error(False, False), "I agree", "not enough"),
        (_consent_error(True, False), "not enough", None),
        # Backwards compatibility: a bare mapping instead of a list.
        ({"consented": False, "cookies_present": False}, "I agree", None),
        ({"consented": True, "cookies_present": True}, "cookie", None),
        # Shapes the parser must reject without raising.
        ("a backend message", "cookies.txt", None),
        (None, "cookies.txt", None),
    ],
)
def test_consent_hint_reads_every_details_shape(
    details, must_contain, must_not_contain
):
    text = cookies_mod.consent_required_hint(details)
    if must_contain == "cookie":
        assert "cookie" not in text.lower(), "do not invent a cookie cause"
        return
    assert must_contain in text
    if must_not_contain:
        assert must_not_contain not in text


async def test_link_hint_names_consent_when_the_backend_says_so(
    api_client, backend
):
    backend.fail_next = (
        403,
        "consent_required",
        "Cookies are needed.",
        _consent_error(False, False),
    )
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    await handle_link(update, FakeContext(bot_data={"api": api_client}))

    text = update.message.replies[-1]["text"]
    assert "/cookies" in text
    assert "I agree" in text
    assert "not enough" not in text, "the missing step is consent, not the file"


async def test_link_hint_names_the_file_when_consented_without_it(
    api_client, backend
):
    backend.fail_next = (
        403,
        "consent_required",
        "Cookies are needed.",
        _consent_error(True, False),
    )
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    await handle_link(update, FakeContext(bot_data={"api": api_client}))

    text = update.message.replies[-1]["text"]
    assert "not enough" in text, "consent alone must not read as sufficient"
    assert "cookies.txt" in text
    assert "document" in text


async def test_link_hint_without_details_falls_back_to_the_generic_wording(
    api_client, backend
):
    backend.fail_next = (403, "consent_required", "Cookies are needed.")
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    await handle_link(update, FakeContext(bot_data={"api": api_client}))

    text = update.message.replies[-1]["text"]
    assert "/cookies" in text
    assert "I agree" in text, "the fallback keeps both steps"
    assert "cookies.txt" in text


async def test_link_hint_says_nothing_about_cookies_when_both_are_true(
    api_client, backend
):
    backend.fail_next = (
        403,
        "consent_required",
        "Refused for another reason.",
        _consent_error(True, True),
    )
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    await handle_link(update, FakeContext(bot_data={"api": api_client}))

    text = update.message.replies[-1]["text"]
    assert "cookie" not in text.lower(), "do not invent a cookie cause"


async def test_collect_url_step_reads_the_details_too(api_client, backend):
    backend.fail_next = (
        403,
        "consent_required",
        "Cookies are needed.",
        _consent_error(True, False),
    )
    update = FakeUpdate(TIKTOK_URL, user_id=UID)
    ctx = FakeContext(bot_data={"api": api_client}, user_data={})

    state = await collect.receive_url(update, ctx)

    assert state == collect.ASK_URL
    assert "not enough" in update.message.replies[-1]["text"]


# ---------------------------------------------------------- PTB registration


def test_registration_dispatches_documents_to_the_cookies_handler_first():
    app = _registered_app()
    group0 = app.handlers[0]

    def index_of(callback) -> int:
        for position, handler in enumerate(group0):
            if getattr(handler, "callback", None) is callback:
                return position
        raise AssertionError(f"{callback} was not registered")

    # A group runs only the FIRST matching handler: the cookies callback must
    # be reachable before merge's bare (matches-everything) CallbackQueryHandler.
    assert index_of(cookies_mod.on_callback) < index_of(merge_mod.on_callback)
    assert (
        index_of(cookies_mod.handle_cookies_document)
        < index_of(uploads_mod.handle_media_upload)
    )

    # Real filters, real Update objects: PTB would run the cookies handler for
    # every document, including group ones and media it must hand back.
    for update in (
        _real_document_update(file_name="cookies.txt", mime_type="text/plain"),
        _real_document_update(file_name="clip.mp4", mime_type="video/mp4"),
        _real_document_update(
            file_name="cookies.txt", mime_type="text/plain", chat_type="group"
        ),
    ):
        handler = _first_matching_handler(app, update)
        assert handler.callback is cookies_mod.handle_cookies_document

    assert any(
        getattr(h, "callback", None) is cookies_mod.cookies_command for h in group0
    )