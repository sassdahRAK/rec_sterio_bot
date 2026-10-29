"""Inline keyboard builders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

__all__ = [
    "cookies_keyboard",
    "help_keyboard",
    "jobs_keyboard",
    "merge_keyboard",
    "merge_list_keyboard",
    "transcript_versions_keyboard",
    "video_keyboard",
]

MERGE_STATUS_ICON = {
    "pending": "⏳",
    "running": "⚙️",
    "ready": "✅",
    "failed": "❌",
}


def merge_keyboard(video_id: str, merge_id: str | None = None, *, ready: bool = False) -> InlineKeyboardMarkup:
    """Actions for one merge: apply when ready, otherwise re-check / start one."""
    rows: list[list[InlineKeyboardButton]] = []
    if ready and merge_id:
        rows.append(
            [InlineKeyboardButton("Apply merged transcript", callback_data=f"apply:{merge_id}")]
        )
        rows.append(
            [
                InlineKeyboardButton("Check again", callback_data=f"mcheck:{merge_id}"),
                InlineKeyboardButton("List merges", callback_data=f"mlist:{video_id}"),
            ]
        )
    else:
        rows.append(
            [InlineKeyboardButton("Start merge", callback_data=f"merge:{video_id}")]
        )
        rows.append(
            [InlineKeyboardButton("Transcripts", callback_data=f"transcripts:{video_id}")]
        )
    return InlineKeyboardMarkup(rows)


def merge_list_keyboard(video_id: str, items: Iterable[Mapping[str, Any]]) -> InlineKeyboardMarkup:
    """One row per merge — apply the ready ones, re-check the rest."""
    rows: list[list[InlineKeyboardButton]] = []
    for item in list(items)[:10]:
        merge_id = str(item.get("id") or "")
        if not merge_id:
            continue
        status = str(item.get("status") or "?")
        icon = MERGE_STATUS_ICON.get(status, "•")
        if status == "ready":
            label = f"{icon} Apply {merge_id[:8]}"
            rows.append([InlineKeyboardButton(label, callback_data=f"apply:{merge_id}")])
        else:
            label = f"{icon} {status} {merge_id[:8]}"
            rows.append([InlineKeyboardButton(label, callback_data=f"mcheck:{merge_id}")])
    rows.append([InlineKeyboardButton("New merge", callback_data=f"merge:{video_id}")])
    return InlineKeyboardMarkup(rows)


def video_keyboard(video_id: str) -> InlineKeyboardMarkup:
    """Buttons for a freshly created / tracked video."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Status", callback_data=f"status:{video_id}"),
                InlineKeyboardButton("Transcript", callback_data=f"transcript:{video_id}"),
            ],
            [
                InlineKeyboardButton("Media", callback_data=f"media:{video_id}"),
                InlineKeyboardButton("Audio", callback_data=f"audio:{video_id}"),
            ],
        ]
    )


def transcript_versions_keyboard(
    video_id: str, items: Iterable[Mapping[str, Any]]
) -> InlineKeyboardMarkup:
    """One button per transcript version (newest first)."""
    rows: list[list[InlineKeyboardButton]] = []
    for item in list(items)[:10]:
        version = item.get("version", "?")
        source = item.get("source", "?")
        rows.append(
            [
                InlineKeyboardButton(
                    f"v{version} ({source})", callback_data=f"tr:{video_id}:{version}"
                )
            ]
        )
    rows.append([InlineKeyboardButton("Status", callback_data=f"status:{video_id}")])
    return InlineKeyboardMarkup(rows)


def jobs_keyboard() -> InlineKeyboardMarkup:
    """Refresh / failure-filter shortcuts for the admin jobs view."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Refresh", callback_data="jobs:refresh"),
                InlineKeyboardButton("Failed only", callback_data="jobs:failed"),
            ]
        ]
    )


def cookies_keyboard() -> InlineKeyboardMarkup:
    """The /cookies consent gate: agree, or remove the stored data."""
    return rows_to_markup(
        [[("I agree", "cookies:agree"), ("Remove my data", "cookies:remove")]]
    )


def help_keyboard() -> InlineKeyboardMarkup:
    """Shortcut buttons shown under /help."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Send a video link", callback_data="help:link")]]
    )


def rows_to_markup(rows: Sequence[Sequence[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Small helper to build a markup from (label, data) pairs."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )
