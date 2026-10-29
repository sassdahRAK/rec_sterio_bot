"""Entrypoint: `python -m bot`."""

from __future__ import annotations

import asyncio
import logging
import sys

from telegram.ext import Application, Defaults
from telegram.request import HTTPXRequest

from .client import ApiClient
from .config import Settings, get_settings
from .handlers import register_handlers
from .handlers.links import WatchRegistry
from .helpers import AdminIdsCache

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


async def _admin_ids_loop(
    cache: AdminIdsCache, client: ApiClient, settings: Settings
) -> None:
    """Keep the linked-admin id cache fresh: refresh now, then every interval.

    A plain asyncio task, not PTB's JobQueue — the job-queue extra (APScheduler)
    is not installed. The refresh itself also swallows API errors (the cache
    keeps its previous set); this belt-and-braces except keeps the loop alive
    even if something unexpected escapes.
    """
    while True:
        try:
            await cache.refresh(client)
        except Exception:  # the refresher must never die
            logger.warning("admin-ids refresh crashed; retrying next interval", exc_info=True)
        await asyncio.sleep(settings.admin_ids_refresh_minutes * 60)


def build_application(settings: Settings | None = None) -> Application:
    """Build the PTB application, wiring the API client into ``bot_data``."""
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Export it (e.g. in your .env) before "
            "starting the bot — the token is never read from source."
        )

    # No global parse_mode on purpose: messages are plain text unless the call
    # site opts into Markdown explicitly. A global default made every message
    # parse its content, so a stray '_' or '*' from a title, an error code or a
    # usage string raised "Can't parse entities" and the user got nothing.
    defaults = Defaults(block=True)
    # Media uploads from this network (Phnom Penh → Telegram) are slow: a
    # measured 13.5 MB upload took 172 s. The previous 60 s media timeout made
    # every `/media` of a real clip fail with `Timed out`, so a 300 s budget is
    # what actually fits (~23 MB at that rate). A file too big for even that
    # ends in the "send it from the portal" reply, not a hang.
    # PTB 21.11 notes: the connect param is `connect_timeout`; file uploads use
    # `media_write_timeout`; getUpdates adds the 10s long-poll to read_timeout.
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=180.0,
        write_timeout=60.0,
        pool_timeout=30.0,
        media_write_timeout=300.0,
    )
    get_updates_request = HTTPXRequest(connect_timeout=30.0, read_timeout=10.0)
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .defaults(defaults)
        .request(request)
        .get_updates_request(get_updates_request)
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
        cache = AdminIdsCache()
        application.bot_data["admin_ids_cache"] = cache
        # Refresh immediately, then on the configured interval; post_shutdown
        # cancels the task (mirroring WatchRegistry.cancel_all's cancel+await).
        application.bot_data["admin_ids_task"] = asyncio.ensure_future(
            _admin_ids_loop(cache, client, settings)
        )
        logger.info("bot ready")

    async def post_shutdown(application: Application) -> None:
        task = application.bot_data.get("admin_ids_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
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
