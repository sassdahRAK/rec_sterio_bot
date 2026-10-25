"""Runtime configuration, sourced exclusively from the environment.

Secrets (TELEGRAM_BOT_TOKEN, BOT_API_TOKEN) are never logged and never
embedded in source.  The bot must remain importable when the token is
missing — validation happens in the entrypoint, at startup, not at import.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from env vars / a local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", description="Bot token from @BotFather.")

    # --- Backend API ---
    bot_api_token: str = Field(default="", description="Service token (Authorization: Bearer ...).")
    bot_api_base_url: str = Field(
        default="http://localhost:8000/api/v1",
        description="Base URL of the backend API, including the /api/v1 prefix.",
    )

    # --- Admin allow-list (comma separated Telegram user ids) ---
    admin_telegram_ids: str = Field(default="", description="Comma separated Telegram user ids allowed to use admin commands.")

    # --- Tunables (all optional, sane defaults) ---
    request_timeout_seconds: float = Field(default=15.0, ge=1.0)
    poll_interval_seconds: float = Field(default=5.0, gt=0.0)
    poll_timeout_seconds: float = Field(default=1200.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=0.5, ge=0.0)
    transcript_text_limit: int = Field(default=3000, ge=256)

    @property
    def admin_ids(self) -> frozenset[int]:
        """Parsed allow-list of admin Telegram user ids."""
        ids: set[int] = set()
        for chunk in self.admin_telegram_ids.replace(" ", "").split(","):
            if not chunk:
                continue
            try:
                ids.add(int(chunk))
            except ValueError:
                continue
        return frozenset(ids)

    @property
    def media_base_url(self) -> str:
        """Base URL used to build user-facing media links."""
        return self.bot_api_base_url.rstrip("/")

    def is_admin(self, telegram_user_id: int | None) -> bool:
        if telegram_user_id is None:
            return False
        return telegram_user_id in self.admin_ids

    def public_dict(self) -> dict[str, object]:
        """Config snapshot safe to log — secrets are redacted."""
        return {
            "bot_api_base_url": self.bot_api_base_url,
            "telegram_bot_token": "***" if self.telegram_bot_token else "",
            "bot_api_token": "***" if self.bot_api_token else "",
            "admin_telegram_ids": sorted(self.admin_ids),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by the test-suite)."""
    get_settings.cache_clear()
