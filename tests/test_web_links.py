"""The tappable web-link buttons under replies that mention a video/folder.

A video gets the portal's per-video page (`/video/<id>`) — the page that can
still play a unit too large for Telegram to carry, and the only page that
exists for an unfiled unit. A folder-only reply gets the folder page. Admins
also get the console's per-video page. A button whose base URL is local is
dropped: Telegram rejects such a URL and fails the whole message.
"""

from __future__ import annotations

import pytest

from bot.config import Settings
from bot.handlers.status import status_command
from bot.helpers import portal_button, portal_token_for, portal_url, web_links
from bot.keyboards import video_keyboard
from tests.conftest import FakeContext, FakeUpdate

ADMIN = 999
NON_ADMIN = 42

VID = "11111111-1111-1111-1111-111111111111"
FOLDER = "22222222-2222-2222-2222-222222222222"


def _context(admin_ids="999", portal="https://portal.test", console="https://console.test"):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids=admin_ids,
        portal_frontend_url=portal,
        admin_frontend_url=console,
    )
    return FakeContext(bot_data={"settings": settings})


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_a_video_links_to_its_portal_page_for_everyone():
    update = FakeUpdate(user_id=NON_ADMIN)

    markup = web_links(update, _context(), video={"id": VID, "folder_id": FOLDER})

    buttons = _buttons(markup)
    assert [b.text for b in buttons] == ["🌐 Go to portal"]
    assert buttons[0].url == f"https://portal.test/video/{VID}"


def test_an_unfiled_video_still_gets_a_portal_link():
    """A unit in no folder has no folder page — the per-video page is the link."""
    update = FakeUpdate(user_id=NON_ADMIN)

    markup = web_links(update, _context(), video={"id": VID, "folder_id": None})

    assert [b.url for b in _buttons(markup)] == [f"https://portal.test/video/{VID}"]


def test_a_folder_only_reply_links_to_the_folder_page():
    update = FakeUpdate(user_id=ADMIN)

    markup = web_links(update, _context(), folder_id=FOLDER)

    buttons = _buttons(markup)
    assert [b.text for b in buttons] == ["🌐 Open folder"]
    assert buttons[0].url == f"https://portal.test/folder/{FOLDER}"


def test_admin_also_gets_a_console_button_in_the_same_row():
    update = FakeUpdate(user_id=ADMIN)

    markup = web_links(update, _context(), video={"id": VID, "folder_id": FOLDER})

    buttons = _buttons(markup)
    assert [b.text for b in buttons] == ["🌐 Go to portal", "🛠 Go to admin"]
    assert buttons[1].url == f"https://console.test/videos/{VID}"
    assert len(markup.inline_keyboard) == 1, "both links share one row"


def test_non_admins_never_get_the_console_button():
    update = FakeUpdate(user_id=NON_ADMIN)

    markup = web_links(update, _context(), video={"id": VID, "folder_id": FOLDER})

    assert all(b.text != "🛠 Go to admin" for b in _buttons(markup))


def test_nothing_to_link_returns_the_caller_markup_unchanged():
    update = FakeUpdate(user_id=NON_ADMIN)

    assert web_links(update, _context(), video={}) is None
    assert web_links(update, _context()) is None


def test_explicit_ids_win_over_the_video_mapping():
    update = FakeUpdate(user_id=ADMIN)

    markup = web_links(
        update,
        _context(),
        video={"id": "stale-id", "folder_id": "stale-folder"},
        folder_id=FOLDER,
        video_id=VID,
    )

    urls = [b.url for b in _buttons(markup)]
    assert f"https://portal.test/video/{VID}" in urls
    assert f"https://console.test/videos/{VID}" in urls


def test_an_existing_keyboard_keeps_its_rows_and_gains_the_link_row():
    update = FakeUpdate(user_id=NON_ADMIN)

    markup = web_links(
        update,
        _context(),
        video={"id": VID, "folder_id": FOLDER},
        markup=video_keyboard(VID),
    )

    rows = markup.inline_keyboard
    assert len(rows) == 3, "video_keyboard's two rows plus the link row"
    assert rows[0][0].text == "Status", "the original buttons survive"
    assert [b.text for b in rows[1]] == ["Media", "Audio"], "Media and Audio share the second row"
    assert rows[1][1].callback_data == f"audio:{VID}"
    assert [b.text for b in rows[-1]] == ["🌐 Go to portal"]


def test_a_localhost_portal_url_is_dropped_instead_of_failing_the_send():
    """Telegram rejects localhost button URLs ("wrong http url") and the whole
    sendMessage fails, so the button must never be built."""
    update = FakeUpdate(user_id=NON_ADMIN)

    assert web_links(update, _context(portal="http://localhost:5174"), video_id=VID) is None


