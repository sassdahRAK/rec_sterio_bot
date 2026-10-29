"""One tap, one job.

Telegram delivers a callback query for *every* tap, and PTB runs update
handlers concurrently, so a double-tapped inline button starts two deliveries
of the same file. The second one is pure waste: the same bytes are fetched
from the API and uploaded to the same chat again.

This is the gate the callback dispatcher checks before starting heavy work. It
mirrors `links.WatchRegistry` — an in-memory set keyed by what the work
targets — and lives in `bot_data` so every handler sees the same one.

Deliberately in-memory: the bot is a single process, and a restart clearing the
set costs at most one repeated delivery, never a lost request.
"""
from __future__ import annotations

import asyncio
from typing import Any


class InFlight:
    """The callback actions currently running, keyed by their target."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(self, key: str) -> bool:
        """Take `key`. False means somebody is already working on it."""
        async with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    async def release(self, key: str) -> None:
        async with self._lock:
            self._active.discard(key)

    def busy(self, key: str) -> bool:
        return key in self._active

    def clear(self) -> None:
        self._active.clear()


def get_inflight(context: Any) -> InFlight:
    """The shared guard, created on first use (same pattern as the watchers)."""
    bot_data = context.bot_data if context.bot_data is not None else {}
    guard: InFlight | None = bot_data.get("inflight")
    if guard is None:
        guard = InFlight()
        context.bot_data["inflight"] = guard
    return guard
