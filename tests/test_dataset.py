"""Dataset commands: admin gating, rendering, and the ZIP fallback."""

from __future__ import annotations

from bot.config import Settings
from bot.handlers.dataset import (
    MAX_ZIP_BYTES,
    cards_command,
    cleaned_command,
    folder_command,
    folders_command,
    mkdir_command,
    passcode_command,
    share_command,
    zip_command,
)
from tests.conftest import FakeContext, FakeUpdate

ADMIN = 999
NON_ADMIN = 42


def _context(client, admin_ids=str(ADMIN), args=None):
    settings = Settings(
        telegram_bot_token="t",
        bot_api_token="test-service-token",
        bot_api_base_url="http://testserver/api/v1",
        admin_telegram_ids=admin_ids,
    )
    return FakeContext(args=list(args or []), bot_data={"api": client, "settings": settings})


def _admin_ctx(client, args=None):
    return _context(client, args=args)


# ---------------------------------------------------------------- gating


async def test_non_admin_cannot_list_folders(api_client, backend):
    update = FakeUpdate(user_id=NON_ADMIN)
    await folders_command(update, _context(api_client))

    assert "not authorised" in update.message.replies[0]["text"]
    assert backend.requests == []


async def test_non_admin_cannot_create_a_folder(api_client, backend):
    update = FakeUpdate(text="/mkdir Demo", user_id=NON_ADMIN)
    await mkdir_command(update, _context(api_client, args=["Demo"]))

    assert "not authorised" in update.message.replies[0]["text"]
    assert backend.requests == []


async def test_non_admin_cannot_issue_a_passcode(api_client, backend):
    update = FakeUpdate(user_id=NON_ADMIN)
    await passcode_command(update, _context(api_client))

    assert "not authorised" in update.message.replies[0]["text"]
    assert backend.requests == []


async def test_non_admin_cannot_export_a_zip(api_client, backend):
    update = FakeUpdate(user_id=NON_ADMIN)
    await zip_command(update, _context(api_client))

    assert "not authorised" in update.message.replies[0]["text"]
    assert backend.requests == []


# ---------------------------------------------------------------- folders


async def test_folders_lists_the_tree_with_visibility(api_client, backend):
    backend.add_folder("Public demo", visibility="public")
    backend.add_folder("Private demo", visibility="private")

    update = FakeUpdate(user_id=ADMIN)
    await folders_command(update, _admin_ctx(api_client))

    text = update.message.replies[0]["text"]
    assert "Public demo" in text
    assert "Private demo" in text
    assert "🌐" in text and "🔒" in text
    assert any(r["path"] == "/api/v1/folders" for r in backend.requests)


async def test_folders_rejects_a_malformed_id(api_client, backend):
    update = FakeUpdate(user_id=ADMIN)
    await folders_command(update, _admin_ctx(api_client, args=["not-a-uuid"]))

    assert "not a valid folder id" in update.message.replies[0]["text"]
    assert backend.requests == []


async def test_folders_says_so_when_empty(api_client, backend):
    update = FakeUpdate(user_id=ADMIN)
    await folders_command(update, _admin_ctx(api_client))

    assert "No folders found." == update.message.replies[0]["text"]


async def test_folder_shows_one_folder_and_its_children(api_client, backend):
    parent = backend.add_folder("Superset")
    backend.add_folder("Unit A", parent_id=parent["id"])

    update = FakeUpdate(user_id=ADMIN)
    await folder_command(update, _admin_ctx(api_client, args=[parent["id"]]))

    text = update.message.replies[0]["text"]
    assert "Superset" in text
    assert "Sub-folders" in text
    assert "Unit A" in text
    assert any(r["path"] == f"/api/v1/folders/{parent['id']}" for r in backend.requests)


async def test_folder_requires_an_id(api_client, backend):
    update = FakeUpdate(user_id=ADMIN)
    await folder_command(update, _admin_ctx(api_client))

    assert "Usage: /folder <folder id>" == update.message.replies[0]["text"]


