"""The per-user cookies + consent gate (TikTok/Facebook link downloads).

Flow: /cookies (private chat) → status + consent text → I agree → the user
sends cookies.txt as a document → their downloads ride their own session.
Any time: /cookies → Remove my data deletes the file and the record.
"""
from __future__ import annotations

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
    ) -> None:
        self.file_name = file_name
        self.file_size = file_size
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
    def __init__(self, document: FakeCookiesDocument, user_id: int = UID) -> None:
        super().__init__(user_id=user_id)
        self.message.document = document


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


async def test_agree_button_maps_version_mismatch(api_client, backend):
    backend.fail_next = (
        422,
        "version_mismatch",
        "The consent text has changed — run /cookies again.",
    )
    update = CallbackUpdate("cookies:agree")
    await on_callback(update, FakeContext(bot_data={"api": api_client}))

    assert "run /cookies again" in update.callback_query.edits[-1]["text"]


async def test_agree_button_gets_mismatch_from_a_changed_backend_version(
    api_client, backend
):
    backend.consent_version = "2026-11-01.1"  # the backend serves a newer text
    update = CallbackUpdate("cookies:agree")
    await on_callback(update, FakeContext(bot_data={"api": api_client}))

    assert "run /cookies again" in update.callback_query.edits[-1]["text"]
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
    assert "your own session" in replies


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


# ---------------------------------------------------------- PTB registration


def test_registration_orders_the_cookies_handlers_correctly():
    app = Application.builder().token("123456:unit-test").build()
    register_handlers(app)
    group0 = app.handlers[0]

    def index_of(callback) -> int:
        for position, handler in enumerate(group0):
            if getattr(handler, "callback", None) is callback:
                return position
        raise AssertionError(f"{callback} was not registered")

    # A group runs only the FIRST matching handler: the cookies callback must
    # be reachable before merge's bare (matches-everything) CallbackQueryHandler.
    assert index_of(cookies_mod.on_callback) < index_of(merge_mod.on_callback)
    # And a .txt document must reach the cookies handler, not the media
    # catch-all (which also matches filters.Document.ALL).
    assert (
        index_of(cookies_mod.handle_cookies_document)
        < index_of(uploads_mod.handle_media_upload)
    )
    assert any(
        getattr(h, "callback", None) is cookies_mod.cookies_command for h in group0
    )