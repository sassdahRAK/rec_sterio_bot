"""Client behaviour: auth header, error envelope, retries, timeouts."""

from __future__ import annotations

import tempfile

import httpx
import pytest

from bot.__main__ import build_application
from bot.client import (
    ApiClient,
    ApiError,
    ApiNetworkError,
    CompressedNotReadyError,
)
from bot.config import Settings


class BoomTransport(httpx.AsyncBaseTransport):
    """A transport that always fails to connect."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectError("connection refused", request=request)


async def test_client_sends_bearer_token_and_actor(api_client, backend):
    await api_client.create_video("https://youtu.be/a", requested_by="telegram:5")
    call = backend.requests[-1]
    assert call["authorization"] == "Bearer test-service-token"
    assert call["requested_by"] == "telegram:5"


async def test_error_envelope_is_parsed(api_client, backend):
    backend.fail_next = (422, "validation_failed", "source_url must be a valid http(s) URL.")
    with pytest.raises(ApiError) as excinfo:
        await api_client.create_video("not-a-url")
    assert excinfo.value.code == "validation_failed"
    assert "validation_failed" in excinfo.value.user_message().replace("\\", "")


async def test_unauthorized_becomes_api_error(api_client, backend):
    backend.token = "a-different-token"
    with pytest.raises(ApiError) as excinfo:
        await api_client.list_jobs()
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "unauthorized"


async def test_network_failure_is_retried_then_raised():
    transport = BoomTransport()
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    client = ApiClient(
        base_url="http://testserver/api/v1",
        token="t",
        transport=transport,
        max_retries=2,
        backoff=0.5,
        sleep=fake_sleep,
    )
    with pytest.raises(ApiNetworkError) as excinfo:
        await client.list_jobs()

    assert transport.calls == 3  # initial attempt + 2 retries
    assert delays == [0.5, 1.0]
    assert "network_error" in excinfo.value.user_message().replace("\\", "")
    await client.aclose()


async def test_duplicate_video_returns_the_existing_video(api_client, backend):
    first = await api_client.create_video("https://youtu.be/dup", requested_by="telegram:1")
    second = await api_client.create_video("https://youtu.be/dup", requested_by="telegram:1")
    assert first["id"] == second["id"]
    assert len(backend.videos) == 1


async def test_media_url_is_built_from_the_base_url(api_client):
    assert api_client.media_url("abc") == "http://testserver/api/v1/videos/abc/media"


async def _downloaded_video(api_client, slug: str) -> str:
    """Create a video and walk it to the 'downloaded' state."""
    video = await api_client.create_video(f"https://youtu.be/{slug}")
    for _ in range(4):
        await api_client.get_video(video["id"])
    return video["id"]


async def test_download_media_to_carries_the_variant_query(api_client, backend):
    """variant=compressed reaches the wire as a query parameter of the streamed
    GET — the same request get_media makes, just without the buffer."""
    video_id = await _downloaded_video(api_client, "stream-variant")
    backend.compressed_media[video_id] = b"compressed-bytes"

    with tempfile.NamedTemporaryFile() as tmp:
        written = await api_client.download_media_to(
            video_id, tmp.name, max_bytes=1024, variant="compressed"
        )

    assert written == len(b"compressed-bytes")
    media_requests = [
        r for r in backend.requests if r["path"] == f"/api/v1/videos/{video_id}/media"
    ]
    assert media_requests, "the stream must hit the media endpoint"
    assert media_requests[-1]["params"] == {"variant": "compressed"}


async def test_download_media_to_maps_a_missing_artifact_to_the_typed_error(
    api_client, backend
):
    """A 404 compressed_not_ready becomes CompressedNotReadyError on the
    streaming method too, so both media reads share one error vocabulary."""
    video_id = await _downloaded_video(api_client, "stream-not-ready")

    with (
        tempfile.NamedTemporaryFile() as tmp,
        pytest.raises(CompressedNotReadyError),
    ):
        await api_client.download_media_to(
            video_id, tmp.name, max_bytes=1024, variant="compressed"
        )


# --- config ---------------------------------------------------------------
def test_admin_ids_are_parsed_from_the_env(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111, 222 ,333")
    settings = Settings()
    assert settings.admin_ids == frozenset({111, 222, 333})
    assert settings.is_admin(222)
    assert not settings.is_admin(999)
    assert not settings.is_admin(None)


def test_secrets_are_redacted_in_public_dict(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:secret")
    monkeypatch.setenv("BOT_API_TOKEN", "service-secret")
    settings = Settings()
    public = settings.public_dict()
    assert public["telegram_bot_token"] == "***"
    assert public["bot_api_token"] == "***"
    assert "secret" not in str(public)


def test_missing_token_fails_at_startup_not_at_import(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("BOT_API_TOKEN", "service-secret")
    # ``_env_file=None`` keeps the real local .env out of the test: deleting the
    # env var alone is not enough, because pydantic-settings also reads .env.
    settings = Settings(_env_file=None)
    assert settings.telegram_bot_token == ""
    with pytest.raises(SystemExit) as excinfo:
        build_application(settings)
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_bot_is_importable_without_a_token():
    import importlib

    import bot
    import bot.client
    import bot.config
    import bot.handlers

    assert importlib.reload(bot.config) is not None
