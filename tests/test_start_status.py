"""`/start` (including the deep link) and `/status`."""

from __future__ import annotations

from bot.config import Settings
from bot.handlers.start import help_command, start
from bot.handlers.status import status_command
from tests.conftest import FakeContext, FakeUpdate


def _context(client, args=None):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
    )
    return FakeContext(args=list(args or []), bot_data={"api": client, "settings": settings})


async def test_start_sends_the_welcome_message(api_client):
    update = FakeUpdate(user_id=1)
    ctx = _context(api_client)

    await start(update, ctx)

    text = update.message.replies[0]["text"]
    assert "recsterio" in text
    assert "link" in text.lower()


async def test_help_sends_the_welcome_message(api_client):
    update = FakeUpdate(user_id=1)
    ctx = _context(api_client)

    await help_command(update, ctx)

    assert "recsterio" in update.message.replies[0]["text"]


async def test_start_with_a_deep_link_shows_the_video(api_client, backend):
    video = await api_client.create_video("https://youtu.be/deep", requested_by="telegram:1")

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[video["id"]])

    await start(update, ctx)

    text = update.message.replies[0]["text"]
    assert video["id"] in text
    assert "Status" in text


async def test_status_shows_progress_size_and_duration(api_client, backend):
    video = await api_client.create_video("https://youtu.be/s", requested_by="telegram:1")

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[video["id"]])

    await status_command(update, ctx)

    text = update.message.replies[0]["text"]
    assert video["id"] in text
    assert "Progress" in text
    assert "Size" in text
    assert "Duration" in text
    assert "Transcript" in text


async def test_status_reply_carries_the_web_links_for_an_admin(api_client, backend):
    video = await api_client.create_video("https://youtu.be/links", requested_by="telegram:1")
    folder = backend.add_folder("Status folder")
    backend.videos[video["id"]]["folder_id"] = folder["id"]

    update = FakeUpdate(user_id=999)  # admin
    ctx = _context(api_client, args=[video["id"]])

    await status_command(update, ctx)

    markup = update.message.replies[0]["reply_markup"]
    urls = [b.url for row in markup.inline_keyboard for b in row]
    assert f"https://portal.test/video/{video['id']}" in urls
    assert f"https://console.test/videos/{video['id']}" in urls
    # the pre-existing video buttons survive the merge
    assert "Status" in [b.text for row in markup.inline_keyboard for b in row]


async def test_status_reports_an_unknown_id(api_client, backend):
    import uuid

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[str(uuid.uuid4())])

    await status_command(update, ctx)

    assert "No video with id" in update.message.replies[0]["text"]


async def test_status_rejects_a_non_uuid(api_client):
    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=["not-a-uuid"])

    await status_command(update, ctx)

    assert "not a valid video id" in update.message.replies[0]["text"]
