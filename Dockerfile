# syntax=docker/dockerfile:1.7
# Single-image build: frontend (node) → backend (python). Final stage serves both.

# ────────────────────────────────────────────────────────────────
# Stage 1 — frontend build
# ────────────────────────────────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /build

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY frontend/ ./
RUN npm run build
# Output: /build/dist


# ────────────────────────────────────────────────────────────────
# Stage 2 — python deps
# ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS deps
ENV POETRY_VERSION=1.8.3 \
    POETRY_HOME=/opt/poetry \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - \
    && ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

WORKDIR /app
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --without dev


# ────────────────────────────────────────────────────────────────
# Stage 3 — runtime
# ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps from stage 2
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Backend code
COPY backend/ ./backend/
COPY alembic.ini ./alembic.ini

# Bundled SinasPackage YAML (downloadable from the admin UI / API)
COPY package/ ./package/

# Frontend build into the static dir FastAPI serves
COPY --from=frontend /build/dist ./backend/app/static

# Boot script (runs migrations, then uvicorn)
COPY scripts/start.sh /usr/local/bin/start
RUN chmod +x /usr/local/bin/start

EXPOSE 8080
ENV SGR_PORT=8080
CMD ["/usr/local/bin/start"]
