# syntax=docker/dockerfile:1
# recsterio Telegram bot. Long-polling only: no inbound port, no volume,
# and it MUST run as a single replica (a second instance gets
# "Conflict: terminated by other getUpdates request").
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY bot ./bot
USER app

CMD ["python", "-m", "bot"]
