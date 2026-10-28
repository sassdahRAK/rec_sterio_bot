"""Async HTTP client for the recsterio backend API.

The bot never imports backend code and never touches the database — every
interaction is a plain HTTP call against the routes frozen in
``docs/API_CONTRACT.md``.

Errors are always surfaced through the contract's error envelope::

    {"error": {"code": "...", "message": "...", "details": [...]}}
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Self

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "ApiClient",
    "ApiError",
    "ApiNetworkError",
    "escape_markdown",
]

#: Characters Telegram's *legacy* Markdown (``ParseMode.MARKDOWN``) treats as
#: entity markers, matching ``telegram.helpers.escape_markdown(version=1)``.
#: Escaping anything else — ``.``, ``-``, ``(``, ``!`` … — is shown to the user
#: as a literal backslash, so those are deliberately left alone.
_MARKDOWN_SPECIALS = "_*`["


def escape_markdown(text: str) -> str:
    """Escape a string for Telegram's legacy Markdown parse mode."""
    return re.sub(rf"([{re.escape(_MARKDOWN_SPECIALS)}])", r"\\\1", str(text))


class ApiError(Exception):
    """A structured error returned by the backend."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Any = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code

    def user_message(self) -> str:
        """Human friendly rendering for Telegram (Markdown-safe)."""
        text = f"`{self.code}` {escape_markdown(self.message)}"
        if self.details:
            text += f"\nDetails: {escape_markdown(self.details)}"
        return text


class ApiNetworkError(Exception):
    """The backend could not be reached after exhausting retries."""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause

    def user_message(self) -> str:
        return escape_markdown(
            "[network_error] Could not reach the recsterio backend. "
            "Please try again in a moment."
        )


def _sleep(seconds: float) -> Awaitable[None]:
    return asyncio.sleep(seconds)


class ApiClient:
    """Thin async wrapper over the backend REST API."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        upload_timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = _sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._max_retries = max_retries
        self._backoff = backoff
        # Uploads move real megabytes, so they need a much longer timeout than
        # a JSON call.
        self._upload_timeout = httpx.Timeout(upload_timeout)
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            transport=transport,
            base_url=self.base_url,
        )
        if client is not None:
            self._client.timeout = httpx.Timeout(timeout)

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- plumbing ----------------------------------------------------------
    def _headers(self, requested_by: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if requested_by:
            headers["X-Requested-By"] = requested_by
        return headers

    def media_url(self, video_id: str) -> str:
        return f"{self.base_url}/videos/{video_id}/media"

    async def send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        requested_by: str | None = None,
    ) -> httpx.Response:
        """Perform an API call, retrying transient network failures.

        Raises :class:`ApiNetworkError` when the backend stays unreachable.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                return await self._client.request(
                    method,
                    url,
                    json=json,
                    params=dict(params) if params else None,
                    headers=self._headers(requested_by),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                if attempt > self._max_retries:
                    logger.warning(
                        "backend unreachable after %s attempt(s): %s", attempt, type(exc).__name__
                    )
                    raise ApiNetworkError(str(exc) or type(exc).__name__, exc) from exc
                delay = self._backoff * (2 ** (attempt - 1))
                logger.info(
                    "retrying %s %s in %.2fs (attempt %s)", method, path, delay, attempt
                )
                await self._sleep(delay)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        requested_by: str | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Perform an API call and decode the JSON body.

        Raises :class:`ApiError` for backend errors and
        :class:`ApiNetworkError` when the backend is unreachable.
        """
        response = await self.send(
            method,
            path,
            json=json,
            params=params,
            requested_by=requested_by,
        )

        if response.status_code >= 400:
            raise self._to_api_error(response)

        if not expect_json:
            return response
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ApiError(
                "invalid_response",
                "The backend returned a non-JSON response.",
                status_code=response.status_code,
            ) from None

    def _to_api_error(self, response: httpx.Response) -> ApiError:
        """Translate an error response into the contract's error envelope."""
        code = f"http_{response.status_code}"
        message = f"Backend returned HTTP {response.status_code}."
        details: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = str(err.get("code") or code)
            message = str(err.get("message") or message)
            details = err.get("details")
        else:
            # Not a contract-shaped envelope (e.g. an HTML error page) — keep a
            # short, harmless snippet so the user still gets something useful.
            snippet = (response.text or "").strip().replace("\n", " ")[:200]
            if snippet:
                message = f"{message} {snippet}"
        return ApiError(code, message, details, status_code=response.status_code)

    # -- endpoints ---------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        return await self.request("GET", "/health")

    async def create_video(
        self,
        source_url: str,
        *,
        requested_by: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        start_transcription: bool = True,
    ) -> dict[str, Any]:
        body = {
            "source_url": source_url,
            "title": title,
            "notes": notes,
            "start_transcription": start_transcription,
        }
        response = await self.send("POST", "/videos", json=body, requested_by=requested_by)
        if response.status_code == 409:
            # The contract says a duplicate returns the *existing* video
            # instead of duplicating. Some backends answer with the standard
            # error envelope instead — in that case we surface the code.
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                # The backend returns the existing video either at the top
                # level or nested under "video" alongside the error envelope.
                existing = payload.get("video") or payload
                if isinstance(existing, dict) and existing.get("id"):
                    return existing
            raise self._to_api_error(response)
        if response.status_code >= 400:
            raise self._to_api_error(response)
        return response.json() if response.content else {}

    async def get_video(self, video_id: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return await self.request("GET", f"/videos/{video_id}", requested_by=requested_by)

    async def list_videos(
        self, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """One page of the video library (`{items, total, page, page_size}`)."""
        return await self.request("GET", "/videos", params=params)

    async def delete_video(self, video_id: str) -> None:
        await self.request("DELETE", f"/videos/{video_id}", expect_json=False)

    async def retry_video(self, video_id: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return await self.request(
            "POST", f"/videos/{video_id}/retry", requested_by=requested_by
        )

    async def get_transcript(self, video_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/videos/{video_id}/transcript")

    async def list_transcripts(self, video_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/videos/{video_id}/transcripts")

    async def get_transcript_segments(self, video_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/videos/{video_id}/transcript/segments")

    async def save_transcript(
        self,
        video_id: str,
        content: str,
        *,
        segments: Any = None,
        language: str | None = None,
        cleaned: bool = True,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Save a manual correction as a NEW transcript version (instruction.txt #3).

        The backend never mutates history: this creates v(n+1) and leaves the
        ASR output retrievable. A manual edit is a human correction, so the new
        version is marked cleaned by default (#8).
        """
        body: dict[str, Any] = {"content": content, "cleaned": cleaned}
        if segments is not None:
            body["segments"] = segments
        if language:
            body["language"] = language
        return await self.request(
            "PUT",
            f"/videos/{video_id}/transcript",
            json=body,
            requested_by=requested_by,
        )

    async def record_tvna(
        self,
        video_id: str,
        *,
        location_tag: str,
        source_type: str = "Video",
        collection_method: str = "Manual browse",
        author_handle: str | None = None,
        date_posted: str | None = None,
        caption: str | None = None,
        collected_by: str | None = None,
        notes: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Attach the TvNa collection record (instruction.txt #6)."""
        body: dict[str, Any] = {
            "location_tag": location_tag,
            "source_type": source_type,
            "collection_method": collection_method,
        }
        for key, value in (
            ("author_handle", author_handle),
            ("date_posted", date_posted),
            ("caption", caption),
            ("collected_by", collected_by),
            ("notes", notes),
        ):
            if value:
                body[key] = value
        return await self.request(
            "POST", f"/videos/{video_id}/tvna", json=body, requested_by=requested_by
        )

    async def link_telegram(
        self,
        video_id: str,
        file_id: str,
        *,
        message_id: int | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Record the Telegram copy of a video (instruction.txt #4)."""
        body: dict[str, Any] = {"file_id": file_id}
        if message_id is not None:
            body["message_id"] = message_id
        return await self.request(
            "POST", f"/videos/{video_id}/telegram", json=body, requested_by=requested_by
        )

    async def generate_transcript(
        self,
        video_id: str,
        engine: str = "whisper",
        *,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Enqueue transcription work (``whisper`` | ``gemini`` | ``both``)."""
        return await self.request(
            "POST",
            f"/videos/{video_id}/transcript/generate",
            json={"engine": engine},
            requested_by=requested_by,
        )

    async def create_merge(
        self,
        video_id: str,
        *,
        base_transcript_id: str | None = None,
        gemini_transcript_id: str | None = None,
        model: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if base_transcript_id:
            body["base_transcript_id"] = base_transcript_id
        if gemini_transcript_id:
            body["gemini_transcript_id"] = gemini_transcript_id
        if model:
            body["model"] = model
        return await self.request(
            "POST",
            f"/videos/{video_id}/transcript/merge",
            json=body,
            requested_by=requested_by,
        )

    async def list_merges(self, video_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/videos/{video_id}/merges")

    async def get_merge(self, merge_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/merges/{merge_id}")

    async def apply_merge(
        self, merge_id: str, *, requested_by: str | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "POST", f"/merges/{merge_id}/apply", requested_by=requested_by
        )

    async def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        kind: str | None = None,
        video_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        if kind:
            params["kind"] = kind
        if video_id:
            params["video_id"] = video_id
        return await self.request("GET", "/jobs", params=params)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/jobs/{job_id}")

    async def retry_job(self, job_id: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return await self.request("POST", f"/jobs/{job_id}/retry", requested_by=requested_by)

    async def get_media(self, video_id: str) -> bytes:
        response = await self.request("GET", f"/videos/{video_id}/media", expect_json=False)
        return response.content

    # -- dataset layer (v2) -----------------------------------------------
    # The bot authenticates as the service token, which the API treats as an
    # editor on every folder, so these can address any folder by id.
    async def list_folders(
        self,
        *,
        parent_id: str | None = None,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if parent_id:
            params["parent_id"] = parent_id
        if visibility:
            params["visibility"] = visibility
        return await self.request("GET", "/folders", params=params)

    async def get_folder(self, folder_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/folders/{folder_id}")

    async def create_folder(
        self,
        name: str,
        *,
        description: str | None = None,
        visibility: str = "private",
        parent_id: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "visibility": visibility,
            "parent_id": parent_id,
        }
        return await self.request(
            "POST", "/folders", json=body, requested_by=requested_by
        )

    async def list_cards(
        self,
        *,
        folder_id: str | None = None,
        cleaned: bool | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if folder_id:
            params["folder_id"] = folder_id
        if cleaned is not None:
            params["cleaned"] = str(cleaned).lower()
        if q:
            params["q"] = q
        return await self.request("GET", "/dataset/cards", params=params)

    async def create_share(
        self,
        folder_id: str,
        role: str = "viewer",
        *,
        expires_at: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"role": role}
        if expires_at:
            body["expires_at"] = expires_at
        return await self.request(
            "POST",
            f"/folders/{folder_id}/shares",
            json=body,
            requested_by=requested_by,
        )

    async def create_passcode(
        self,
        folder_id: str,
        *,
        code: str | None = None,
        label: str | None = None,
        expires_at: str | None = None,
        max_uses: int | None = None,
        max_devices: int | None = None,
        one_time: bool = False,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": code,
            "label": label,
            "expires_at": expires_at,
            "max_uses": max_uses,
            "max_devices": max_devices,
            "one_time": one_time,
        }
        return await self.request(
            "POST",
            f"/folders/{folder_id}/passcodes",
            json=body,
            requested_by=requested_by,
        )

    async def download_folder_zip(self, folder_id: str) -> bytes:
        """The folder and its subtree as one archive (video + audio + transcript)."""
        response = await self.request(
            "GET", f"/folders/{folder_id}/download", expect_json=False
        )
        return response.content

    async def upload_video(
        self,
        content: bytes,
        filename: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        start_transcription: bool = True,
        requested_by: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Send a file the user already has to the backend's upload endpoint.

        Ingestion must not depend on a platform serving a download — TikTok and
        YouTube refuse most server-side requests — so a user can hand the bot a
        file directly and get the same pipeline behind it.
        """
        data: dict[str, Any] = {"start_transcription": str(start_transcription).lower()}
        if title:
            data["title"] = title
        if notes:
            data["notes"] = notes

        url = f"{self.base_url}/videos/upload"
        attempt = 0
        while True:
            try:
                response = await self._client.post(
                    url,
                    files={"file": (filename, content, content_type)},
                    data=data,
                    headers=self._headers(requested_by),
                    timeout=self._upload_timeout,
                )
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise ApiNetworkError(
                        str(exc) or type(exc).__name__, exc
                    ) from exc
                await self._sleep(self._backoff * (2 ** (attempt - 1)))

        if response.status_code == 409:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                existing = payload.get("video") or payload
                if isinstance(existing, dict) and existing.get("id"):
                    return existing
            raise self._to_api_error(response)
        if response.status_code >= 400:
            raise self._to_api_error(response)
        return response.json() if response.content else {}
