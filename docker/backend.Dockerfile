# syntax=docker/dockerfile:1
# Harness backend — FastAPI + Uvicorn (Python 3.11, uv-managed deps).
FROM ghcr.io/astral-sh/uv:0.7.16 AS uv

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# curl: healthcheck; nodejs/npm: stdio MCP servers (memory/fetch via npx).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# --- dependency layer (cache-friendly) ---
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# --- project source + editable install ---
COPY harness ./harness
COPY scripts ./scripts
COPY mcp_servers.json ./
RUN uv sync --frozen --no-dev

# --- runtime data paths (persisted via compose volume) ---
ENV HARNESS_DB_PATH=/app/data/harness.db \
    JAGENT_WORKSPACE_BASE_DIR=/app/data/workspaces \
    HARNESS_LOG_DIR=/app/data/logs \
    HARNESS_PORT=8000

RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=10 \
    CMD curl -fsS http://localhost:8000/openapi.json >/dev/null || exit 1

# Linux container: default asyncio loop handles subprocess (no Proactor needed).
CMD ["uvicorn", "harness.api.serve:app", "--host", "0.0.0.0", "--port", "8000"]
