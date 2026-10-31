"""Feature 4 from Telegram: `/merge`, `/merges`, `/apply` and their buttons.

A merge reconciles the local Whisper transcript with an independent Gemini
transcript. The backend does the alignment; the bot's job is to start it, follow
it, and let the user apply the result — never to edit transcripts itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import ApiClient, ApiError, ApiNetworkError
from ..helpers import (
    BOT_NOT_READY,
    actor_for,
    escape_markdown,
    get_client,
    is_uuid,
    portal_token_for,
    web_links,
)
from ..inflight import get_inflight
from ..keyboards import merge_keyboard, merge_list_keyboard
from . import media as media_handler
from . import status as status_handler
from . import transcript as transcript_handler
from .links import WatchRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "apply_command",
    "merge_command",
    "merges_command",
    "on_callback",
]

MERGE_USAGE = "Usage: /merge <video id> [model]"
APPLY_USAGE = "Usage: /apply <merge id>"
MERGES_USAGE = "Usage: /merges <video id>"

TERMINAL_MERGE_STATUSES = frozenset({"ready", "failed"})

#: How long the bot waits for Gemini's transcript before giving up (seconds).
GEMINI_WAIT_SECONDS = 600.0
#: How long the bot follows a single merge job before giving up.
MERGE_WAIT_SECONDS = 900.0


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []


def _ready(transcripts: list[Any], source: str | None = None) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for t in transcripts:
        if not isinstance(t, Mapping) or t.get("status") != "ready":
            continue
        if source is not None and t.get("source") != source:
            continue
        out.append(t)
    return out


def pick_transcripts(
    transcripts: list[Any],
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Return ``(base, gemini)`` — the best base transcript and the newest Gemini one.

    Base prefers a Whisper transcript, falling back to any ready one (manual or a
    previous merge) so re-running a merge after an edit stays useful.
    """
    ready = _ready(transcripts)
    gemini = _ready(transcripts, "gemini")
    whisper = _ready(transcripts, "whisper")
    base: Mapping[str, Any] | None = None
    if whisper:
        base = whisper[0]
    elif ready:
        base = ready[0]
    return base, (gemini[0] if gemini else None)


def format_merge(merge: Mapping[str, Any]) -> str:
    """Render a merge object, including the diff summary when it is ready."""
    status = str(merge.get("status") or "?")
    merge_id = str(merge.get("id") or "?")
    lines = [f"*Merge* `{merge_id[:8]}`: `{status}`"]
    if merge.get("model"):
        lines.append(f"Model: `{escape_markdown(str(merge['model']))}`")

    diff = merge.get("diff")
    if isinstance(diff, Mapping):
        stats = diff.get("stats") or {}
        if isinstance(stats, Mapping) and stats:
            lines.append(
                "Segments: {out} output · {base} from {base_label} · "
                "{cand} from {cand_label} · {flagged} flagged".format(
                    out=stats.get("output_segments", "?"),
                    base=stats.get("from_base", "?"),
                    base_label=stats.get("base_label", "base"),
                    cand=stats.get("from_candidate", "?"),
                    cand_label=stats.get("candidate_label", "candidate"),
                    flagged=stats.get("flagged", "?"),
                )
            )
            if stats.get("mean_agreement") is not None:
                lines.append(f"Mean agreement: {stats['mean_agreement']}")
        summary = diff.get("summary")
        if summary:
            lines.append(escape_markdown(str(summary)))

        segments = diff.get("segments") or []
        if isinstance(segments, list) and segments:
            preview = []
            for seg in segments[:5]:
                if not isinstance(seg, Mapping):
                    continue
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                preview.append(
                    f"· [{seg.get('source', '?')}] {escape_markdown(text[:120])}"
                )
            if preview:
                lines.append("*Reconciled text*")
                lines.extend(preview)
            if len(segments) > 5:
                lines.append(f"…and {len(segments) - 5} more segment(s).")

    if merge.get("error"):
        lines.append(f"Error: {escape_markdown(str(merge['error']))}")
    return "\n".join(lines)