# ---------------------------------------------------------------- cards


async def test_cards_lists_units_with_their_badge(api_client, backend):
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    backend.file_video(video["id"], title="A fake video")

    update = FakeUpdate(user_id=ADMIN)
    await cards_command(update, _admin_ctx(api_client))

    text = update.message.replies[0]["text"]
    assert "A fake video" in text
    assert "raw" in text
    assert any(r["path"] == "/api/v1/dataset/cards" for r in backend.requests)


async def test_cards_includes_cleaned_units(api_client, backend):
    """`cleaned` is a filter, not a default: /cards must not drop cleaned units.

    The API reads `cleaned=false` as "raw only", so passing it unconditionally
    would hide exactly the units /cleaned is meant to surface.
    """
    raw = await api_client.create_video("https://youtu.be/raw", requested_by="telegram:1")
    cleaned = await api_client.create_video("https://youtu.be/clean", requested_by="telegram:1")
    backend.file_video(raw["id"], title="Raw unit")
    backend.file_video(cleaned["id"], title="Cleaned unit", cleaned=True)

    update = FakeUpdate(user_id=ADMIN)
    await cards_command(update, _admin_ctx(api_client))

    text = update.message.replies[0]["text"]
    assert "Raw unit" in text
    assert "Cleaned unit" in text
    assert "total 2" in text


async def test_cards_narrows_to_a_folder(api_client, backend):
    folder = backend.add_folder("Only this one")
    video = await api_client.create_video("https://youtu.be/x", requested_by="telegram:1")
    backend.file_video(video["id"], folder_id=folder["id"], title="Inside")

    update = FakeUpdate(user_id=ADMIN)
    await cards_command(update, _admin_ctx(api_client, args=[folder["id"]]))

    text = update.message.replies[0]["text"]
    assert "Inside" in text
    assert "total 1" in text


async def test_cleaned_only_returns_cleaned_units(api_client, backend):
    raw = await api_client.create_video("https://youtu.be/raw", requested_by="telegram:1")
    cleaned = await api_client.create_video("https://youtu.be/clean", requested_by="telegram:1")
    backend.file_video(raw["id"], title="Raw unit")
    backend.file_video(cleaned["id"], title="Cleaned unit", cleaned=True)

    update = FakeUpdate(user_id=ADMIN)
    await cleaned_command(update, _admin_ctx(api_client))

    text = update.message.replies[0]["text"]
    assert "Cleaned units" in text
    assert "Cleaned unit" in text
    assert "Raw unit" not in text


# ---------------------------------------------------------------- mkdir


async def test_mkdir_creates_a_folder(api_client, backend):
    update = FakeUpdate(text="/mkdir Demo unit", user_id=ADMIN)
    await mkdir_command(update, _admin_ctx(api_client, args=["Demo", "unit"]))

    text = update.message.replies[0]["text"]
    assert "Created" in text and "Demo unit" in text
    assert len(backend.folders) == 1
    assert next(iter(backend.folders.values()))["name"] == "Demo unit"


async def test_mkdir_can_create_a_public_folder(api_client, backend):
    update = FakeUpdate(text="/mkdir Open --public", user_id=ADMIN)
    await mkdir_command(update, _admin_ctx(api_client, args=["Open", "--public"]))

    assert "🌐 public" in update.message.replies[0]["text"]
    assert next(iter(backend.folders.values()))["visibility"] == "public"


async def test_mkdir_requires_a_name(api_client, backend):
    update = FakeUpdate(text="/mkdir", user_id=ADMIN)
    await mkdir_command(update, _admin_ctx(api_client))

    assert "Usage: /mkdir" in update.message.replies[0]["text"]
    assert backend.requests == []


# ---------------------------------------------------------------- share


