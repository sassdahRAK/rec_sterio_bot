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
| `/edit <video_id> [text]` | anyone | **Edit the transcript from the chat.** With text, saves immediately; without, opens a draft and the next plain message becomes the new version. The previous version is always kept. |
| `/cancel` | anyone | Abandon a pending transcript edit. |
| `/collect` | anyone | **Guided TvNa record wizard** (instruction schema): paste the link → location tag → collection method → notes. Assigns the next `PP-NNN` and prints the full record back. |
| `/record <video_id>` | anyone | Show one item's TvNa record in the platform's field order. |
| `/records` | anyone | The most recent TvNa records. |
| `/merge <video_id> [model]` | anyone | Feature 4 — queue a Whisper×Gemini merge. If no Gemini transcript exists yet it enqueues one and starts the merge automatically when it lands. Reports the diff with per-segment provenance when ready. |
| `/merges <video_id>` | anyone | Merge history; ready merges get an **Apply** button. |
| `/apply <merge_id>` | anyone | Promote a ready merge to the current transcript (creates a new version, never overwrites). |
| `/media <video_id>` | anyone | Uploads the downloaded file to the chat (the media route is authenticated, so a raw URL would 401 in a browser). The returned Telegram `file_id` is recorded against the video, so a clip under the Bot API's 20 MB `getFile` ceiling can be fetched back later. |
| `/jobs [failed\|queued\|running\|succeeded]` | admin | Recent jobs and their errors. |
| `/retry <video_id>` | admin | `POST /videos/{id}/retry` — re-enqueue a download. |
| `/jobretry <job_id>` | admin | `POST /jobs/{id}/retry` — reset a failed/dead job. |

Admins are the Telegram user ids listed in `ADMIN_TELEGRAM_IDS`. A non-admin
gets a "not authorised" message and **no backend request is made**.

## TvNa collection schema

`/collect` fills the schema the TvNa platform requires. Only two fields need a
human; the rest are taken from the download metadata or defaulted:

| Field | Source |
|---|---|
| `ID` (`PP-001`) | Allocated by the backend, under a lock, on first record. Re-recording keeps the number. |
| `Platform` | Auto-detected from the URL. |
| `Source Type` | Default `Video`; the wizard keeps it unless changed. |
| `Author/Handle` | yt-dlp `uploader_id` / `uploader`, prefixed with `@`. |
| `Date Posted` | yt-dlp `upload_date`, normalised to `YYYY-MM-DD` (or `n.d.`). |
| `Caption` | First 20 words of the description, verbatim. |
| `URL` | The `source_url` the collector pasted. |
| `Location Tag` | **Asked in the wizard** — the only required field. |
| `Collection Method` | **Asked in the wizard**: `Manual browse` or `Research API`. |
| `Collected By` | The Telegram display name. |
| `Collection Date` | UTC date the record was saved. |
| `Notes` | **Asked in the wizard** (optional; `-` to skip). |

## Telegram storage (instruction #4)

The instruction asked for the video to live in Telegram via *Telegram Drive* and
be linked from the database. Telegram Drive is a Tauri/Rust **desktop app** with
MTProto user login, so it cannot run inside this bot — and the Bot API caps
`getFile` downloads at **20 MB**, so Telegram cannot be primary storage for the
media anyway.

What is implemented instead: after `/media` uploads a file, the bot records the
returned `telegram_file_id` and `telegram_message_id` against the video. That
makes a clip under the 20 MB ceiling re-fetchable, and leaves a pointer in the
catalogue for anything larger. Local disk stays authoritative.

## Scraping TikTok and Facebook (instruction #5)

Both platforms are handled by **yt-dlp**, already a dependency — no separate
scraper repo is vendored. A block now produces an actionable error instead of a
generic failure: the bot tells the operator to supply `YTDLP_COOKIES_FILE`
(and, for TikTok, `YTDLP_IMPERSONATE`) rather than silently retrying.

Rate limits are treated as *transient* (the job retries with backoff); a bot
check is treated as *permanent* (the job fails with the remedy in the message).

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
| `PORTAL_FRONTEND_URL` | for portal links | Portal base — `https://your-host` deployed, or this machine's LAN IP in dev (`http://192.168.1.50:5174`). |
| `ADMIN_FRONTEND_URL` | for console links | Console base — `https://your-host/console` deployed, or the LAN IP on `:5173` in dev. |

**Loopback is not a working button URL.** Telegram answers `BadRequest: ... is
invalid: wrong http url` for `localhost`/`127.0.0.1`/`[::1]` and fails the whole
`sendMessage`, so such a button is hidden and a one-time warning names the
variable to fix. A **LAN address is accepted** (probed against the live Bot
API), so local development sets these to the Mac's IP — then the "Go to portal"
button opens the portal on the operator's phone, on the same Wi-Fi. The Vite dev
servers bind `0.0.0.0` for exactly this (`host: true` in both `vite.config.ts`).

"Go to portal" points at the portal's per-video page (`/video/<id>`); admins
also get "Go to admin" for the console's per-video page. That portal page is
also what a `/media` reply links to — as a button *and* as a spelled-out URL —
when the file is too large for Telegram (a bot may upload at most 50 MB) or the
upload fails (the link to Telegram is slow: a 13.5 MB clip measured 172 s, so
the write budget is 300 s). See `deliver_media` in `handlers/media.py`.

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
