"""Transcript delivery: short text inline, long text as a .txt document."""

from __future__ import annotations

from bot.config import Settings
from bot.client import escape_markdown
from bot.handlers.transcript import transcript_command, transcripts_command
from tests.conftest import FakeContext, FakeUpdate


def _context(client, limit=3000, args=None):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids="999",
        transcript_text_limit=limit,
    )
    return FakeContext(
        args=list(args or []), bot_data={"api": client, "settings": settings}
    )


async def test_short_transcript_is_sent_as_text(api_client, backend):
    video = await api_client.create_video("https://youtu.be/short", requested_by="telegram:1")
    backend.add_transcript(video["id"], "Short and sweet.")

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[video["id"]])
    await transcript_command(update, ctx)

    assert len(update.message.replies) == 1
    text = update.message.replies[0]["text"]
    assert escape_markdown("Short and sweet.") in text
    assert "v1" in text
    assert update.message.documents == []


async def test_long_transcript_is_sent_as_a_document(api_client, backend):
    video = await api_client.create_video("https://youtu.be/long", requested_by="telegram:1")
    backend.add_transcript(video["id"], "x" * 5000)

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, limit=1000, args=[video["id"]])
    await transcript_command(update, ctx)

    assert len(update.message.documents) == 1
    doc = update.message.documents[0]
    assert doc["document"] == b"x" * 5000
    assert doc["filename"].endswith(".txt")
    assert "v1" in doc["caption"]


async def test_transcript_reply_carries_the_web_links(api_client, backend):
    """/transcript fetches the video too, so its reply can link to both apps."""
    video = await api_client.create_video("https://youtu.be/linked", requested_by="telegram:1")
    backend.add_transcript(video["id"], "Linked transcript.")
    folder = backend.add_folder("Transcript folder")
    backend.videos[video["id"]]["folder_id"] = folder["id"]

    update = FakeUpdate(user_id=999)  # admin
    ctx = _context(api_client, args=[video["id"]])
    await transcript_command(update, ctx)

    markup = update.message.replies[0]["reply_markup"]
    urls = [b.url for row in markup.inline_keyboard for b in row]
    assert f"https://portal.test/video/{video['id']}" in urls
    assert f"https://console.test/videos/{video['id']}" in urls


async def test_transcript_404_is_reported_friendlyly(api_client, backend):
    video = await api_client.create_video("https://youtu.be/none", requested_by="telegram:1")

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[video["id"]])
    await transcript_command(update, ctx)

    text = update.message.replies[0]["text"]
    assert "No transcript yet" in text
    assert "not_found" not in text  # a 404 is not an error for the user


async def test_transcripts_lists_versions(api_client, backend):
    video = await api_client.create_video("https://youtu.be/versions", requested_by="telegram:1")
    backend.add_transcript(video["id"], "version one")
    backend.add_transcript(video["id"], "version two", version=2, source="manual")

    update = FakeUpdate(user_id=1)
    ctx = _context(api_client, args=[video["id"]])
    await transcripts_command(update, ctx)

    text = update.message.replies[0]["text"]
    assert "Transcript versions" in text
    assert "v1" in text


async def test_transcript_requires_a_video_id(api_client):
    update = FakeUpdate(user_id=1)
    ctx = _context(api_client)
    await transcript_command(update, ctx)
    assert "Usage" in update.message.replies[0]["text"]