def test_a_localhost_portal_drops_only_that_button():
    update = FakeUpdate(user_id=ADMIN)

    markup = web_links(
        update,
        _context(portal="http://localhost:5174"),
        video={"id": VID, "folder_id": FOLDER},
    )

    assert [b.text for b in _buttons(markup)] == ["🛠 Go to admin"]


def test_callback_buttons_still_ship_when_every_web_link_is_local():
    """The rest of the reply must survive a locally configured frontend."""
    update = FakeUpdate(user_id=NON_ADMIN)
    base = video_keyboard(VID)

    markup = web_links(
        update, _context(portal="http://localhost:5174"), video_id=VID, markup=base
    )

    assert markup is base
    assert [b.text for b in _buttons(markup)] == ["Status", "Transcript", "Media", "Audio"]


def test_loopback_and_unusable_hosts_are_never_turned_into_buttons():
    update = FakeUpdate(user_id=NON_ADMIN)

    for base in (
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://caddy:5174",
        "ftp://recsterio.example.com",
    ):
        assert web_links(update, _context(portal=base), video_id=VID) is None, base


def test_a_lan_address_is_a_working_button():
    """Probed against the live Bot API: private ranges are accepted, and this is
    how local development reaches the portal from a phone on the same Wi-Fi."""
    update = FakeUpdate(user_id=NON_ADMIN)

    markup = web_links(update, _context(portal="http://192.168.1.50:5174"), video_id=VID)

    assert markup is not None
    assert _buttons(markup)[0].url == f"http://192.168.1.50:5174/video/{VID}"


def test_portal_button_can_be_built_without_an_update():
    """The oversized-file notice has no Update to hand, only settings."""
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="t",
        portal_frontend_url="https://portal.test",
        admin_frontend_url="https://console.test",
    )

    button = portal_button(VID, settings)

    assert button is not None
    assert button.url == f"https://portal.test/video/{VID}"


def test_trailing_slashes_and_a_console_subpath_shape_the_urls_correctly():
    update = FakeUpdate(user_id=ADMIN)

    markup = web_links(
        update,
        _context(portal="https://portal.example/", console="https://console.example/console/"),
        video={"id": VID, "folder_id": FOLDER},
    )

    urls = [b.url for b in _buttons(markup)]
    assert f"https://portal.example/video/{VID}" in urls
    assert f"https://console.example/console/videos/{VID}" in urls
    assert all("//video" not in (url or "") for url in urls), "no doubled slash"


# --------------------------------------------------------- signed portal token
# An unfiled unit is invisible to a browser visitor, so a reply's portal link
# carries a backend-minted ?t= token. The bot keeps no signing secret: when the
# mint call fails it degrades to the bare page instead of dropping the button.


async def test_the_portal_button_carries_the_backend_token(api_client, backend):
    video = await api_client.create_video("https://youtu.be/token")
    vid = video["id"]

    portal = await portal_token_for(
        api_client, vid, requested_by=f"telegram:{NON_ADMIN}"
    )
    markup = web_links(
        FakeUpdate(user_id=NON_ADMIN),
        _context(),
        video={"id": vid},
        token=portal.token,
        allow_portal=portal.linkable(confirmed=True),
    )

    assert portal.token == f"tkn-{vid}"
    assert _buttons(markup)[0].url == f"https://portal.test/video/{vid}?t=tkn-{vid}"


def test_portal_url_carries_the_token_too():
    """The spelled-out link in the oversized-file notice needs the token."""
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="t",
        portal_frontend_url="https://portal.test",
    )

    assert (
        portal_url(VID, settings, token="abc")
        == f"https://portal.test/video/{VID}?t=abc"
    )


@pytest.mark.parametrize(
    "failure",
    [
        (500, "internal_error", "boom"),
        (404, "not_found", "gone"),
    ],
)
async def test_a_failed_token_call_still_yields_the_bare_portal_url(
    api_client, backend, failure
):
    video = await api_client.create_video(f"https://youtu.be/fail-{failure[0]}")
    vid = video["id"]
    backend.portal_link_failure = failure

    portal = await portal_token_for(
        api_client, vid, requested_by=f"telegram:{NON_ADMIN}"
    )

    assert portal.token is None
    assert portal.forbidden is False, "an infrastructure failure is not a 403"
    markup = web_links(
        FakeUpdate(user_id=NON_ADMIN),
        _context(),
        video_id=vid,
        token=portal.token,
        allow_portal=portal.linkable(confirmed=True),
    )
    assert _buttons(markup)[0].url == f"https://portal.test/video/{vid}"


