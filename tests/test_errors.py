"""Backend errors are surfaced to the user and never crash a handler."""

from __future__ import annotations

from bot.client import ApiError, ApiNetworkError, escape_markdown
from bot.config import Settings
from bot.handlers.errors import error_handler
from bot.handlers.links import handle_link
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


async def test_error_envelope_is_surfaced_to_the_user(api_client, backend):
    backend.fail_next = (422, "validation_failed", "source_url must be a valid http(s) URL.")

    update = FakeUpdate(text="https://example.com/video")
    ctx = _context(api_client)

    await handle_link(update, ctx)

    text = update.message.replies[0]["text"]
    assert "validation_failed" in text.replace("\\", "")
    assert escape_markdown("must be a valid http(s) URL.") in text


async def test_duplicate_video_code_is_surfaced(api_client, backend):
    await api_client.create_video("https://youtu.be/dup", requested_by="telegram:1")
    backend.fail_next = (409, "duplicate_video", "A video with this source_url already exists.")

    update = FakeUpdate(text="https://youtu.be/dup")
    ctx = _context(api_client)

    await handle_link(update, ctx)

    text = update.message.replies[0]["text"]
    assert "duplicate_video" in text.replace("\\", "")


async def test_500_from_backend_does_not_crash_the_handler(api_client, backend):
    backend.fail_next = (500, "internal_error", "boom")

    update = FakeUpdate(text="https://youtu.be/broken")
    ctx = _context(api_client)

    await handle_link(update, ctx)

    assert len(update.message.replies) == 1
    text = update.message.replies[0]["text"]
    assert "http_500" in text.replace("\\", "")
    assert "boom" in text


async def test_status_handler_survives_a_500(api_client, backend):
    import uuid

    video_id = str(uuid.uuid4())
    backend.fail_next = (500, "internal_error", "kaboom")

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[video_id])
    await status_command(update, ctx)

    assert "http_500" in update.message.replies[0]["text"].replace("\\", "")


async def test_global_error_handler_replies_to_the_user():
    update = FakeUpdate(text="boom")
    ctx = FakeContext()
    ctx.error = ApiError("validation_failed", "bad input", [{"field": "x"}])

    await error_handler(update, ctx)

    text = update.message.replies[0]["text"]
    assert "validation_failed" in text.replace("\\", "")
    assert "bad input" in text


async def test_global_error_handler_handles_network_errors():
    update = FakeUpdate(text="boom")
    ctx = FakeContext()
    ctx.error = ApiNetworkError("no route to host")

    await error_handler(update, ctx)

    assert "network_error" in update.message.replies[0]["text"].replace("\\", "")


async def test_global_error_handler_handles_unexpected_errors():
    update = FakeUpdate(text="boom")
    ctx = FakeContext()
    ctx.error = ValueError("kaboom")

    await error_handler(update, ctx)

    assert "kaboom" in update.message.replies[0]["text"]
