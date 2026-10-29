"""`/media <id>` — deliver the downloaded video to the Telegram chat.

The media endpoint is authenticated, so a plain URL handed to the user would
just return 401 in their browser. Instead the bot fetches the bytes through
its own service token and uploads them to the chat.

`/audio <id>` does the same for the stored mp3 — request-only, never
auto-delivered.
"""
from __future__ import annotations

import logging
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..client import (
    ApiError,
    ApiNetworkError,
    CompressedNotReadyError,
    OversizeMediaError,
)
from ..helpers import (
    BOT_NOT_READY,
    escape_markdown,
    format_bytes,
    get_client,
    get_settings_or_default,
    is_uuid,
    portal_button,
    portal_url,
    web_links,
)

logger = logging.getLogger(__name__)

__all__ = ["audio_command", "deliver_media", "media_command"]

USAGE = "Usage: /media <video id>  (the id shown when you queued the link)"
AUDIO_USAGE = "Usage: /audio <video id>  (the id shown when you queued the link)"

# Telegram's Bot API upload limit for bots is 50 MB.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Telegram's Bot API `getFile` ceiling for bots: 20 MB. Re-sending an existing
# copy by file_id has no limit; fetching its bytes back to the server does.
MAX_REFETCH_BYTES = 20 * 1024 * 1024
# The backend keeps a smaller compressed copy (h264 mp4, inside the upload
# limit) of every unit that is over it; the media endpoint serves it on
# `?variant=compressed` while the artifact exists.
COMPRESSED_VARIANT = "compressed"
# The one reply for an oversize unit whose compressed copy is still being
# made. Kept clear of _explain_too_large's prose: that one is the final
# "get it from the portal" answer; this is the interim "ask me again" one.
_PREPARING_COPY_TEXT = (
    "That clip is larger than Telegram allows. I've started preparing a smaller "
    "copy — ask for /media again in a minute."
)


def _filename(video: Mapping[str, Any], video_id: str) -> str:
    """Build the upload filename: sanitized title plus the recorded extension.

    The backend returns ``local_path`` as a basename (``media.mp4``,
    ``media.webm``), so its suffix is the real extension; fall back to mp4
    when there is none. A title that already ends with that extension keeps
    it as is instead of stacking a second one (``clip.mp4`` stays
    ``clip.mp4``).
    """
    ext = Path(str(video.get("local_path") or "")).suffix or ".mp4"
    title = str(video.get("title") or "").strip()
    safe = "".join(ch for ch in title if ch.isalnum() or ch in " ._-")[:80].strip()
    name = safe or video_id
    if name.lower().endswith(ext.lower()):
        return name
    return f"{name}{ext}"


def _explain_missing_media(video: Mapping[str, Any], video_id: str) -> str:
    """Explain why there is no file yet, in terms the user can act on."""
    status = str(video.get("status") or "") if isinstance(video, Mapping) else ""

    if status in {"pending", "downloading"}:
        progress = video.get("progress")
        pct = f" ({progress}%)" if isinstance(progress, int) else ""
        return (
            f"⏳ The download is still in progress{pct} — the file is not on disk yet.\n"
            f"Video `{video_id}`. Try /media again in a moment, or /status {video_id}."
        )

    if status in {"failed", "quarantined"}:
        error = str(video.get("error") or "").strip()
        lines = [f"❌ The download failed for video `{video_id}`, so there is no file to send."]
        if error:
            lines.append(f"Reason: {escape_markdown(error[:400])}")
        if "tiktok" in error.lower() or "unexpected response" in error.lower():
            lines.append(
                "This is TikTok's bot check, not a problem with your link. "
                "TikTok now refuses most server-side requests."
            )
        elif "unavailable" in error.lower() or "private" in error.lower():
            lines.append(
                "The video looks unavailable, private, or region-locked. "
                "Only public content that you have the rights to can be fetched."
            )
        lines.append("Ask an admin to retry with /retry, or send a different public link.")
        return "\n\n".join(lines)

    if status == "transcribing":
        return (
            f"⏳ The media for `{video_id}` is being transcribed. The file itself "
            f"should already exist — try /media again, or /status {video_id}."
        )

    text = (
        f"The media for `{video_id}` is not on disk"
        + (f" (status: `{status}`)" if status else "")
        + ". Check /status for details."
    )
    if isinstance(video, Mapping) and video.get("telegram_file_id"):
        # A copy was recorded earlier, so a re-send was attempted — and it
        # failed. Say so instead of implying nothing is left of the video.
        text += "\nThe Telegram copy exists but re-sending it failed."
    return text


