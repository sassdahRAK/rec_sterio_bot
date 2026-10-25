"""Entrypoint: `python -m bot`."""

from __future__ import annotations

import asyncio
import logging
import sys

from telegram.constants import ParseMode
from telegram.ext import Application, Defaults

from .client import ApiClient
from .config import Settings, get_settings
from .handlers import register_handlers
from .handlers.links import WatchRegistry

logger = logging.getLogger(__name__)

__all__ = ["build_application", "main"]


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logger.info("starting recsterio bot (api=%s)", settings.bot_api_base_url)


def build_application(settings: Settings | None = None) -> Application:
    """Build the PTB application, wiring the API client into ``bot_data``."""
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Export it (e.g. in your .env) before "
            "starting the bot — the token is never read from source."
        )

    defaults = Defaults(parse_mode=ParseMode.MARKDOWN, block=True)
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .defaults(defaults)
        .build()
    )
    register_handlers(app)

    async def post_init(application: Application) -> None:
        client = ApiClient(
            base_url=settings.bot_api_base_url,
            token=settings.bot_api_token,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            backoff=settings.retry_backoff_seconds,
        )
        application.bot_data["api"] = client
        application.bot_data["settings"] = settings
        application.bot_data["watchers"] = WatchRegistry()
        application.bot_data["merge_watchers"] = WatchRegistry()
        logger.info("bot ready")

    async def post_shutdown(application: Application) -> None:
        for key in ("watchers", "merge_watchers"):
            watchers = application.bot_data.get(key)
            if watchers is not None:
                await watchers.cancel_all()
        client = application.bot_data.get("api")
        if client is not None:
            await client.aclose()

    app.post_init = post_init
    app.post_shutdown = post_shutdown
    return app


def main() -> int:
    _configure_logging(get_settings())
    app = build_application()
    # Python 3.10+ requires an explicit event loop before run_polling
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