def _merge_id_of(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        for key in ("id", "merge_id"):
            if payload.get(key):
                return str(payload[key])
        if isinstance(payload.get("merge"), Mapping):
            return _merge_id_of(payload["merge"])
    return None


async def _safe_send(bot: Any, chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a notify failure must not kill the task
        logger.warning("could not notify chat %s: %s", chat_id, exc)


def _trackers(context: ContextTypes.DEFAULT_TYPE):
    """The merge watcher registry, created lazily for tests and older setups."""
    bot_data = context.bot_data
    if bot_data is None:
        return None
    registry = bot_data.get("merge_watchers")
    if registry is None:
        registry = WatchRegistry()
        bot_data["merge_watchers"] = registry
    return registry


async def _watch_or_run(
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
    coro: Awaitable[Any],
    message: Any,
) -> None:
    """Run a merge task under the registry so one key is never watched twice."""
    trackers = _trackers(context)
    if trackers is None:  # pragma: no cover - no bot_data (direct handler call)
        await coro
        return
    if not trackers.start(key, coro):
        await message.reply_text("That merge is already being watched, so hang tight.")


async def watch_merge(
    client: ApiClient,
    bot: Any,
    chat_id: int,
    merge_id: str,
    *,
    requested_by: str | None = None,
    interval: float = 5.0,
    timeout: float = MERGE_WAIT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll a merge until it settles, then report it with an Apply button.

    *requested_by* is the Telegram user who started the merge; every poll
    carries it, so the backend evaluates that user's access to the merge's
    video instead of the bot's service identity.
    """
    deadline = clock() + timeout
    status = "unknown"
    while clock() < deadline:
        try:
            merge = await client.get_merge(merge_id, requested_by=requested_by)
        except ApiError as exc:
            await _safe_send(
                bot, chat_id, f"Could not check merge: {exc.user_message()}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return f"error:{exc.code}"
        except ApiNetworkError as exc:
            await _safe_send(
                bot,
                chat_id,
                f"Lost contact with the backend: {exc.user_message()}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return "error:network_error"

        status = str(merge.get("status") or "unknown")
        if status in TERMINAL_MERGE_STATUSES:
            text = format_merge(merge)
            video_id = str(merge.get("video_id") or "")
            await _safe_send(
                bot,
                chat_id,
                f"{text}\n\n"
                + (
                    "Apply it with the button below, or /apply "
                    f"{merge_id[:8] if status == 'ready' else merge_id}."
                    if status == "ready"
                    else "Nothing was applied. Fix the cause and run /merge again."
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=merge_keyboard(video_id, merge_id, ready=status == "ready"),
            )
            return status
        await sleep(interval)

    await _safe_send(
        bot,
        chat_id,
        f"Merge `{merge_id[:8]}` is still `{status}`. Check with /merges "
        f"{'(video id)'} or the dashboard.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return "timeout"


async def watch_gemini_then_merge(
    client: ApiClient,
    bot: Any,
    chat_id: int,
    video_id: str,
    *,
    requested_by: str | None = None,
    interval: float = 5.0,
    timeout: float = GEMINI_WAIT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Wait for Gemini's transcript, then start the merge automatically."""
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            video = await client.get_video(video_id, requested_by=requested_by)
        except ApiError as exc:
            await _safe_send(
                bot, chat_id, f"Could not check video: {exc.user_message()}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return f"error:{exc.code}"
        except ApiNetworkError as exc:
            await _safe_send(
                bot,
                chat_id,
                f"Lost contact with the backend: {exc.user_message()}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return "error:network_error"

        if str(video.get("status")) == "failed":
            await _safe_send(
                bot,
                chat_id,
                "Gemini transcription failed, so there is nothing to merge. "
                f"Error: {escape_markdown(str(video.get('error') or 'unknown'))}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return "failed"

        base, gemini = pick_transcripts(list(video.get("transcripts") or []))
        if base is not None and gemini is not None:
            try:
                created = await client.create_merge(
                    video_id,
                    base_transcript_id=str(base.get("id") or "") or None,
                    gemini_transcript_id=str(gemini.get("id") or "") or None,
                    requested_by=requested_by,
                )
            except ApiError as exc:
                await _safe_send(
                    bot, chat_id, f"Could not start the merge: {exc.user_message()}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return f"error:{exc.code}"
            except ApiNetworkError as exc:
                await _safe_send(
                    bot,
                    chat_id,
                    f"Lost contact with the backend: {exc.user_message()}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return "error:network_error"

            merge_id = _merge_id_of(created)
            if not merge_id:
                await _safe_send(bot, chat_id, "The merge was accepted but returned no id.")
                return "error:no_id"
            await _safe_send(
                bot,
                chat_id,
                f"Gemini transcript ready. Merge `{merge_id[:8]}` queued.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return await watch_merge(
                client,
                bot,
                chat_id,
                merge_id,
                requested_by=requested_by,
                interval=interval,
            )

        await sleep(interval)

    await _safe_send(
        bot,
        chat_id,
        f"Gemini transcript for `{video_id}` is still not ready. "
        f"Try /merge {video_id} again in a few minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return "timeout"


async def merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a Whisper×Gemini merge for a video."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(MERGE_USAGE)
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(
            f"`{video_id}` is not a valid video id.\n{MERGE_USAGE}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    model = args[1].strip() if len(args) > 1 else None

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    # Every call below names the requesting user, so the backend evaluates
    # their access to the video rather than the bot's service identity.
    requested_by = actor_for(update)
    try:
        video = await client.get_video(video_id, requested_by=requested_by)
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    transcripts = list(video.get("transcripts") or [])
    base, gemini = pick_transcripts(transcripts)

    if base is None:
        await message.reply_text(
            f"Video `{video_id}` has no ready transcript yet, so there is nothing to "
            f"merge.\nRun /transcript {video_id} once transcription finishes, or ask an "
            "admin to start it from the dashboard.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if gemini is None:
        try:
            await client.generate_transcript(
                video_id, "gemini", requested_by=requested_by
            )
        except ApiError as exc:
            await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
            return
        except ApiNetworkError as exc:
            await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
            return

        await message.reply_text(
            f"No Gemini transcript yet for `{video_id}`, so I queued it now.\n"
            "I will start the merge automatically as soon as it is ready.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _watch_or_run(
            context,
            f"gemini:{video_id}",
            watch_gemini_then_merge(
                client, context.bot, message.chat_id, video_id, requested_by=requested_by
            ),
            message,
        )
        return

    try:
        created = await client.create_merge(
            video_id,
            base_transcript_id=str(base.get("id") or "") or None,
            gemini_transcript_id=str(gemini.get("id") or "") or None,
            model=model,
            requested_by=requested_by,
        )
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    merge_id = _merge_id_of(created)
    if not merge_id:
        await message.reply_text("[unexpected response] The merge returned no id.")
        return

    await message.reply_text(
        f"Merge `{merge_id[:8]}` queued using v{base.get('version', '?')} "
        f"({base.get('source', '?')}) as the base.\n"
        "I will report the diff when it finishes.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _watch_or_run(
        context,
        f"merge:{merge_id}",
        watch_merge(
            client,
            context.bot,
            message.chat_id,
            merge_id,
            requested_by=requested_by,
        ),
        message,
    )


async def merges_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List merges for a video."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(MERGES_USAGE)
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(
            f"`{video_id}` is not a valid video id.\n{MERGES_USAGE}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    try:
        payload = await client.list_merges(
            video_id, requested_by=actor_for(update)
        )
    except ApiError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    items = _items(payload)
    if not items:
        await message.reply_text(
            f"No merges for `{video_id}` yet.\nStart one with /merge {video_id}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"*Merges* for `{video_id}`: {len(items)} total"]
    for item in items:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            "- `{id}` · {status} · {model} · {created}".format(
                id=str(item.get("id") or "?")[:8],
                status=escape_markdown(str(item.get("status", "?"))),
                model=escape_markdown(str(item.get("model") or "?")),
                created=item.get("created_at", "?"),
            )
        )
    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=merge_list_keyboard(video_id, items),
    )


async def _apply(client: ApiClient, merge_id: str, requested_by: str | None) -> str:
    transcript = await client.apply_merge(merge_id, requested_by=requested_by)
    video_id = str(transcript.get("video_id") or "")
    read_hint = f"\nRead it with /transcript {video_id}" if video_id else ""
    return (
        f"✅ Applied merge `{merge_id[:8]}`.\n"
        f"Current transcript is now v{transcript.get('version', '?')} "
        f"(source={transcript.get('source', 'merged')})."
        f"{read_hint}"
    )


async def apply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Promote a ready merge to the current transcript."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(APPLY_USAGE)
        return
    merge_id = args[0].strip()
    if not is_uuid(merge_id):
        await message.reply_text(
            f"`{merge_id}` is not a valid merge id.\n{APPLY_USAGE}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    try:
        text = await _apply(client, merge_id, actor_for(update))
    except ApiError as exc:
        if exc.status_code == 409:
            await message.reply_text(
                f"Merge `{merge_id[:8]}` is not ready yet: {exc.user_message()}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


#: Actions that ship bytes to the chat or write a row. A second tap while one
#: is still running would repeat the whole fetch/upload — see `bot.inflight`.
GUARDED_ACTIONS = frozenset({"media", "audio", "transcript", "apply", "merge", "mcheck", "mlist"})


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle merge-related inline buttons."""
    query = update.callback_query
    if query is None:
        return
    data = str(query.data or "")
    action, _, arg = data.partition(":")

    try:
        client = get_client(context)
    except RuntimeError:
        await query.answer(
            text="That did not work, so please try again.", show_alert=True
        )
        return

    # A double-tapped button sends two callbacks; the second must not fetch and
    # upload the same file again. Keyed per chat so two different users asking
    # for the same video are not made to wait for each other.
    guard = get_inflight(context)
    key = ""
    if action in GUARDED_ACTIONS:
        key = f"{action}:{query.message.chat_id if query.message else 0}:{arg}"
        if not await guard.claim(key):
            await query.answer(
                text="Already working on that. One moment.", show_alert=False
            )
            return

    try:
        await _dispatch(update, query, context, client, action, arg)
    finally:
        if key:
            await guard.release(key)


async def _dispatch(
    update: Update,
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    client: Any,
    action: str,
    arg: str,
) -> None:
    """The button actions themselves, with the double-tap guard already held."""
    try:
        if action == "media" and arg:
            # The media endpoint is authenticated, so the bot fetches the bytes
            # with its own token and uploads them to the chat.
            await query.answer("Fetching the media…")
            # Same attribution as /media: the video read and the token mint
            # both name this Telegram user, so a 403 withholds the link.
            requested_by = actor_for(update)
            try:
                video = await client.get_video(arg, requested_by=requested_by)
            except (ApiError, ApiNetworkError):
                video = {}
            portal = await portal_token_for(
                client, arg, requested_by=requested_by
            )
            await media_handler.deliver_media(
                context.bot,
                query.message.chat_id,
                client,
                arg,
                video=video,
                requested_by=requested_by,
                reply_markup=web_links(
                    update,
                    context,
                    video=video,
                    video_id=arg,
                    token=portal.token,
                    allow_portal=portal.linkable(confirmed=bool(video)),
                ),
            )
            return

        if action == "audio" and arg:
            # Same flow as /audio: the command replies through
            # update.effective_message, which a callback update also carries.
            await query.answer("Fetching the audio…")
            context.args = [arg]
            await media_handler.audio_command(update, context)
            return

        if action == "status" and arg:
            await query.answer()
            context.args = [arg]
            await status_handler.status_command(update, context)
            return

        if action == "transcript" and arg:
            await query.answer()
            context.args = [arg]
            await transcript_handler.transcript_command(update, context)
            return

        if action == "apply" and arg:
            try:
                text = await _apply(client, arg, actor_for(update))
            except ApiError as exc:
                await query.answer(
                    text=f"Not applied: {exc.message}"[:180], show_alert=True
                )
                return
            except ApiNetworkError as exc:
                await query.answer(text=exc.message[:180], show_alert=True)
                return
            await query.answer(text="Applied")
            if query.message is not None:
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
            return

        if action == "mcheck" and arg:
            # The button was tapped by a Telegram user, so the merge read names
            # them: the backend evaluates their access to the merge's video.
            merge = await client.get_merge(arg, requested_by=actor_for(update))
            await query.answer()
            if query.message is not None:
                await query.message.reply_text(
                    format_merge(merge),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=merge_keyboard(
                        str(merge.get("video_id") or ""),
                        arg,
                        ready=str(merge.get("status")) == "ready",
                    ),
                )
            return

        if action == "mlist" and arg:
            await query.answer()
            payload = await client.list_merges(
                arg, requested_by=actor_for(update)
            )
            items = _items(payload)
            if query.message is not None:
                await query.message.reply_text(
                    f"{len(items)} merge(s) for `{arg}`.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=merge_list_keyboard(arg, items),
                )
            return

        if action == "merge" and arg:
            await query.answer()
            context.args = [arg]
            await merge_command(update, context)
            return

        await query.answer(text="That button is no longer wired up.", show_alert=False)
    except ApiError as exc:
        await query.answer(text=exc.message[:180], show_alert=True)
    except ApiNetworkError as exc:
        await query.answer(text=exc.message[:180], show_alert=True)
