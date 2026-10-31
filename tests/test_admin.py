"""Admin-only commands must reject non-admins *without* calling the backend."""

from __future__ import annotations

import asyncio

from bot.config import Settings
from bot.handlers.admin import jobretry_command, jobs_command, retry_command
from tests.conftest import FakeBot, FakeContext, FakeUpdate


def _context(client, admin_ids="999", args=None):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids=admin_ids,
    )
    return FakeContext(args=list(args or []), bot_data={"api": client, "settings": settings})


def _fast_settings(admin_ids="999"):
    """Poll settings that let a background watch settle inside a test."""
    return Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids=admin_ids,
        poll_interval_seconds=0.01,
        poll_timeout_seconds=5.0,
    )


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
    await ctx.bot_data["watchers"].cancel_all()


async def test_admin_can_retry_a_job(api_client, backend):
    await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    job_id = next(iter(backend.jobs))
    update = FakeUpdate(user_id=999)
    ctx = _context(api_client, args=[job_id])

    await jobretry_command(update, ctx)

    assert "re-queued" in update.message.replies[0]["text"]
    assert any(r["path"] == f"/api/v1/jobs/{job_id}/retry" for r in backend.requests)
    await ctx.bot_data["watchers"].cancel_all()


async def test_admin_retry_starts_a_watch_that_delivers_the_media(api_client, backend):
    """/retry must not just re-queue: the admin's chat gets the same background
    watch a fresh link gets, so the media is delivered (instruction.txt #2)
    and the Telegram copy is recorded (instruction.txt #4)."""
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    bot = FakeBot()
    ctx = FakeContext(
        args=[video["id"]],
        bot_data={"api": api_client, "settings": _fast_settings()},
        bot=bot,
    )
    update = FakeUpdate(user_id=999)  # admin

    await retry_command(update, ctx)

    assert "Re-queued" in update.message.replies[0]["text"]
    # the fake backend walks the video back through the status ladder, so the
    # watch settles at `transcribed` within a few polls
    watchers = ctx.bot_data["watchers"]
    for _ in range(500):  # bounded wait (~5s); settles in well under a second
        if not watchers.is_watching(video["id"]):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the watch started by /retry did not settle")

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the retried video's file should arrive without being asked"
    assert documents[0]["document"] == b"fake-media-bytes"
    assert documents[0]["chat_id"] == update.effective_chat.id
    texts = [m["text"] for m in bot.sent if "text" in m]
    assert any("Transcript ready" in t for t in texts)
    # instruction.txt #4: delivering the file also records the Telegram copy
    assert backend.telegram_links, "the delivered Telegram copy must be recorded"
    assert backend.telegram_links[0]["file_id"].startswith("file-")


async def test_admin_retry_keeps_media_calls_on_the_service_identity(api_client, backend):
    """The admin gate is the authorization: /retry, its video read and the
    watch it starts must never send X-Requested-By, or a video the admin does
    not own would be refused after the backend's ownership change. The
    portal-token mint is the one exception (see the next two tests)."""
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    bot = FakeBot()
    ctx = FakeContext(
        args=[video["id"]],
        bot_data={"api": api_client, "settings": _fast_settings()},
        bot=bot,
    )
    update = FakeUpdate(user_id=999)  # admin who does not own the video

    await retry_command(update, ctx)

    watchers = ctx.bot_data["watchers"]
    for _ in range(500):
        if not watchers.is_watching(video["id"]):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the watch started by /retry did not settle")

    documents = [m for m in bot.sent if m.get("document") is not None]
    assert documents, "the service identity still delivers the media"
    touched = [
        r for r in backend.requests
        if r["path"].startswith(f"/api/v1/videos/{video['id']}")
    ]
    assert touched, "the retry path must call the video routes"
    service_calls = [
        r for r in touched if not r["path"].endswith("/portal-link")
    ]
    assert all(r["requested_by"] is None for r in service_calls), (
        "/retry and its watch must stay on the service identity"
    )
    mints = [r for r in touched if r["path"].endswith("/portal-link")]
    assert mints and all(r["requested_by"] == "telegram:999" for r in mints), (
        "the portal-token mint must name the admin, not the service"
    )


