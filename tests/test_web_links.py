"""The tappable web-link buttons under replies that mention a video/folder.

A video gets the portal's per-video page (`/video/<id>`) — the page that can
still play a unit too large for Telegram to carry, and the only page that
exists for an unfiled unit. A folder-only reply gets the folder page. Admins
also get the console's per-video page. A button whose base URL is local is
dropped: Telegram rejects such a URL and fails the whole message.
"""

from __future__ import annotations

from bot.config import Settings
from bot.helpers import portal_button, web_links
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