def _portal_link_markup(video_id: str) -> InlineKeyboardMarkup | None:
    """A one-button keyboard pointing at the unit's portal page, if possible."""
    button = portal_button(video_id)
    return InlineKeyboardMarkup([[button]]) if button is not None else None


async def _explain_too_large(
    bot: Any,
    chat_id: int,
    video_id: str,
    size: int | None,
    reply_markup: InlineKeyboardMarkup | None,
    video: Mapping[str, Any] | None = None,
) -> None:
    """Say why the file is not coming, and put the portal page in reach.

    Telegram's Bot API refuses uploads over 50 MB, so a large unit can never
    arrive as a chat file. The portal can play and download it, so the reply
    carries that page as a button **and** spells the address out in the text
    (a link stays usable in a client that does not render the button, and can
    be copied). Only when the frontend is configured at a loopback address —
    which Telegram refuses in a button — does it fall back to naming the
    console and the id instead of promising a link that is not there.
    """
    markup = reply_markup or _portal_link_markup(video_id)
    lead = (
        f"The file is {format_bytes(size)} — too large for Telegram to carry "
        f"(a bot can upload at most {format_bytes(MAX_UPLOAD_BYTES)})."
        if size is not None
        else f"This file is too large for Telegram to carry (a bot can upload at most "
        f"{format_bytes(MAX_UPLOAD_BYTES)})."
    )
    settings = get_settings_or_default()
    lines = [lead, _portal_fallback(settings, video_id, video)]
    lines.append(f"Video `{video_id}`.")
    await bot.send_message(
        chat_id=chat_id,
        text="\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=markup,
    )


def _portal_fallback(
    settings: Any, video_id: str, video: Mapping[str, Any] | None = None
) -> str:
    """The "where to get it instead" sentence, with the real address in it.

    An unfiled unit is in no folder, so it is not part of the public dataset
    and the portal shows it only to its creator (or an admin) — the visitor
    must sign in first. Saying so beats a link that answers "isn't available".
    """
    portal = portal_url(video_id, settings)
    if not portal:
        return f"An admin can download it from the console (`{video_id}`)."
    text = f"Open it in the portal — watch or download it there:\n{portal}"
    if isinstance(video, Mapping) and not video.get("folder_id"):
        text += "\nIt is not in a folder yet, so sign in as the account that owns it."
    return text


async def _explain_upload_failed(
    bot: Any,
    chat_id: int,
    video_id: str,
    exc: BaseException,
    reply_markup: InlineKeyboardMarkup | None,
    video: Mapping[str, Any] | None = None,
) -> None:
    """A failed Telegram transfer still gets an explanation and the way in.

    An upload can fail for reasons that are not the file's size — the link to
    Telegram is slow (a 13.5 MB clip measured 172 s), a phone dropped the
    connection, or Telegram answered 5xx. The unit itself is fine, so the
    reply points at the portal page where it can be watched or downloaded
    instead of leaving a bare error string.
    """
    markup = reply_markup or _portal_link_markup(video_id)
    timed_out = "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower()
    lead = (
        "The upload to Telegram timed out."
        if timed_out
        else "Telegram refused the upload."
    )
    lines = [
        f"{lead} The video itself is fine — this network's link to Telegram "
        "is what failed.",
        _portal_fallback(get_settings_or_default(), video_id, video),
        f"Video `{video_id}`.",
    ]
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="\n\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
    except Exception:  # noqa: BLE001 - the notice itself must never raise
        logger.warning("could not send the upload-failure notice for %s", video_id)


