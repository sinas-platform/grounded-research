#!/usr/bin/env bash
# Renders package/sinas-grounded-research.yaml with SGR_URL substituted in.
# Usage:
#   SGR_URL=https://sgr.example.com ./scripts/render-package.sh | sinas package install -
set -euo pipefail

: "${SGR_URL:?SGR_URL must be set (e.g. http://host.docker.internal:8080 for local docker-compose)}"

HERE="$(cd "$(dirname "$0")/.." && pwd)"
sed "s|__SGR_URL__|${SGR_URL}|g" "${HERE}/package/sinas-grounded-research.yaml"
