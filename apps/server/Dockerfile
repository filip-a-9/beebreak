# Build from the repository root:
#   docker build -f apps/server/Dockerfile -t beebreak-server .
# Base images are pinned by digest (the tag is the human-readable name; bump both
# together, resolving the digest with `docker pull <tag>`).
# python:3.12-slim
FROM python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
# ghcr.io/astral-sh/uv:0.11.32
COPY --from=ghcr.io/astral-sh/uv@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps apps
# Server and its workspace dependency only; no dev group, no Qt.
RUN uv sync --frozen --no-dev --package beebreak-server

# python:3.12-slim (same digest as the build stage)
FROM python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin beebreak
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER beebreak
EXPOSE 8000
# Migrations run in the one-shot `migrate` compose service, not here.
# One worker only: the server refuses any other count.
CMD ["beebreak-server", "serve", "--host", "0.0.0.0"]
