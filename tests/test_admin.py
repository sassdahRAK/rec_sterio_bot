"""Admin-only commands must reject non-admins *without* calling the backend."""

from __future__ import annotations

from bot.config import Settings
from bot.handlers.admin import jobretry_command, jobs_command, retry_command
from tests.conftest import FakeContext, FakeUpdate


def _context(client, admin_ids="999", args=None):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids=admin_ids,
    )
    return FakeContext(args=list(args or []), bot_data={"api": client, "settings": settings})


async def test_non_admin_cannot_list_jobs(api_client, backend):
    update = FakeUpdate(user_id=42)  # 42 is not in ADMIN_TELEGRAM_IDS
    ctx = _context(api_client)

    await jobs_command(update, ctx)

    assert "not authorised" in update.message.replies[0]["text"]
    # no request may reach the backend
    assert backend.requests == []


async def test_non_admin_cannot_retry(api_client, backend):
    update = FakeUpdate(user_id=42)
    ctx = _context(api_client)

    await retry_command(update, ctx)

    assert "not authorised" in update.message.replies[0]["text"]
    assert backend.requests == []


async def test_non_admin_cannot_retry_job(api_client, backend):
    update = FakeUpdate(user_id=42)
    ctx = _context(api_client)

    await jobretry_command(update, ctx)

    assert "not authorised" in update.message.replies[0]["text"]
    assert backend.requests == []


async def test_admin_can_list_jobs(api_client, backend):
    await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    update = FakeUpdate(user_id=999)  # admin
    ctx = _context(api_client)

    await jobs_command(update, ctx)

    text = update.message.replies[0]["text"]
    assert "Recent jobs" in text
    assert "download" in text
    assert any(r["path"] == "/api/v1/jobs" for r in backend.requests)


async def test_admin_can_retry_a_video(api_client, backend):
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    update = FakeUpdate(user_id=999)
    ctx = _context(api_client, args=[video["id"]])

    await retry_command(update, ctx)

    assert "Re-queued" in update.message.replies[0]["text"]
    assert any(r["path"] == f"/api/v1/videos/{video['id']}/retry" for r in backend.requests)


async def test_admin_can_retry_a_job(api_client, backend):
    await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    job_id = next(iter(backend.jobs))
    update = FakeUpdate(user_id=999)
    ctx = _context(api_client, args=[job_id])

    await jobretry_command(update, ctx)

    assert "re-queued" in update.message.replies[0]["text"]
    assert any(r["path"] == f"/api/v1/jobs/{job_id}/retry" for r in backend.requests)
