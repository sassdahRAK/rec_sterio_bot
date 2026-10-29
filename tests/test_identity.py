"""Console account linking: /start link-<code>, its Confirm/Cancel buttons,
/whoami, and the linked-admin id cache that feeds is_admin."""

from __future__ import annotations

from telegram.ext import Application

from bot.client import ApiError
from bot.config import Settings
from bot.handlers import identity, register_handlers
from bot.handlers import merge as merge_mod
from bot.handlers.start import start
from bot.helpers import AdminIdsCache, is_admin
from tests.conftest import FakeContext, FakeUpdate, FakeUser

UID = 77
CODE = "abcd1234"
EMAIL = "sokha@example.com"


def _settings(admin_ids: str = "999") -> Settings:
    return Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids=admin_ids,
    )


def _context(client, *, admin_ids: str = "999", cache=None, args=None):
    bot_data = {"api": client, "settings": _settings(admin_ids)}
    if cache is not None:
        bot_data["admin_ids_cache"] = cache
    return FakeContext(args=list(args or []), bot_data=bot_data)


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


class RecordingClient:
    """A stub ApiClient for the cache: records calls, returns what it is told."""

    def __init__(self, ids: list[int] | None = None, error: Exception | None = None):
        self.calls = 0
        self.ids_to_return = list(ids or [])
        self.error = error

    async def get_admin_ids(self) -> list[int]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.ids_to_return)


# ------------------------------------------------------ /start link-<code>


async def test_start_link_dry_run_shows_target_and_buttons(api_client, backend):
    backend.link_codes[CODE] = EMAIL
    update = FakeUpdate(user_id=UID)
    ctx = _context(api_client, args=[f"link-{CODE}"])

    await start(update, ctx)

    reply = update.message.replies[-1]
    assert EMAIL in reply["text"]
    assert "Confirm to continue" in reply["text"]
    datas = [b.callback_data for row in reply["reply_markup"].inline_keyboard for b in row]
    assert f"link:confirm:{CODE}" in datas
    assert f"link:cancel:{CODE}" in datas
    # the dry-run POST reached the backend and linked nothing
    assert backend.link_redeems == [
        {
            "telegram_id": UID,
            "code": CODE,
            "confirm": False,
            "requested_by": f"telegram:{UID}",
        }
    ]
    assert backend.linked == {}


async def test_start_link_with_an_invalid_code_says_so(api_client, backend):
    update = FakeUpdate(user_id=UID)

    await start(update, _context(api_client, args=[f"link-{CODE}"]))

    text = update.message.replies[-1]["text"]
    assert "not valid (wrong, expired, or already used)" in text
    assert "Generate a fresh one from the console Settings page" in text
    assert backend.linked == {}


async def test_start_link_conflict_reports_the_existing_link(api_client, backend):
    backend.link_codes[CODE] = EMAIL
    backend.link_conflict = True
    update = FakeUpdate(user_id=UID)

    await start(update, _context(api_client, args=[f"link-{CODE}"]))

    text = update.message.replies[-1]["text"]
    assert "already_linked" in text
    assert "already linked to a different console account" in text
    assert backend.linked == {}


# ------------------------------------------------------------- the buttons


async def test_confirm_callback_links_and_refreshes_the_admin_cache(api_client, backend):
    backend.link_codes[CODE] = EMAIL
    backend.admin_ids = [UID]
    cache = AdminIdsCache()
    ctx = _context(api_client, cache=cache)
    update = CallbackUpdate(f"link:confirm:{CODE}")

    await identity.on_callback(update, ctx)

    assert update.callback_query.answers, "the button must be answered"
    assert backend.link_redeems[-1] == {
        "telegram_id": UID,
        "code": CODE,
        "confirm": True,
        "requested_by": f"telegram:{UID}",
    }
    assert backend.linked[UID]["email"] == EMAIL
    text = update.callback_query.edits[-1]["text"]
    assert "Linked — this Telegram is now" in text
    assert EMAIL in text
    # the fresh link may grant admin rights, so the cache was refreshed now
    assert cache.ids == frozenset({UID})
    assert any(r["path"] == "/api/v1/bot-users/admin-ids" for r in backend.requests)


