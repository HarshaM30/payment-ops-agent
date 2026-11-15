# syntax=docker/dockerfile:1.7
# Multi-stage image for the API. Multi-arch: python:3.12-slim and every wheel we use
# (psycopg[binary] included) exist for linux/arm64 and linux/amd64.

# ---------------------------------------------------------------- build: deps + app
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first, so this layer stays cached until uv.lock changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime
RUN groupadd --system --gid 10001 payops \
    && useradd --system --uid 10001 --gid payops --no-create-home --shell /usr/sbin/nologin payops

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production

USER payops
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# Migrations run on start (idempotent), then uvicorn replaces the shell so it receives
# SIGTERM directly and shuts down gracefully: stop accepting, finish in-flight requests
# and their background runs (up to 30s), then exit.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn payops.main:create_app --factory --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 30 --no-access-log"]