async def test_share_returns_a_sendable_url(api_client, backend):
    folder = backend.add_folder("Shared")
    update = FakeUpdate(user_id=ADMIN)
    await share_command(
        update, _admin_ctx(api_client, args=[folder["id"], "editor", "7"])
    )

    text = update.message.replies[0]["text"]
    assert "Share link" in text
    assert "editor" in text
    assert "https://portal.example.org/share/" in text
    assert len(backend.shares) == 1


async def test_share_rejects_an_unknown_role(api_client, backend):
    folder = backend.add_folder("Shared")
    update = FakeUpdate(user_id=ADMIN)
    await share_command(update, _admin_ctx(api_client, args=[folder["id"], "owner"]))

    assert "viewer, commenter or editor" in update.message.replies[0]["text"]
    assert backend.shares == {}


# ---------------------------------------------------------------- passcode


async def test_passcode_shows_the_generated_code_once(api_client, backend):
    folder = backend.add_folder("Private demo")
    update = FakeUpdate(user_id=ADMIN)
    await passcode_command(
        update, _admin_ctx(api_client, args=[folder["id"], "class", "demo"])
    )

    text = update.message.replies[0]["text"]
    assert "Passcode" in text
    assert "ABCD-2345" in text
    assert "shown once" in text


async def test_passcode_forwards_every_limit(api_client, backend):
    folder = backend.add_folder("Private demo")
    update = FakeUpdate(
        text=f"/passcode {folder['id']} class demo --uses 3 --devices 2 --one-time --days 7",
        user_id=ADMIN,
    )
    await passcode_command(
        update,
        _admin_ctx(
            api_client,
            args=[folder["id"], "class", "demo", "--uses", "3", "--devices", "2", "--one-time", "--days", "7"],
        ),
    )

    passcode = next(iter(backend.passcodes.values()))
    assert passcode["max_uses"] == 3
    assert passcode["max_devices"] == 2
    assert passcode["one_time"] is True
    assert passcode["expires_at"] is not None
    assert "3 use(s)" in update.message.replies[0]["text"]


async def test_passcode_rejects_a_non_numeric_limit(api_client, backend):
    folder = backend.add_folder("Private demo")
    update = FakeUpdate(
        text=f"/passcode {folder['id']} --uses many", user_id=ADMIN
    )
    await passcode_command(
        update, _admin_ctx(api_client, args=[folder["id"], "--uses", "many"])
    )

    assert "must be a positive whole number" in update.message.replies[0]["text"]
    assert backend.passcodes == {}


# ---------------------------------------------------------------- zip


async def test_zip_sends_the_archive_when_it_fits(api_client, backend):
    folder = backend.add_folder("Exportable")
    update = FakeUpdate(user_id=ADMIN)
    await zip_command(update, _admin_ctx(api_client, args=[folder["id"]]))

    assert len(update.message.documents) == 1
    assert update.message.documents[0]["filename"].endswith(".zip")
    assert any(r["path"] == f"/api/v1/folders/{folder['id']}/download" for r in backend.requests)


async def test_zip_points_at_the_portal_when_too_large(api_client, backend):
    folder = backend.add_folder("Huge")
    backend.zip_bytes = b"x" * (MAX_ZIP_BYTES + 1)

    update = FakeUpdate(user_id=ADMIN)
    await zip_command(update, _admin_ctx(api_client, args=[folder["id"]]))

    assert update.message.documents == []
    text = update.message.replies[0]["text"]
    assert "over Telegram's" in text


async def test_zip_reports_an_unknown_folder(api_client, backend):
    update = FakeUpdate(user_id=ADMIN)
    await zip_command(
        update,
        _admin_ctx(api_client, args=["11111111-1111-1111-1111-111111111111"]),
    )

    assert "not_found" in update.message.replies[0]["text"]


async def test_zip_requires_an_id(api_client, backend):
    update = FakeUpdate(user_id=ADMIN)
    await zip_command(update, _admin_ctx(api_client))

    assert "Usage: /zip <folder id>" == update.message.replies[0]["text"]