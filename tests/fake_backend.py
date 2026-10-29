"""A tiny in-memory fake of the recsterio backend API.

It serves exactly the shapes frozen in ``docs/API_CONTRACT.md`` so the bot
can be exercised end-to-end without the real backend running.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

__all__ = ["FakeBackend", "make_backend"]

DEFAULT_STATUS_SCRIPT = ["pending", "downloading", "downloaded", "transcribed"]

#: The consent-text version the backend currently serves; POSTing a different
#: version is the 422 version_mismatch path.
DEFAULT_CONSENT_VERSION = "2026-10-28.1"


class FakeBackend:
    """Mutable in-memory backend state."""

    def __init__(self, token: str = "test-service-token") -> None:
        self.token = token
        self.videos: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.merges: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, dict[str, Any]] = {}
        self.shares: dict[str, dict[str, Any]] = {}
        self.passcodes: dict[str, dict[str, Any]] = {}
        #: body returned by GET /folders/{id}/download; tests enlarge it to
        #: exercise the "too big for Telegram" fallback.
        self.zip_bytes = b"PK\x03\x04fake-zip-for-tests"
        #: statuses a newly created merge walks through, one per GET /merges/{id}
        self.merge_status_script: list[str] = ["ready"]
        self.requests: list[dict[str, Any]] = []
        #: bodies received by POST /videos/upload (size, content type, attribution)
        self.uploads: list[dict[str, Any]] = []
        #: versions created by PUT /videos/{id}/transcript
        self.saved_transcripts: list[dict[str, Any]] = []
        #: records written by POST /videos/{id}/tvna
        self.tvna_records: list[dict[str, Any]] = []
        #: bodies received by POST /videos/{id}/telegram
        self.telegram_links: list[dict[str, Any]] = []
        self.status_script: list[str] = list(DEFAULT_STATUS_SCRIPT)
        self.transcript_content: str = "Hello world. This is a short transcript."
        self.transcript_language: str = "en"
        self.long_transcript_content: str | None = None
        #: per-Telegram-user consent + cookies records (/bot-users/{uid})
        self.bot_users: dict[int, dict[str, Any]] = {}
        #: one-time console link codes → the account email they point at
        self.link_codes: dict[str, str] = {}
        #: telegram ids already linked to a console account
        #: (uid → {email, name, role}); mirrored into GET /bot-users/{uid}
        self.linked: dict[int, dict[str, Any]] = {}
        #: when True, POST /bot-users/{uid}/link answers 409 already_linked
        #: (given a valid code) — the "linked to a different console account"
        #: path.
        self.link_conflict = False
        #: ids returned by GET /bot-users/admin-ids (linked ACTIVE admins)
        self.admin_ids: list[int] = []
        #: bodies received by POST /bot-users/{uid}/link (both phases)
        self.link_redeems: list[dict[str, Any]] = []
        #: bodies received by POST /bot-users/{uid}/cookies (multipart)
        self.cookie_uploads: list[dict[str, Any]] = []
        #: the consent version a POST must match; mismatch → 422
        self.consent_version: str = DEFAULT_CONSENT_VERSION
        #: when True, POST /videos answers 403 consent_required for a
        #: tiktok/facebook URL from a telegram user without consent + cookies.
        #: Off by default so the pre-gate link tests keep their behaviour.
        self.enforce_consent_gate = False
        #: when set, the next request raises this HTTP status with this body
        self.fail_next: tuple[int, str, str] | None = None
        self.app = FastAPI(title="fake-recsterio")
        self._register_routes()

    # -- helpers -----------------------------------------------------------
    def _error(self, status: int, code: str, message: str, details: Any = None) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "details": details}},
        )

    def _auth(self, request: Request) -> JSONResponse | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return self._error(401, "unauthorized", "Missing bearer token.")
        if header.split(" ", 1)[1] != self.token:
            return self._error(401, "unauthorized", "Invalid token.")
        return None

    def _record(self, request: Request) -> None:
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "requested_by": request.headers.get("x-requested-by"),
                "authorization": request.headers.get("authorization"),
            }
        )

    def _maybe_fail(self) -> PlainTextResponse | None:
        if self.fail_next is None:
            return None
        status, code, message = self.fail_next
        self.fail_next = None
        if status >= 500:
            return PlainTextResponse(
                f"<html><body>Internal Server Error: {code} {message}</body></html>",
                status_code=status,
            )
        return self._error(status, code, message)

    def _transcripts_for(self, video: dict[str, Any]) -> list[dict[str, Any]]:
        if video.get("status") != "transcribed":
            return []
        return video.setdefault("transcripts", [])

    def _advance(self, video: dict[str, Any]) -> dict[str, Any]:
        """Move the video one step along the scripted status sequence."""
        script = video.get("status_script") or self.status_script
        index = int(video.get("script_index", 0))
        if index < len(script):
            video["status"] = script[index]
            video["script_index"] = index + 1
        if video["status"] == "downloading":
            video["progress"] = min(100, int(video.get("progress", 0)) + 40)
        if video["status"] == "downloaded":
            video["progress"] = 100
            video["file_size"] = 1234567
            video["duration_seconds"] = 61.5
            video["title"] = video.get("title") or "A fake video"
        if video["status"] == "transcribed" and not video.get("transcripts"):
            video["transcripts"] = [
                {
                    "id": str(uuid.uuid4()),
                    "video_id": video["id"],
                    "version": 1,
                    "source": "whisper",
                    "language": self.transcript_language,
                    "content": self.transcript_content,
                    "segments": None,
                    "status": "ready",
                    "error": None,
                    "created_by": "whisper",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ]
        return video

    def add_transcript(
        self, video_id: str, content: str, version: int = 1, source: str = "whisper"
    ) -> dict[str, Any]:
        """Attach a ready transcript to a video (test helper)."""
        video = self.videos[video_id]
        transcript = {
            "id": str(uuid.uuid4()),
            "video_id": video_id,
            "version": version,
            "source": source,
            "language": self.transcript_language,
            "content": content,
            "segments": None,
            "status": "ready",
            "error": None,
            "created_by": source,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        video.setdefault("transcripts", []).insert(0, transcript)
        return transcript

    def add_folder(
        self,
        name: str,
        *,
        visibility: str = "private",
        parent_id: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a folder row directly (test helper)."""
        folder_id = str(uuid.uuid4())
        folder = {
            "id": folder_id,
            "name": name,
            "description": description,
            "visibility": visibility,
            "owner_id": "00000000-0000-0000-0000-000000000001",
            "owner_email": "admin@recsterio.local",
            "parent_id": parent_id,
            "position": 0,
        }
        self.folders[folder_id] = folder
        return folder

    def file_video(self, video_id: str, folder_id: str | None = None, **overrides: Any) -> None:
        """Put a video in a folder and mark it ready (test helper)."""
        video = self.videos[video_id]
        video["folder_id"] = folder_id
        video["status"] = "downloaded"
        video["progress"] = 100
        video["file_size"] = 1234567
        video["duration_seconds"] = 61.5
        video.setdefault("title", "A fake video")
        for key, value in overrides.items():
            video[key] = value

    def _empty_bot_user(self, telegram_id: int) -> dict[str, Any]:
        return {
            "telegram_id": telegram_id,
            "consented": False,
            "consented_at": None,
            "consent_version": None,
            "cookies_present": False,
            "cookies_updated_at": None,
        }

    def _link_account(self, email: str) -> dict[str, Any]:
        """The console account a link code points at (name/role derived)."""
        local = email.split("@", 1)[0]
        name = local.replace(".", " ").replace("_", " ").strip().title() or email
        return {
            "account_email": email,
            "account_name": name,
            "account_role": "admin",
        }

    def _consent_gate_error(self, request: Request, source_url: str) -> JSONResponse | None:
        """The pinned contract's gate: tiktok/facebook needs consent + cookies.

        Only applies to telegram users (``X-Requested-By: telegram:<uid>``);
        any other caller — the dashboard, the service token itself — passes.
        """
        if not self.enforce_consent_gate:
            return None
        requested_by = request.headers.get("x-requested-by") or ""
        if not requested_by.startswith("telegram:"):
            return None
        try:
            uid = int(requested_by.split(":", 1)[1])
        except ValueError:
            return None
        host = source_url.split("://", 1)[-1].split("/", 1)[0].lower()
        if "tiktok" not in host and "facebook" not in host:
            return None
        record = self.bot_users.get(uid)
        if record is not None and record.get("consented") and record.get("cookies_present"):
            return None
        return self._error(
            403,
            "consent_required",
            "Run /cookies in the bot to allow downloads with your own session.",
        )

    # -- routes ------------------------------------------------------------
    def _register_routes(self) -> None:
        app = self.app

        @app.get("/api/v1/health")
        async def health() -> dict[str, Any]:
            return {"status": "ok", "db": True, "worker_alive": True}

        @app.get("/api/v1/videos")
        async def list_videos(request: Request) -> Any:
            """One page of the library, newest first."""
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
            items = [
                {k: v for k, v in video.items() if k not in {"script_index", "status_script"}}
                for video in self.videos.values()
            ]
            start = (page - 1) * page_size
            return {
                "items": items[start : start + page_size],
                "total": len(items),
                "page": page,
                "page_size": page_size,
            }

        @app.post("/api/v1/videos")
        async def create_video(request: Request) -> Any:
            self._record(request)
            failure = self._maybe_fail()
            if failure is not None:
                return failure
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            body = await request.json()
            source_url = body.get("source_url")
            if not isinstance(source_url, str) or not source_url.startswith(("http://", "https://")):
                return self._error(
                    422,
                    "validation_failed",
                    "source_url must be a valid http(s) URL.",
                    [{"field": "source_url", "issue": "invalid_url"}],
                )
            gate = self._consent_gate_error(request, source_url)
            if gate is not None:
                return gate
            for video in self.videos.values():
                if video["source_url"] == source_url and video["status"] != "failed":
                    return JSONResponse(status_code=409, content=video)
            video_id = str(uuid.uuid4())
            video = {
                "id": video_id,
                "source_url": source_url,
                "platform": "youtube",
                "title": None,
                "notes": body.get("notes"),
                "status": "pending",
                "progress": 0,
                "error": None,
                "local_path": None,
                "file_size": None,
                "duration_seconds": None,
                "mime_type": "video/mp4",
                "checksum_sha256": None,
                "thumbnail_url": None,
                # Parity with VideoOut: the Telegram copy recorded by
                # POST /videos/{id}/telegram, absent until one exists.
                "telegram_file_id": None,
                # Parity with VideoOut: the folder the video is filed into,
                # None until it is filed (the portal link button reads it).
                "folder_id": None,
                "created_by": request.headers.get("x-requested-by", "system"),
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "script_index": 0,
                "transcripts": [],
            }
            self.videos[video_id] = video
            job = {
                "id": str(uuid.uuid4()),
                "kind": "download",
                "video_id": video_id,
                "status": "queued",
                "payload": {"source_url": source_url},
                "attempts": 0,
                "max_attempts": 3,
                "priority": 0,
                "last_error": None,
                "scheduled_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-01T00:00:00Z",
            }
            self.jobs[job["id"]] = job
            return JSONResponse(status_code=201, content=video)

        @app.post("/api/v1/videos/upload")
        async def upload_video(request: Request) -> Any:
            # Declared before /videos/{video_id} so "upload" is not taken as an id.
            self._record(request)
            failure = self._maybe_fail()
            if failure is not None:
                return failure
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            raw = await request.body()
            self.uploads.append(
                {
                    "content_type": request.headers.get("content-type", ""),
                    "bytes": len(raw),
                    "requested_by": request.headers.get("x-requested-by"),
                }
            )
            if not raw:
                return self._error(422, "validation_failed", "The uploaded file is empty.")
            video_id = str(uuid.uuid4())
            video = {
                "id": video_id,
                "source_url": "upload://clip.mp4",
                "platform": "direct",
                "title": None,
                "notes": None,
                "status": "downloaded",
                # The upload path already has the file, so the video is
                # immediately downloadable and the watcher settles fast.
                "progress": 100,
                "error": None,
                "local_path": "/tmp/fake.mp4",
                "file_size": len(raw),
                "duration_seconds": 3.0,
                "mime_type": "video/mp4",
                "checksum_sha256": "deadbeef",
                "thumbnail_url": None,
                "telegram_file_id": None,
                # Parity with VideoOut, like the link-created video above.
                "folder_id": None,
                "created_by": request.headers.get("x-requested-by", "system"),
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "script_index": 0,
                "transcripts": [],
            }
            self.videos[video_id] = video
            return JSONResponse(status_code=201, content=video)

        @app.get("/api/v1/videos/{video_id}")
        async def get_video(video_id: str, request: Request) -> Any:
            self._record(request)
            failure = self._maybe_fail()
            if failure is not None:
                return failure
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            self._advance(video)
            return {
                **{k: v for k, v in video.items() if k not in {"script_index", "status_script"}},
                "transcripts": list(video.get("transcripts") or []),
                "jobs": [j for j in self.jobs.values() if j["video_id"] == video_id],
            }

        @app.get("/api/v1/videos/{video_id}/media")
        async def get_media(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None or video["status"] not in {"downloaded", "transcribed"}:
                return self._error(404, "not_found", "Media is not available yet.")
            return PlainTextResponse("fake-media-bytes")

        @app.get("/api/v1/videos/{video_id}/audio")
        async def get_audio(video_id: str, request: Request) -> Any:
            """The extracted mp3, mirroring the media route's availability."""
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            if request.query_params.get("format", "mp3") != "mp3":
                return self._error(
                    422,
                    "validation_failed",
                    "format must be mp3.",
                    [{"field": "format", "issue": "unsupported_format"}],
                )
            video = self.videos.get(video_id)
            if video is None or video["status"] not in {"downloaded", "transcribed"}:
                return self._error(
                    404, "not_found", "MP3 has not been extracted for this video."
                )
            return Response(
                content=b"fake-mp3",
                media_type="audio/mpeg",
                headers={
                    "Content-disposition": f'attachment; filename="{video_id}-audio.mp3"'
                },
            )

        @app.get("/api/v1/videos/{video_id}/transcript")
        async def get_transcript(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            transcripts = video.get("transcripts") or []
            if not transcripts:
                return self._error(404, "not_found", "No transcript for this video.")
            return transcripts[0]

        @app.put("/api/v1/videos/{video_id}/transcript")
        async def save_transcript(video_id: str, request: Request) -> Any:
            """Manual edit → a NEW version, matching the real backend."""
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            body = await request.json()
            transcripts = video.setdefault("transcripts", [])
            version = max((t.get("version", 0) for t in transcripts), default=0) + 1
            transcript = {
                "id": f"tr-{video_id}-v{version}",
                "video_id": video_id,
                "version": version,
                "source": "manual",
                "language": body.get("language"),
                "content": body.get("content", ""),
                "segments": body.get("segments"),
                "status": "ready",
                "cleaned": body.get("cleaned", True),
                "error": None,
                "created_by": request.headers.get("X-Requested-By", "system"),
                "created_at": "2026-10-28T00:00:00Z",
                "updated_at": "2026-10-28T00:00:00Z",
            }
            transcripts.insert(0, transcript)
            self.saved_transcripts.append(transcript)
            return JSONResponse(transcript, status_code=201)

        @app.post("/api/v1/videos/{video_id}/tvna")
        async def record_tvna(video_id: str, request: Request) -> Any:
            """Allocate PP-NNN the first time, keep it on a re-record."""
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            body = await request.json()
            if not body.get("location_tag"):
                return self._error(422, "validation_failed", "location_tag is required.")
            if video.get("tvna_serial") is None:
                video["tvna_serial"] = len(
                    [v for v in self.videos.values() if v.get("tvna_serial")]
                ) + 1
            serial = video["tvna_serial"]
            video.update(
                {
                    "tvna_serial": serial,
                    "tvna_id": f"PP-{serial:03d}",
                    "location_tag": body["location_tag"],
                    "source_type": body.get("source_type", "Video"),
                    "collection_method": body.get("collection_method", "Manual browse"),
                    "collected_by": body.get("collected_by") or "system",
                    "collection_date": body.get("collection_date") or "2026-10-28",
                    "notes": body.get("notes", video.get("notes")),
                }
            )
            for key in ("author_handle", "date_posted", "caption"):
                if body.get(key):
                    video[key] = body[key]
            self.tvna_records.append(dict(video))
            return JSONResponse(video)

        @app.post("/api/v1/videos/{video_id}/telegram")
        async def link_telegram(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            body = await request.json()
            video["telegram_file_id"] = body.get("file_id")
            if body.get("message_id") is not None:
                video["telegram_message_id"] = body["message_id"]
            self.telegram_links.append(dict(body))
            return JSONResponse(video)

        @app.post("/api/v1/videos/{video_id}/transcript/generate")
        async def generate_transcript(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            body = await request.json()
            engine = body.get("engine", "whisper")
            job = {
                "id": str(uuid.uuid4()),
                "kind": "gemini_transcribe" if engine == "gemini" else "transcribe",
                "video_id": video_id,
                "status": "queued",
                "payload": {"engine": engine},
                "attempts": 0,
                "max_attempts": 3,
                "priority": 0,
                "last_error": None,
                "scheduled_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-01T00:00:00Z",
            }
            self.jobs[job["id"]] = job
            if engine == "gemini":
                # The fake backend "finishes" the Gemini transcript immediately so
                # the merge path can be exercised without waiting.
                self.add_transcript(video_id, "សួស្តី hello from gemini", source="gemini")
            return JSONResponse(status_code=202, content={"jobs": [job]})

        @app.post("/api/v1/videos/{video_id}/transcript/merge")
        async def create_merge(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            body = await request.json()
            transcripts = video.get("transcripts") or []
            gemini = [t for t in transcripts if t.get("source") == "gemini"]
            if not gemini:
                return self._error(
                    409, "conflict", "No Gemini transcript to merge."
                )
            merge_id = str(uuid.uuid4())
            merge = {
                "id": merge_id,
                "video_id": video_id,
                "base_transcript_id": body.get("base_transcript_id"),
                "gemini_transcript_id": body.get("gemini_transcript_id") or gemini[0]["id"],
                "merged_transcript_id": None,
                "status": "pending",
                "diff": None,
                "model": body.get("model") or "gemini-2.5-pro",
                "error": None,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "merge_script": list(self.merge_status_script),
            }
            self.merges[merge_id] = merge
            return JSONResponse(status_code=202, content=merge)

        @app.get("/api/v1/videos/{video_id}/merges")
        async def list_merges(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            if video_id not in self.videos:
                return self._error(404, "not_found", f"No video {video_id}.")
            items = [m for m in self.merges.values() if m["video_id"] == video_id]
            return {"items": items}

        @app.get("/api/v1/merges/{merge_id}")
        async def get_merge(merge_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            merge = self.merges.get(merge_id)
            if merge is None:
                return self._error(404, "not_found", f"No merge {merge_id}.")
            script = merge.get("merge_script") or []
            if script:
                merge["status"] = script.pop(0)
            if merge["status"] == "ready" and merge.get("diff") is None:
                merge["diff"] = {
                    "stats": {
                        "output_segments": 2,
                        "from_base": 1,
                        "from_candidate": 1,
                        "flagged": 1,
                        "base_label": "whisper",
                        "candidate_label": "gemini",
                        "mean_agreement": 0.5,
                    },
                    "summary": "1 segment from Whisper, 1 from Gemini, 1 flagged.",
                    "segments": [
                        {
                            "index": 0,
                            "start": 0.0,
                            "end": 1.0,
                            "text": "Hello world.",
                            "source": "both",
                            "language": "en",
                            "flagged": False,
                        },
                        {
                            "index": 1,
                            "start": 1.0,
                            "end": 2.0,
                            "text": "សួស្តី from gemini",
                            "source": "gemini",
                            "language": "km-en",
                            "flagged": True,
                        },
                    ],
                }
                merge["merged_transcript_id"] = str(uuid.uuid4())
            return merge

        @app.post("/api/v1/merges/{merge_id}/apply")
        async def apply_merge(merge_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            merge = self.merges.get(merge_id)
            if merge is None:
                return self._error(404, "not_found", f"No merge {merge_id}.")
            if merge.get("status") != "ready":
                return self._error(409, "conflict", "This merge is not ready to be applied.")
            video = self.videos[merge["video_id"]]
            version = max(
                (int(t.get("version", 0)) for t in video.get("transcripts") or []),
                default=0,
            ) + 1
            transcript = self.add_transcript(
                merge["video_id"], "Hello world. សួស្តី from gemini", version, "merged"
            )
            return transcript

        @app.get("/api/v1/videos/{video_id}/transcripts")
        async def list_transcripts(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            return {"items": list(video.get("transcripts") or [])}

        @app.post("/api/v1/videos/{video_id}/retry")
        async def retry_video(video_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            video = self.videos.get(video_id)
            if video is None:
                return self._error(404, "not_found", f"No video {video_id}.")
            job = {
                "id": str(uuid.uuid4()),
                "kind": "download",
                "video_id": video_id,
                "status": "queued",
                "payload": {"source_url": video["source_url"]},
                "attempts": 0,
                "max_attempts": 3,
                "priority": 0,
                "last_error": None,
                "scheduled_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-01T00:00:00Z",
            }
            self.jobs[job["id"]] = job
            return JSONResponse(status_code=202, content=job)

        @app.get("/api/v1/jobs")
        async def list_jobs(request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            items = list(self.jobs.values())
            status_filter = request.query_params.get("status")
            if status_filter:
                items = [j for j in items if j["status"] == status_filter]
            return {"items": items, "total": len(items), "page": 1, "page_size": 20}

        @app.post("/api/v1/jobs/{job_id}/retry")
        async def retry_job(job_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            job = self.jobs.get(job_id)
            if job is None:
                return self._error(404, "not_found", f"No job {job_id}.")
            job["status"] = "queued"
            job["attempts"] = 0
            return JSONResponse(status_code=202, content=job)

        # ---------------------------------------------------------- dataset
        def folder_count(folder_id: str) -> int:
            return sum(1 for v in self.videos.values() if v.get("folder_id") == folder_id)

        def folder_node(folder: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": folder["id"],
                "name": folder["name"],
                "description": folder.get("description"),
                "visibility": folder["visibility"],
                "owner_id": folder["owner_id"],
                "owner_email": folder.get("owner_email"),
                "parent_id": folder.get("parent_id"),
                "position": folder.get("position", 0),
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "video_count": folder_count(folder["id"]),
                "my_role": "editor",
                "children": [
                    folder_node(child)
                    for child in self.folders.values()
                    if child.get("parent_id") == folder["id"]
                ],
            }

        def cards_for(folder_id: str | None = None) -> list[dict[str, Any]]:
            cards = []
            for video in self.videos.values():
                if folder_id and video.get("folder_id") != folder_id:
                    continue
                transcript = (video.get("transcripts") or [None])[0]
                folder = self.folders.get(video.get("folder_id") or "")
                cards.append(
                    {
                        "video_id": video["id"],
                        "title": video.get("title"),
                        "source_url": video.get("source_url"),
                        "status": video.get("status"),
                        "duration_seconds": video.get("duration_seconds"),
                        "thumbnail_url": video.get("thumbnail_url"),
                        "folder_id": video.get("folder_id"),
                        "folder_name": folder["name"] if folder else None,
                        "badge": "cleaned" if video.get("cleaned") else "raw",
                        "transcript_id": transcript["id"] if transcript else None,
                        "transcript_preview": (
                            " ".join((transcript.get("content") or "").split())[:280]
                            if transcript
                            else None
                        ),
                        "audio_available": video.get("status") == "downloaded",
                        "media_url": f"/api/v1/videos/{video['id']}/media",
                        "audio_url": f"/api/v1/videos/{video['id']}/audio",
                        "transcript_url": f"/api/v1/videos/{video['id']}/transcript",
                    }
                )
            return cards

        @app.get("/api/v1/folders")
        async def list_folders(request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            parent = request.query_params.get("parent_id")
            items = [
                folder_node(folder)
                for folder in self.folders.values()
                if folder.get("parent_id") == parent
            ]
            return {"items": items, "total": len(items)}

        @app.post("/api/v1/folders")
        async def create_folder(request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            body = await request.json()
            name = body.get("name")
            if not name:
                return self._error(422, "validation_failed", "name is required.")
            folder_id = str(uuid.uuid4())
            folder = {
                "id": folder_id,
                "name": name,
                "description": body.get("description"),
                "visibility": body.get("visibility", "private"),
                "owner_id": "00000000-0000-0000-0000-000000000001",
                "owner_email": "admin@recsterio.local",
                "parent_id": body.get("parent_id"),
                "position": body.get("position", 0),
            }
            self.folders[folder_id] = folder
            return JSONResponse(status_code=201, content=folder_node(folder))

        @app.get("/api/v1/folders/{folder_id}")
        async def get_folder(folder_id: str, request: Request) -> Any:
            self._record(request)
            folder = self.folders.get(folder_id)
            if folder is None:
                return self._error(404, "not_found", "Folder not found.", [{"id": folder_id}])
            return folder_node(folder)

        @app.get("/api/v1/dataset/cards")
        async def list_cards(request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            folder_id = request.query_params.get("folder_id")
            query = (request.query_params.get("q") or "").lower()
            cleaned = request.query_params.get("cleaned")
            cards = cards_for(folder_id)
            if query:
                cards = [
                    c
                    for c in cards
                    if query in (c.get("title") or "").lower()
                    or query in (c.get("source_url") or "").lower()
                ]
            if cleaned is not None:
                wanted = cleaned.lower() == "true"
                cards = [c for c in cards if (c["badge"] == "cleaned") == wanted]
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
            total = len(cards)
            start = (page - 1) * page_size
            return {
                "items": cards[start : start + page_size],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

        @app.post("/api/v1/folders/{folder_id}/shares")
        async def create_share(folder_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            if folder_id not in self.folders:
                return self._error(404, "not_found", "Folder not found.", [{"id": folder_id}])
            body = await request.json()
            share_id = str(uuid.uuid4())
            share = {
                "id": share_id,
                "folder_id": folder_id,
                "token": uuid.uuid4().hex,
                "role": body.get("role", "viewer"),
                "expires_at": body.get("expires_at"),
                "revoked_at": None,
                "created_by": request.headers.get("x-requested-by", "system"),
                "created_at": "2026-01-01T00:00:00Z",
            }
            self.shares[share_id] = share
            return JSONResponse(
                status_code=201,
                content={**share, "url": f"https://portal.example.org/share/{share['token']}"},
            )

        @app.post("/api/v1/folders/{folder_id}/passcodes")
        async def create_passcode(folder_id: str, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            if folder_id not in self.folders:
                return self._error(404, "not_found", "Folder not found.", [{"id": folder_id}])
            body = await request.json()
            passcode_id = str(uuid.uuid4())
            passcode = {
                "id": passcode_id,
                "folder_id": folder_id,
                "label": body.get("label"),
                "expires_at": body.get("expires_at"),
                "max_uses": body.get("max_uses"),
                "max_devices": body.get("max_devices"),
                "one_time": body.get("one_time", False),
                "use_count": 0,
                "device_count": 0,
                "revoked_at": None,
                "created_by": request.headers.get("x-requested-by", "system"),
                "created_at": "2026-01-01T00:00:00Z",
                # The API generates a code when none is supplied and returns it
                # exactly once, in this response.
                "code": body.get("code") or "ABCD-2345",
            }
            self.passcodes[passcode_id] = passcode
            return JSONResponse(status_code=201, content=passcode)

        @app.get("/api/v1/folders/{folder_id}/download")
        async def download_folder(folder_id: str, request: Request) -> Any:
            self._record(request)
            if folder_id not in self.folders:
                return self._error(404, "not_found", "Folder not found.", [{"id": folder_id}])
            from fastapi.responses import Response

            return Response(
                content=self.zip_bytes,
                media_type="application/zip",
                headers={"Content-disposition": "attachment; filename=dataset.zip"},
            )

        # ------------------------------------------------------- bot-users
        # Per-user consent + cookies behind /bot-users/{uid}, plus the console
        # account-linking surface, mirroring the pinned contract the real
        # backend implements.

        # Declared before /bot-users/{telegram_id} so "admin-ids" is not
        # swallowed by the id route.
        @app.get("/api/v1/bot-users/admin-ids")
        async def get_admin_ids(request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            return {"ids": list(self.admin_ids)}

        @app.post("/api/v1/bot-users/{telegram_id}/link")
        async def link_account(telegram_id: int, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            # The real contract pins the acting user: X-Requested-By must be
            # the linking Telegram id. Lenient — only enforced when present,
            # so callers that skip the header still work.
            requested_by = request.headers.get("x-requested-by")
            if requested_by and requested_by != f"telegram:{telegram_id}":
                return self._error(
                    403, "forbidden", "X-Requested-By must be the linking telegram id."
                )
            body = await request.json()
            code = body.get("code")
            confirm = bool(body.get("confirm"))
            self.link_redeems.append(
                {
                    "telegram_id": telegram_id,
                    "code": code,
                    "confirm": confirm,
                    "requested_by": requested_by,
                }
            )
            email = self.link_codes.get(code)
            if email is None:
                return self._error(401, "unauthorized", "That link code is not valid.")
            if self.link_conflict:
                return self._error(
                    409,
                    "already_linked",
                    "This Telegram account is already linked to a different console account.",
                )
            if not confirm:
                # The dry-run: name the target account, link nothing.
                return {"account_email": email}
            account = self._link_account(email)
            # The link is made: the code was one-time, the id is linked now.
            self.link_codes.pop(code, None)
            self.linked[telegram_id] = {
                "email": account["account_email"],
                "name": account["account_name"],
                "role": account["account_role"],
            }
            return account

        @app.get("/api/v1/bot-users/{telegram_id}")
        async def get_bot_user(telegram_id: int, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            linked = self.linked.get(telegram_id)
            record = self.bot_users.get(telegram_id)
            if record is None and linked is None:
                return self._error(404, "not_found", f"No bot user {telegram_id}.")
            if record is None:
                # A linked user has a row even before /cookies creates one.
                record = self._empty_bot_user(telegram_id)
            return {
                **record,
                "linked_email": linked["email"] if linked else None,
                "linked_name": linked.get("name") if linked else None,
                "linked_role": linked.get("role") if linked else None,
            }

        @app.post("/api/v1/bot-users/{telegram_id}/consent")
        async def create_consent(telegram_id: int, request: Request) -> Any:
            self._record(request)
            failure = self._maybe_fail()
            if failure is not None:
                return failure
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            body = await request.json()
            if body.get("consent_version") != self.consent_version:
                return self._error(
                    422,
                    "version_mismatch",
                    "The consent text has changed — run /cookies again.",
                )
            record = self.bot_users.setdefault(
                telegram_id, self._empty_bot_user(telegram_id)
            )
            record.update(
                {
                    "consented": True,
                    "consented_at": record.get("consented_at") or "2026-10-28T00:00:00Z",
                    "consent_version": body.get("consent_version"),
                }
            )
            return record

        @app.delete("/api/v1/bot-users/{telegram_id}")
        async def delete_bot_user(telegram_id: int, request: Request) -> Any:
            self._record(request)
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            self.bot_users.pop(telegram_id, None)
            return Response(status_code=204)

        @app.post("/api/v1/bot-users/{telegram_id}/cookies")
        async def upload_cookies(telegram_id: int, request: Request) -> Any:
            # Multipart with a `file` part (text/plain); the raw body is
            # recorded like the video upload route does.
            self._record(request)
            failure = self._maybe_fail()
            if failure is not None:
                return failure
            unauth = self._auth(request)
            if unauth is not None:
                return unauth
            raw = await request.body()
            self.cookie_uploads.append(
                {
                    "content_type": request.headers.get("content-type", ""),
                    "bytes": len(raw),
                    "requested_by": request.headers.get("x-requested-by"),
                }
            )
            if not raw:
                return self._error(422, "validation_failed", "The cookies file is empty.")
            record = self.bot_users.setdefault(
                telegram_id, self._empty_bot_user(telegram_id)
            )
            record["cookies_present"] = True
            record["cookies_updated_at"] = "2026-10-28T00:00:00Z"
            return {
                "cookies_present": True,
                "cookies_updated_at": record["cookies_updated_at"],
            }


def make_backend(token: str = "test-service-token") -> FakeBackend:
    """Create a fresh :class:`FakeBackend`."""
    return FakeBackend(token=token)
