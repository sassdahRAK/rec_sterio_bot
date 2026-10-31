"""The /collect TvNa wizard (instruction.txt #6)."""
from __future__ import annotations

import pytest

from bot.handlers import collect
from tests.conftest import FakeContext, FakeUpdate

VIDEO_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def ctx(api_client):
    """A context with a fresh per-user draft."""
    return FakeContext(bot_data={"api": api_client}, user_data={})


def seed(backend):
    """A created video, as the wizard's URL step would leave it."""
    backend.videos[VIDEO_ID] = {
        "id": VIDEO_ID,
        "source_url": "https://www.tiktok.com/@chewbash/video/1",
        "platform": "tiktok",
        "title": "Chew & Bash",
        "status": "pending",
        "progress": 0,
        "transcripts": [],
        # The wizard runs as the default test user (42); content routes are
        # owner-only, so the seeded unit belongs to that same user.
        "created_by": "telegram:42",
        "author_handle": "@chewbash",
        "date_posted": "2026-09-14",
        "caption": "Chew & Bash is the best late-night spot in Phnom Penh",
    }


async def test_the_wizard_asks_for_a_link_first(ctx):
    update = FakeUpdate("/collect")
    state = await collect.start_collect(update, ctx)
    assert state == collect.ASK_URL
    assert "Paste" in update.message.replies[-1]["text"]


async def test_a_non_link_reply_is_refused(ctx):
    update = FakeUpdate("this is not a link")
    state = await collect.receive_url(update, ctx)

    assert state == collect.ASK_URL
    assert "does not look like a link" in update.message.replies[-1]["text"]


async def test_pasting_a_link_creates_the_video_and_asks_for_the_place(ctx, backend):
    update = FakeUpdate("https://www.tiktok.com/@chewbash/video/1")
    state = await collect.receive_url(update, ctx)

    assert state == collect.ASK_LOCATION
    assert ctx.user_data["tvna_draft"]["video_id"]
    assert "Location tag" in update.message.replies[-1]["text"]


async def test_the_location_tag_is_required(ctx):
    ctx.user_data["tvna_draft"] = {"video_id": VIDEO_ID}
    update = FakeUpdate("   ")
    state = await collect.receive_location(update, ctx)

    assert state == collect.ASK_LOCATION
    assert "required" in update.message.replies[-1]["text"]


async def test_location_then_method_then_notes_saves_the_record(ctx, backend):
    seed(backend)
    ctx.user_data["tvna_draft"] = {"video_id": VIDEO_ID}

    loc = FakeUpdate("Chew & Bash, Phnom Penh")
    state = await collect.receive_location(loc, ctx)
    assert state == collect.ASK_METHOD

    method = FakeUpdate("1")
    state = await collect.receive_method(method, ctx)
    assert state == collect.ASK_NOTES
    assert ctx.user_data["tvna_draft"]["collection_method"] == "Manual browse"

    notes = FakeUpdate("Best late-night spot; verified on site.")
    state = await collect.receive_notes(notes, ctx)

    assert state == collect.ConversationHandler.END
    record = backend.tvna_records[-1]
    assert record["location_tag"] == "Chew & Bash, Phnom Penh"
    assert record["collection_method"] == "Manual browse"
    assert record["notes"] == "Best late-night spot; verified on site."
    # The confirmation shows the TvNa block back, with the assigned ID.
    assert "PP-001" in notes.message.replies[-1]["text"]
    # The draft is closed.
    assert "tvna_draft" not in ctx.user_data


async def test_notes_can_be_skipped_with_a_dash(ctx, backend):
    seed(backend)
    ctx.user_data["tvna_draft"] = {
        "video_id": VIDEO_ID,
        "location_tag": "Chew & Bash",
        "collection_method": "Manual browse",
    }
    update = FakeUpdate("-")
    state = await collect.receive_notes(update, ctx)

    assert state == collect.ConversationHandler.END
    assert backend.tvna_records[-1]["notes"] is None


async def test_an_unrecognised_method_re_asks(ctx):
    ctx.user_data["tvna_draft"] = {"video_id": VIDEO_ID, "location_tag": "Place"}
    update = FakeUpdate("maybe?")
    state = await collect.receive_method(update, ctx)

    assert state == collect.ASK_METHOD
    assert "Reply 1 or 2" in update.message.replies[-1]["text"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", "Manual browse"),
        ("manual", "Manual browse"),
        # The full prompt line, exactly as the bot displays it, in both
        # dash styles — a reply copied straight from the prompt must count.
        ("1 — Manual browse", "Manual browse"),
        ("1 - manual browse", "Manual browse"),
        ("1—Manual browse", "Manual browse"),
        ("2", "Research API"),
        ("api", "Research API"),
        ("RESEARCH API", "Research API"),
        ("2 — Research API", "Research API"),
        ("  2 ", "Research API"),
        ("nonsense", None),
    ],
)
async def test_parse_method(text, expected):
    assert collect.parse_method(text) == expected


async def test_cancel_abandons_the_draft(ctx):
    ctx.user_data["tvna_draft"] = {"video_id": VIDEO_ID, "location_tag": "Place"}
    update = FakeUpdate("/cancel")
    state = await collect.cancel(update, ctx)

    assert state == collect.ConversationHandler.END
    assert "tvna_draft" not in ctx.user_data


async def test_record_command_renders_the_schema(ctx, backend):
    seed(backend)
    backend.videos[VIDEO_ID].update(
        {
            "tvna_serial": 1,
            "tvna_id": "PP-001",
            "location_tag": "Chew & Bash, Phnom Penh",
            "source_type": "Video",
            "collection_method": "Manual browse",
            "collected_by": "Omra",
            "collection_date": "2026-10-28",
        }
    )
    ctx.args = [VIDEO_ID]
    update = FakeUpdate("/record " + VIDEO_ID)
    await collect.record_command(update, ctx)

    text = update.message.replies[-1]["text"]
    for field in (
        "PP-001",
        "Platform",
        "Source Type",
        "Author/Handle",
        "Date Posted",
        "Caption",
        "Location Tag",
        "Collection Method",
        "Collected By",
        "Collection Date",
        "Notes",
    ):
        assert field in text, field


async def test_records_command_lists_only_recorded_items(ctx, backend):
    backend.videos[VIDEO_ID] = {
        "id": VIDEO_ID,
        "source_url": "https://example.com/a.mp4",
        "platform": "tiktok",
        "title": "A",
        "status": "downloaded",
        "progress": 100,
        "tvna_serial": 1,
        "tvna_id": "PP-001",
        "location_tag": "Chew & Bash",
        "transcripts": [],
    }
    backend.videos["unrecorded"] = {
        "id": "unrecorded",
        "source_url": "https://example.com/b.mp4",
        "platform": "direct",
        "title": "B",
        "status": "downloaded",
        "progress": 100,
        "transcripts": [],
    }
    update = FakeUpdate("/records")
    await collect.records_command(update, ctx)

    text = update.message.replies[-1]["text"]
    assert "PP-001" in text
    assert "Chew & Bash" in text
    assert "unrecorded" not in text