async def test_admin_retry_by_a_linked_admin_keeps_the_portal_button(api_client, backend):
    """The mint rides as the admin: the backend resolves them to a console
    admin account, so the button survives on a retried video."""
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    backend.linked[999] = {"email": "ops@example.com", "name": "Ops", "role": "admin"}
    update = FakeUpdate(user_id=999)
    ctx = _context(api_client, args=[video["id"]])

    await retry_command(update, ctx)

    markup = update.message.replies[0]["reply_markup"]
    urls = [b.url for row in markup.inline_keyboard for b in row]
    portal = f"https://portal.test/video/{video['id']}"
    assert any(url and url.startswith(portal) and "?t=" in url for url in urls)
    mints = [
        r for r in backend.requests
        if r["path"] == f"/api/v1/videos/{video['id']}/portal-link"
    ]
    assert mints and mints[0]["requested_by"] == "telegram:999"
    await ctx.bot_data["watchers"].cancel_all()


async def test_admin_retry_by_an_unlinked_admin_fails_closed_on_the_portal(
    api_client, backend
):
    """An ADMIN_TELEGRAM_IDS-only admin is not a console principal, so the
    backend refuses the mint. The retry still runs; only the button is gone."""
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    update = FakeUpdate(user_id=999)  # admin via the env list only
    ctx = _context(api_client, args=[video["id"]])

    await retry_command(update, ctx)

    assert "Re-queued" in update.message.replies[0]["text"]
    assert any(
        r["path"] == f"/api/v1/videos/{video['id']}/retry"
        for r in backend.requests
    ), "the retry itself must still be sent"
    markup = update.message.replies[0]["reply_markup"]
    urls = (
        []
        if markup is None
        else [b.url for row in markup.inline_keyboard for b in row if b.url]
    )
    assert all("/portal.test/" not in url for url in urls), (
        "a refused mint must leave no portal button (fail closed)"
    )
    assert backend.portal_links == [], "the fake must refuse, not mint"
    await ctx.bot_data["watchers"].cancel_all()


async def test_admin_retry_threads_the_console_link_into_the_watch(api_client, backend):
    """/retry computes the link buttons once: the reply gets them and so does
    every notification the background watch later sends."""
    video = await api_client.create_video("https://youtu.be/retry-links", requested_by="telegram:1")
    bot = FakeBot()
    ctx = FakeContext(
        args=[video["id"]],
        bot_data={"api": api_client, "settings": _fast_settings()},
        bot=bot,
    )
    update = FakeUpdate(user_id=999)  # admin

    await retry_command(update, ctx)

    markup = update.message.replies[0]["reply_markup"]
    urls = [b.url for row in markup.inline_keyboard for b in row]
    assert f"https://console.test/videos/{video['id']}" in urls

    watchers = ctx.bot_data["watchers"]
    for _ in range(500):  # bounded wait (~5s); settles in well under a second
        if not watchers.is_watching(video["id"]):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the watch started by /retry did not settle")

    ready = [m for m in bot.sent if "Transcript ready" in m.get("text", "")]
    assert ready, "the watch must reach its terminal notification"
    urls = [b.url for row in ready[0]["reply_markup"].inline_keyboard for b in row]
    assert f"https://console.test/videos/{video['id']}" in urls


async def test_admin_jobretry_watches_download_jobs_only(api_client, backend):
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    download_job = next(j for j in backend.jobs.values() if j["kind"] == "download")
    # a transcribe job on the same video — retried, but nothing re-downloads
    await api_client.generate_transcript(video["id"])
    transcribe_job = next(j for j in backend.jobs.values() if j["kind"] == "transcribe")
    update = FakeUpdate(user_id=999)  # admin

    ctx = FakeContext(
        args=[download_job["id"]],
        bot_data={"api": api_client, "settings": _fast_settings()},
    )
    await jobretry_command(update, ctx)

    assert ctx.bot_data["watchers"].is_watching(video["id"])
    await ctx.bot_data["watchers"].cancel_all()

    # other job kinds do not re-download, so no watch is started for them
    ctx = FakeContext(
        args=[transcribe_job["id"]],
        bot_data={"api": api_client, "settings": _fast_settings()},
    )
    await jobretry_command(update, ctx)

    watchers = ctx.bot_data.get("watchers")
    assert watchers is None or not watchers.is_watching(video["id"])
