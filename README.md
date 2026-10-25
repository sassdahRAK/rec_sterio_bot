# recsterio — Telegram bot

The user-facing surface of **recsterio**. Send it a video link; it queues the
download on the backend, notifies you when it finishes, and serves the
transcript.

This package is a **pure HTTP client** of the backend API described in
[`../docs/API_CONTRACT.md`](../docs/API_CONTRACT.md). It never imports backend
code and never touches the database.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | anyone | Welcome + usage. `/start <video_id>` shows that video's status (deep link). |
| *(plain message with a URL)* | anyone | `POST /videos`, replies with the new video id, then polls in the background and notifies on completion. |
| `/status <video_id>` | anyone | Status, progress %, file size, duration, transcript availability, media link. |
| `/transcript <video_id>` | anyone | Current transcript — inline text when short, a `.txt` document when long. Includes the version number. |
| `/transcripts <video_id>` | anyone | Every transcript version, newest first. |
| `/merge <video_id> [model]` | anyone | Feature 4 — queue a Whisper×Gemini merge. If no Gemini transcript exists yet it enqueues one and starts the merge automatically when it lands. Reports the diff with per-segment provenance when ready. |
| `/merges <video_id>` | anyone | Merge history; ready merges get an **Apply** button. |
| `/apply <merge_id>` | anyone | Promote a ready merge to the current transcript (creates a new version, never overwrites). |
| `/media <video_id>` | anyone | Uploads the downloaded file to the chat (the media route is authenticated, so a raw URL would 401 in a browser). |
| `/jobs [failed\|queued\|running\|succeeded]` | admin | Recent jobs and their errors. |
| `/retry <video_id>` | admin | `POST /videos/{id}/retry` — re-enqueue a download. |
| `/jobretry <job_id>` | admin | `POST /jobs/{id}/retry` — reset a failed/dead job. |

Admins are the Telegram user ids listed in `ADMIN_TELEGRAM_IDS`. A non-admin
gets a "not authorised" message and **no backend request is made**.

## Install

```bash
cd recsterio/bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env    # then edit it — never commit .env
```

| Variable | Required | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from @BotFather. The bot exits with a clear message if it is missing. |
| `BOT_API_TOKEN` | yes | Service token sent as `Authorization: Bearer …`. |
| `BOT_API_BASE_URL` | yes (default given) | Backend base URL, including `/api/v1`. |
| `ADMIN_TELEGRAM_IDS` | for admin commands | Comma separated Telegram user ids. |

All secrets come from the environment only. The token is never logged, never
echoed, and never written to a file by the bot.

## Run

```bash
cd recsterio/bot
.venv/bin/python -m bot
```

Polling (long-polling) is used, so no public webhook is needed.

## Test

The suite runs against an in-process fake of the backend
(`tests/fake_backend.py`, FastAPI + `httpx` ASGI transport) — no real backend
or Telegram connection required.

```bash
cd recsterio/bot
.venv/bin/python -m pytest -q
```

## How the completion notification works

`POST /videos` returns immediately with a `pending` video. The bot then:

1. registers a watch task in a per-video registry (a video is never polled
   twice) — `asyncio` tasks, so the bot never blocks;
2. polls `GET /videos/{id}` every `POLL_INTERVAL_SECONDS` (default 5 s) for at
   most `POLL_TIMEOUT_SECONDS` (default 1200 s ≈ 20 min);
3. notifies the chat when the video reaches `downloaded` (media link + current
   transcript status) and again when it reaches `transcribed` (transcript
   ready) or `failed` (with the backend's error message).

Watchers are cancelled when the bot shuts down.

## Error handling

The backend's error envelope

```json
{"error": {"code": "...", "message": "...", "details": [...]}}
```

is turned into a Telegram message that always contains the machine-readable
`code`, e.g. `[validation_failed] source_url must be a valid http(s) URL.` and
`[duplicate_video] …`. Network failures are retried with exponential backoff
(`MAX_RETRIES`, `RETRY_BACKOFF_SECONDS`); once the retries are exhausted the
user is told to try again. A global `Application.add_error_handler` catches
anything that slips through so the bot never dies on a single bad request.
