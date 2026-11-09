# Advance Quiz Bot — single-service image for Railway (also works on any
# Docker host / docker compose).
#
# One container = both Telegram bots + the Mini App HTTP server, because a
# PaaS routes external traffic to exactly one port ($PORT) per service.
# Start with `python run.py` (see railway.json / Procfile).
#
# Python 3.11 matches runtime.txt; every pinned dependency has a prebuilt
# cp311 manylinux wheel, so no compiler toolchain is needed in the image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# The app thinks in IST (scheduled quizzes, premium expiry, PDF timestamps).
# Containers default to UTC, so logs and any naive local-time string would be
# 5h30m off; tzdata is what makes TZ actually resolve. Change this if you run
# the bot for a different audience.
ENV TZ=Asia/Kolkata

WORKDIR /app

# System libraries WeasyPrint (PDF quiz reports) links against, plus real
# fonts so the generated PDFs render text/symbols instead of empty boxes.
# NOTE: keep this list in one layer AND clean apt lists in the same RUN --
# otherwise the image grows by ~100MB and Railway builds get slower.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libglib2.0-0 \
        libffi8 \
        shared-mime-info \
        fontconfig \
        fonts-dejavu-core \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (own layer) so code edits don't re-install everything.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# The Mini App must listen on all interfaces; the actual port comes from
# $PORT (Railway/router) and falls back to MINI_APP_PORT when $PORT is unset
# (local `docker run`). See quizbot/shared/config.py.
ENV MINI_APP_HOST=0.0.0.0 \
    MINI_APP_PORT=8080

EXPOSE 8080

# Health/uptime is handled by the platform, not by a Docker HEALTHCHECK:
# Railway polls /healthz (railway.json -> deploy.healthcheckPath) and restarts
# on failure (ON_FAILURE). A HEALTHCHECK here would only mark the bots-only
# `docker compose` services as "unhealthy" forever, since they run no web
# server at all. `curl` is installed purely for `docker exec` debugging.
CMD ["python", "run.py"]