async def _send_document_and_record(
    bot: Any,
    chat_id: int,
    client: Any,
    video_id: str,
    video: Mapping[str, Any] | None,
    payload: bytes,
    refetch_size: int | None,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Send *payload* as a document and record the copy Telegram stores.

    The shared tail of both delivery paths: the oversized unit's compressed
    copy and the ordinary in-limit original arrive the same way, so the
    caption, the ``getFile``-ceiling hint (for ``refetch_size``, the size of
    the stored copy) and the post-send ``POST /videos/{id}/telegram`` record
    live here. A Telegram 413 stays as the last-resort safety net for a
    size that lied.
    """
    filename = _filename(video, video_id) if isinstance(video, Mapping) else f"{video_id}.mp4"
    caption = f"Video `{video_id}` — {format_bytes(len(payload))}"
    if isinstance(refetch_size, int) and refetch_size > MAX_REFETCH_BYTES:
        # Past Telegram's 20 MB `getFile` ceiling: the copy can be re-sent to
        # any chat, but the bot can never download those bytes back.
        caption += (
            " — Telegram can re-send this copy, but the bot cannot"
            " fetch it back (20 MB limit)"
        )
    try:
        sent = await bot.send_document(
            chat_id=chat_id,
            document=payload,
            filename=filename,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
        )
    except Exception as exc:  # noqa: BLE001 - a failed upload must not crash the bot
        logger.warning("could not upload media for %s: %s", video_id, exc)
        reason = str(exc).lower()
        if "too large" in reason or "413" in reason:
            # `file_size` is not always recorded, so the pre-check above can
            # miss a big file and only Telegram's own limit catches it.
            await _explain_too_large(
                bot, chat_id, video_id, len(payload), reply_markup, video
            )
            return False
        # Slow link, a dropped connection or a Telegram 5xx — the unit is fine,
        # so say what failed and hand over the portal page.
        await _explain_upload_failed(bot, chat_id, video_id, exc, reply_markup, video)
        return False

    # instruction.txt #4: link the Telegram copy back to the catalogue. Only a
    # file under the Bot API's 20 MB `getFile` ceiling can actually be pulled
    # back, so the caption above says so rather than implying the bytes are
    # mirrored.
    document = getattr(sent, "document", None)
    file_id = getattr(document, "file_id", None)
    if file_id:
        try:
            await client.link_telegram(
                video_id, file_id, message_id=getattr(sent, "message_id", None)
            )
        except (ApiError, ApiNetworkError) as exc:
            # Recording the reference is a convenience; the delivery already
            # succeeded, so never turn this into a user-visible failure.
            logger.info("could not record telegram copy for %s: %s", video_id, exc)

    return True


async def _deliver_oversize(
    bot: Any,
    chat_id: int,
    client: Any,
    video_id: str,
    video: Mapping[str, Any] | None,
    size: int | None,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Deliver a unit Telegram refuses at full size, via its compressed copy.

    The original is never fetched (or, when its size was unknown, never
    fetched *past the cap*). The compressed variant is *streamed* to a temp
    file under the upload cap rather than buffered whole: the backend may
    degrade a missing artifact to the original, so only the byte cap stands
    between a multi-gigabyte unit and the bot's RAM. Outcomes, matching the
    endpoint contract:

    - the artifact streams back inside the cap → read back and sent as a
      normal document, and the copy Telegram stores is recorded like any
      other delivery;
    - 404 ``compressed_not_ready`` → the request just (idempotently) enqueued
      the compress job, so the reply says a smaller copy is on its way — the
      bot does *not* poll; the user's next /media finds it ready;
    - the body passes the cap (the degraded original) → abandoned mid-stream,
      so Telegram's 413 never sees it and RAM never holds it; the reply is the
      portal-page explanation;
    - any other API/network error → the same portal-page explanation.
    """
    with tempfile.NamedTemporaryFile(prefix="recsterio-", suffix=".part") as tmp:
        try:
            written = await client.download_media_to(
                video_id,
                tmp.name,
                max_bytes=MAX_UPLOAD_BYTES,
                variant=COMPRESSED_VARIANT,
            )
        except CompressedNotReadyError:
            # The request enqueued the compress job (idempotently); the user's
            # next /media finds it ready — no polling here.
            logger.info(
                "compressed copy for %s is not ready yet; told the user to retry",
                video_id,
            )
            await bot.send_message(chat_id=chat_id, text=_PREPARING_COPY_TEXT)
            return False
        except OversizeMediaError as exc:
            # The backend degraded to the ORIGINAL: the read crossed the cap
            # and only the cap kept the body out of memory.
            logger.info(
                "compressed variant for %s served an oversize original "
                "(%s bytes seen, cap %s); not sent, pointed at the portal",
                video_id,
                exc.size_seen,
                MAX_UPLOAD_BYTES,
            )
            await _explain_too_large(bot, chat_id, video_id, size, reply_markup, video)
            return False
        except (ApiError, ApiNetworkError) as exc:
            if getattr(exc, "status_code", None) == 404 and getattr(exc, "code", None) == (
                "compressed_not_ready"
            ):
                # A stub/older client may raise the plain error; same answer.
                logger.info("compressed copy for %s is not ready yet", video_id)
                await bot.send_message(chat_id=chat_id, text=_PREPARING_COPY_TEXT)
                return False
            logger.warning("compressed copy for %s is not available: %s", video_id, exc)
            await _explain_too_large(bot, chat_id, video_id, size, reply_markup, video)
            return False

        if not written:
            await _explain_too_large(bot, chat_id, video_id, size, reply_markup, video)
            return False

        logger.info(
            "streamed %s bytes of the compressed copy for %s to a temp file",
            written,
            video_id,
        )
        tmp.seek(0)
        payload = tmp.read()

    # The artifact's own size is what decides its copy's refetchability.
    return await _send_document_and_record(
        bot, chat_id, client, video_id, video, payload, len(payload), reply_markup
    )


async def _stream_original(client: Any, video_id: str) -> bytes | None:
    """Fetch an unknown-size original without ever holding it all in RAM.

    ``file_size`` is not always recorded, and the bot used to pull the whole
    body into memory before Telegram's 413 revealed it was oversize. Instead
    the media stream is written to a temp file, capped at the upload limit:
    a unit past it is abandoned mid-body (None returned — the caller then
    tries the compressed copy), and anything the cap allowed is small enough
    to send. Telegram's 413 stays as the safety net for a size that lied.
    """
    with tempfile.NamedTemporaryFile(prefix="recsterio-", suffix=".part") as tmp:
        try:
            written = await client.download_media_to(
                video_id, tmp.name, max_bytes=MAX_UPLOAD_BYTES
            )
        except OversizeMediaError:
            return None
        logger.info("streamed %s bytes of %s to a temp file", written, video_id)
        tmp.seek(0)
        return tmp.read()


async def deliver_media(
    bot: Any,
    chat_id: int,
    client: Any,
    video_id: str,
    video: Mapping[str, Any] | None = None,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Fetch the media through the API and upload it to the chat.

    ``reply_markup`` is the caller's web-link keyboard, shown under the file
    once it arrives. Returns True when the file was delivered. Still always a
    document upload — a unit over Telegram's 50 MB limit arrives through the
    backend's compressed copy (``variant=compressed``) when that exists, and
    a unit with no recorded size is *streamed* to a temp file so an oversize
    body is discovered and abandoned without landing whole in RAM.
    """
    if video is None:
        try:
            video = await client.get_video(video_id)
        except (ApiError, ApiNetworkError):
            video = {}

    size = video.get("file_size") if isinstance(video, Mapping) else None
    if isinstance(size, int) and size > MAX_UPLOAD_BYTES:
        # Telegram cannot carry the original at all — try the compressed copy.
        return await _deliver_oversize(
            bot, chat_id, client, video_id, video, size, reply_markup
        )

    payload: bytes | None
    try:
        if isinstance(size, int):
            # The recorded size is inside the limit — the original is safe to
            # fetch whole and send, as before.
            payload = await client.get_media(video_id)
        else:
            # `file_size` was never recorded — fetch by streaming instead of
            # trusting the request: an oversize body is abandoned past the
            # upload cap and the compressed copy is tried, so RAM (and the
            # wire) only ever carry what could actually be sent.
            payload = await _stream_original(client, video_id)
    except ApiError as exc:
        if exc.status_code == 404:
            # A 404 means there is no file on disk. Say *why*: still working on
            # it, or the download failed and here is the reason. "Not available
            # yet" alone leaves the user with no idea what to do next. But if
            # Telegram still holds a recorded copy of the file, the disk copy
            # is not needed at all — re-send it first.
            telegram_file_id = (
                video.get("telegram_file_id") if isinstance(video, Mapping) else None
            )
            if telegram_file_id:
                # A re-send by file_id happens inside Telegram's servers, so
                # the upload limit does not apply here.
                try:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=telegram_file_id,
                        caption=f"Video `{video_id}` — re-sent from the Telegram copy",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=reply_markup,
                    )
                    return True
                except Exception as send_exc:  # noqa: BLE001 - fall back to the explanation
                    logger.warning(
                        "could not re-send the telegram copy for %s: %s",
                        video_id,
                        send_exc,
                    )
            await bot.send_message(
                chat_id=chat_id,
                text=_explain_missing_media(video, video_id),
                parse_mode=ParseMode.MARKDOWN,
            )
            return False
        await bot.send_message(
                chat_id=chat_id, text=exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
        return False
    except ApiNetworkError as exc:
        await bot.send_message(
                chat_id=chat_id, text=exc.user_message(), parse_mode=ParseMode.MARKDOWN
            )
        return False

    if payload is None:
        # The streamed original reached past the upload cap and was abandoned:
        # what arrived cannot be sent, and the rest was never pulled. The same
        # compressed-copy attempt an oversize `file_size` gets.
        return await _deliver_oversize(
            bot, chat_id, client, video_id, video, None, reply_markup
        )

    if not payload:
        await bot.send_message(
            chat_id=chat_id, text=f"The media for `{video_id}` is empty.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False

    return await _send_document_and_record(
        bot, chat_id, client, video_id, video, payload, size, reply_markup
    )


async def media_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the downloaded video to the chat."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(USAGE)
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(
            f"`{video_id}` is not a valid video id.\n{USAGE}", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    await message.reply_text(f"Fetching the media for `{video_id}`…", parse_mode=ParseMode.MARKDOWN)
    # The link buttons need the video's folder, so the dict is fetched here
    # and handed to deliver_media (which then skips its own fetch).
    try:
        video = await client.get_video(video_id)
    except (ApiError, ApiNetworkError):
        video = {}
    await deliver_media(
        context.bot,
        message.chat_id,
        client,
        video_id,
        video=video,
        reply_markup=web_links(update, context, video=video, video_id=video_id),
    )


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the video's stored mp3 to the chat."""
    message = update.effective_message
    if message is None:
        return
    args = [a for a in (context.args or []) if a]
    if not args:
        await message.reply_text(AUDIO_USAGE)
        return
    video_id = args[0].strip()
    if not is_uuid(video_id):
        await message.reply_text(
            f"`{video_id}` is not a valid video id.\n{AUDIO_USAGE}", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        client = get_client(context)
    except RuntimeError:
        await message.reply_text(BOT_NOT_READY)
        return

    await message.reply_text(f"Fetching the audio for `{video_id}`…", parse_mode=ParseMode.MARKDOWN)

    # Request-only by design: no watcher, no auto-delivery — the mp3 goes out
    # when a user asks for it, never on its own.
    try:
        video = await client.get_video(video_id)
    except (ApiError, ApiNetworkError):
        video = {}
    title = str(video.get("title") or "").strip() if isinstance(video, Mapping) else ""

    try:
        payload = await client.get_audio(video_id)
    except ApiError as exc:
        if exc.status_code == 404:
            await message.reply_text(
                f"The mp3 for `{video_id}` has not been extracted yet — "
                f"/status `{video_id}` shows where it is.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return
    except ApiNetworkError as exc:
        await message.reply_text(exc.user_message(), parse_mode=ParseMode.MARKDOWN)
        return

    if not payload:
        await message.reply_text(
            f"The audio for `{video_id}` is empty.", parse_mode=ParseMode.MARKDOWN
        )
        return

    label = escape_markdown(title) if title else video_id
    try:
        await context.bot.send_document(
            chat_id=message.chat_id,
            document=payload,
            filename=f"{video_id}.mp3",
            caption=f"Audio (mp3) — `{label}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:  # noqa: BLE001 - a failed upload must not crash the bot
        logger.warning("could not upload the audio for %s: %s", video_id, exc)
        await message.reply_text("Could not upload the file. Please try again in a moment.")
