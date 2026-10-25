"""A tiny in-memory fake of the recsterio backend API.

It serves exactly the shapes frozen in ``docs/API_CONTRACT.md`` so the bot
can be exercised end-to-end without the real backend running.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

__all__ = ["FakeBackend", "make_backend"]

DEFAULT_STATUS_SCRIPT = ["pending", "downloading", "downloaded", "transcribed"]


class FakeBackend:
    """Mutable in-memory backend state."""

    def __init__(self, token: str = "test-service-token") -> None:
        self.token = token
        self.videos: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.merges: dict[str, dict[str, Any]] = {}
        #: statuses a newly created merge walks through, one per GET /merges/{id}
        self.merge_status_script: list[str] = ["ready"]
        self.requests: list[dict[str, Any]] = []
        self.status_script: list[str] = list(DEFAULT_STATUS_SCRIPT)
        self.transcript_content: str = "Hello world. This is a short transcript."
        self.transcript_language: str = "en"
        self.long_transcript_content: str | None = None
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

    # -- routes ------------------------------------------------------------
    def _register_routes(self) -> None:
        app = self.app

        @app.get("/api/v1/health")
        async def health() -> dict[str, Any]:
            return {"status": "ok", "db": True, "worker_alive": True}

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


def make_backend(token: str = "test-service-token") -> FakeBackend:
    """Create a fresh :class:`FakeBackend`."""
    return FakeBackend(token=token)
