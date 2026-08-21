#!/usr/bin/env bash
set -euo pipefail

cd /app

echo "[sgr] running migrations…"
alembic upgrade head

echo "[sgr] starting uvicorn on port ${SGR_PORT:-8080}"
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${SGR_PORT:-8080}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