async def test_a_loopback_portal_url_is_dropped_even_with_a_token(api_client, backend):
    video = await api_client.create_video("https://youtu.be/loopback")
    vid = video["id"]
    portal = await portal_token_for(
        api_client, vid, requested_by=f"telegram:{NON_ADMIN}"
    )
    assert portal.token

    markup = web_links(
        FakeUpdate(user_id=NON_ADMIN),
        _context(portal="http://localhost:5174"),
        video_id=vid,
        token=portal.token,
        allow_portal=portal.linkable(confirmed=True),
    )

    assert markup is None


async def test_the_token_is_requested_once_per_reply(api_client, backend):
    """One mint per reply, however long the message is — not per character."""
    video = await api_client.create_video(
        "https://youtu.be/once", requested_by=f"telegram:{NON_ADMIN}"
    )
    vid = video["id"]
    backend.file_video(vid)
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
        portal_frontend_url="https://portal.test",
        admin_frontend_url="https://console.test",
    )
    context = FakeContext(bot_data={"settings": settings, "api": api_client})
    context.args = [vid]
    update = FakeUpdate(user_id=NON_ADMIN)

    await status_command(update, context)

    calls = [
        call
        for call in backend.requests
        if call["path"].endswith(f"/videos/{vid}/portal-link")
    ]
    assert len(calls) == 1
    url = update.message.replies[-1]["reply_markup"].inline_keyboard[-1][0].url
    assert url == f"https://portal.test/video/{vid}?t=tkn-{vid}"


# ------------------------------------------------- fail closed on a backend 403
# The portal-link endpoint evaluates the END USER's access when the bot sends
# X-Requested-By. A 403 means no button and no bare URL; an infrastructure
# failure keeps the bare page only because the video read for that same user
# already vouched for access.


def _api_context(api_client):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
        portal_frontend_url="https://portal.test",
        admin_frontend_url="https://console.test",
    )
    return FakeContext(bot_data={"settings": settings, "api": api_client})


async def _run_status(api_client, video_id, user_id=NON_ADMIN):
    context = _api_context(api_client)
    context.args = [video_id]
    update = FakeUpdate(user_id=user_id)
    await status_command(update, context)
    return update


def _reply_urls(update) -> list[str]:
    markup = update.message.replies[-1]["reply_markup"]
    if markup is None:
        return []
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


def _portal_mints(backend, video_id):
    return [
        call
        for call in backend.requests
        if call["path"].endswith(f"/videos/{video_id}/portal-link")
    ]


async def test_a_forbidden_user_gets_no_portal_button_at_all(api_client, backend):
    """A 403 is about rights, not availability: the page would refuse them."""
    # Owned by the reader: the ownership guard refuses any other unit, and
    # these tests are about the portal-token decision, not that refusal.
    video = await api_client.create_video(
        "https://youtu.be/denied", requested_by=f"telegram:{NON_ADMIN}"
    )
    vid = video["id"]
    backend.file_video(vid)
    backend.portal_link_denied.add(f"telegram:{NON_ADMIN}")

    update = await _run_status(api_client, vid)

    urls = _reply_urls(update)
    assert all("portal.test" not in url for url in urls), urls
    # The refusal only happens when the identity actually rides along; dropping
    # requested_by would let the fake mint a token and the button reappear.
    mints = _portal_mints(backend, vid)
    assert len(mints) == 1
    assert mints[0]["requested_by"] == f"telegram:{NON_ADMIN}"


async def test_a_permitted_user_gets_a_signed_portal_link(api_client, backend):
    video = await api_client.create_video(
        "https://youtu.be/allowed", requested_by=f"telegram:{NON_ADMIN}"
    )
    vid = video["id"]
    backend.file_video(vid)

    update = await _run_status(api_client, vid)

    assert (
        f"https://portal.test/video/{vid}?t=tkn-{vid}" in _reply_urls(update)
    )
    mints = _portal_mints(backend, vid)
    assert mints and mints[0]["requested_by"] == f"telegram:{NON_ADMIN}"
    reads = [
        call
        for call in backend.requests
        if call["method"] == "GET" and call["path"] == f"/api/v1/videos/{vid}"
    ]
    assert reads and all(
        call["requested_by"] == f"telegram:{NON_ADMIN}" for call in reads
    ), "the video must be read as the same end user"


async def test_an_infrastructure_failure_keeps_the_bare_url_when_access_is_known(
    api_client, backend
):
    """The 500 is not a rights answer, and the video read proved access."""
    video = await api_client.create_video(
        "https://youtu.be/outage", requested_by=f"telegram:{NON_ADMIN}"
    )
    vid = video["id"]
    backend.file_video(vid)
    backend.portal_link_failure = (500, "internal_error", "boom")

    update = await _run_status(api_client, vid)

    urls = _reply_urls(update)
    assert f"https://portal.test/video/{vid}" in urls
    assert all("?t=" not in url for url in urls)
    mints = _portal_mints(backend, vid)
    assert mints and mints[0]["requested_by"] == f"telegram:{NON_ADMIN}"
