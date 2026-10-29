"""Shared pytest fixtures and Telegram fakes."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("BOT_API_TOKEN", "test-service-token")
os.environ.setdefault("BOT_API_BASE_URL", "http://testserver/api/v1")
os.environ.setdefault("ADMIN_TELEGRAM_IDS", "999")
# Deployed-style frontend URLs: URL buttons pointing at localhost are dropped
# (Telegram rejects them outright), so tests must not rely on the dev defaults.
os.environ.setdefault("PORTAL_FRONTEND_URL", "https://portal.test")
os.environ.setdefault("ADMIN_FRONTEND_URL", "https://console.test")


class FakeMessage:
    """Minimal stand-in for :class:`telegram.Message`."""

    def __init__(self, text: str = "", chat_id: int = 42) -> None:
        self.text = text
        self.chat_id = chat_id
        self.replies: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.replies.append({"text": text, **kwargs})
        return self

    async def reply_document(self, document: Any = None, **kwargs: Any) -> FakeMessage:
        self.documents.append({"document": document, **kwargs})
        return self

    async def edit_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.edits.append({"text": text, **kwargs})
        # An edit replaces the message body, so keep replies in sync for tests
        # that assert on "what the user ends up seeing".
        self.replies.append({"text": text, **kwargs})
        return self


class FakeUser:
    def __init__(self, user_id: int = 42) -> None:
        self.id = user_id
        self.username = "tester"
        self.first_name = "Test"


class FakeChat:
    def __init__(self, chat_id: int = 42, chat_type: str = "private") -> None:
        self.id = chat_id
        self.type = chat_type


class FakeUpdate:
    def __init__(
        self,
        text: str = "",
        user_id: int = 42,
        chat_id: int = 42,
        chat_type: str = "private",
    ) -> None:
        self.message = FakeMessage(text=text, chat_id=chat_id)
        self.effective_message = self.message
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(chat_id, chat_type)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return {"message_id": len(self.sent)}

    async def send_document(self, chat_id: int, document: Any = None, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "document": document, **kwargs})
        # The real bot hands back a Message whose .document.file_id identifies
        # the stored copy, so the fake must too — the record step reads it.
        return types.SimpleNamespace(
            document=types.SimpleNamespace(file_id=f"file-{len(self.sent)}"),
            message_id=len(self.sent),
        )


class FakeContext:
    def __init__(
        self,
        *,
        args: list[str] | None = None,
        bot_data: dict[str, Any] | None = None,
        bot: FakeBot | None = None,
        user_data: dict[str, Any] | None = None,
    ) -> None:
        self.args = args or []
        self.bot_data = bot_data if bot_data is not None else {}
        # Per-Telegram-user scratch space, as python-telegram-bot provides.
        self.user_data = user_data if user_data is not None else {}
        self.bot = bot or FakeBot()
        self.error: BaseException | None = None


@pytest.fixture
def backend():
    from tests.fake_backend import make_backend

    return make_backend()


@pytest.fixture
def api_client(backend):
    import httpx

    from bot.client import ApiClient

    transport = httpx.ASGITransport(app=backend.app)
    client = ApiClient(
        base_url="http://testserver/api/v1",
        token="test-service-token",
        transport=transport,
        max_retries=2,
        backoff=0.0,
    )
    yield client