async def test_cancel_callback_answers_without_confirming(api_client, backend):
    backend.link_codes[CODE] = EMAIL
    update = CallbackUpdate(f"link:cancel:{CODE}")

    await identity.on_callback(update, _context(api_client))

    assert update.callback_query.answers
    assert update.callback_query.edits[-1]["text"] == "Not linked."
    assert backend.link_redeems == [], "Cancel must not redeem anything"
    assert backend.linked == {}


async def test_confirm_callback_with_a_stale_code_says_so(api_client, backend):
    # the code was consumed by an earlier confirm, so redeeming it again 401s
    backend.link_codes[CODE] = EMAIL
    await api_client.redeem_link(UID, CODE, confirm=True, requested_by=f"telegram:{UID}")
    update = CallbackUpdate(f"link:confirm:{CODE}")

    await identity.on_callback(update, _context(api_client))

    assert update.callback_query.edits[-1]["text"] == (
        "That code is no longer valid — generate a fresh one from the console."
    )


# ------------------------------------------------------------------ /whoami


async def test_whoami_unlinked_shows_id_and_no_link(api_client, backend):
    update = FakeUpdate(user_id=UID)

    await identity.whoami_command(update, _context(api_client))

    text = update.message.replies[-1]["text"]
    assert f"Telegram id: {UID}" in text
    assert "@tester" in text
    assert "No linked console account" in text
    assert "not set up — run /cookies" in text
    assert "Bot admin: no" in text


async def test_whoami_linked_shows_the_console_account(api_client, backend):
    backend.linked[UID] = {"email": EMAIL, "name": "Sokha San", "role": "admin"}
    backend.bot_users[UID] = {
        "telegram_id": UID,
        "consented": True,
        "consented_at": "2026-10-28T00:00:00Z",
        "consent_version": "2026-10-28.1",
        "cookies_present": True,
        "cookies_updated_at": "2026-10-28T00:00:00Z",
    }
    update = FakeUpdate(user_id=UID)

    await identity.whoami_command(update, _context(api_client))

    text = update.message.replies[-1]["text"]
    assert "Linked console account: Sokha San (sokha@example.com), role admin" in text
    assert "consented + cookies on file" in text
    assert "Bot admin: no" in text, "a linked admin must still come through the cache"


# ------------------------------------------------- the admin cache + is_admin


def test_is_admin_merges_env_list_and_cache():
    cache = AdminIdsCache()
    cache.ids = frozenset({4242})  # not in the env allow-list ("999")

    # via the cache only
    assert is_admin(FakeUpdate(user_id=4242), _context(None, cache=cache))
    # without the cache the same id is not an admin
    assert not is_admin(FakeUpdate(user_id=4242), _context(None))
    # the env allow-list still works, cache or not
    assert is_admin(FakeUpdate(user_id=999), _context(None, cache=cache))
    assert is_admin(FakeUpdate(user_id=999), _context(None))


async def test_admin_cache_refresh_failure_keeps_old_ids():
    cache = AdminIdsCache()
    cache.ids = frozenset({1, 2})
    client = RecordingClient(
        error=ApiError("unauthorized", "bad token", status_code=401)
    )

    await cache.refresh(client)  # must not raise

    assert client.calls == 1
    assert cache.ids == frozenset({1, 2})


async def test_admin_cache_refresh_swaps_ids_atomically():
    cache = AdminIdsCache()
    assert cache.ids == frozenset()
    client = RecordingClient(ids=[7, 8])

    await cache.refresh(client)
    assert cache.ids == frozenset({7, 8})

    client.ids_to_return = [7]
    await cache.refresh(client)
    assert cache.ids == frozenset({7}), "a refresh replaces the set, never merges"


# ---------------------------------------------------------- PTB registration


def test_link_callback_is_registered_before_the_merge_catch_all():
    app = Application.builder().token("123456:unit-test").build()
    register_handlers(app)
    group0 = app.handlers[0]

    def index_of(callback) -> int:
        for position, handler in enumerate(group0):
            if getattr(handler, "callback", None) is callback:
                return position
        raise AssertionError(f"{callback} was not registered")

    # A group runs only the FIRST matching handler: the pattern-scoped link
    # callback must be reachable before merge's bare (matches-everything) one.
    assert index_of(identity.on_callback) < index_of(merge_mod.on_callback)
    assert any(
        getattr(h, "callback", None) is identity.whoami_command for h in group0
    )