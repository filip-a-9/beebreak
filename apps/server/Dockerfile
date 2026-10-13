# Build from the repository root:
#   docker build -f apps/server/Dockerfile -t breakroom-server .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps apps
# Server and its workspace dependency only; no dev group, no Qt.
RUN uv sync --frozen --no-dev --package breakroom-server

FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin breakroom
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER breakroom
EXPOSE 8000
CMD ["sh", "-c", "breakroom-server migrate && exec breakroom-server serve --host 0.0.0.0"]
