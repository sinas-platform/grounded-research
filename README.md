# Sinas Grounded Research

> ⚠️ **Status: alpha — built in the open.** APIs, schemas, and the package
> contract change between commits. Don't depend on this in production yet.
> Issues and PRs are welcome; expect breaking changes.

Stateful, multi-agent document indexing and retrieval system. Built on top of [Sinas](https://github.com/sinas-platform/sinas).

SGR turns unstructured documents into a structured, filterable graph and exposes agentic search and synthesis on top of it. Every claim in a synthesized answer is bound to a specific span in a specific document version.

## Architecture

- **Backend**: FastAPI + SQLAlchemy (asyncpg) + Postgres. Owns the SGR domain model (document classes, properties, entities, relationships, dossiers, results, answers).
- **Frontend**: Vite + React + TypeScript + Tailwind. Admin UI for configuration and review.
- **Sinas package** (`package/sinas-grounded-research.yaml`): defines the agents, connector, collection, and post-upload function that Sinas needs to install in order to drive SGR.
- **Single image**: one Dockerfile builds the frontend and the backend; FastAPI serves the static SPA. Suitable for Render, Heroku, Scaleway Serverless Containers, Fly, etc.

SGR depends on a running Sinas instance for agents, file storage, RBAC, and skills. Standalone deployment is not supported in v1.

> **Sinas settings required** (in the Sinas deployment's environment — its
> `.env` for docker-compose; applies to every environment running the SGR
> package, including the deployed one):
>
> - `MAX_TOOL_ITERATIONS=50` — Sinas defaults to 25 consecutive tool rounds
>   per agent job; SGR's deep-search loop (playbook → introspect → mutate →
>   introspect … → publish) legitimately needs more, and at the default the
>   agent is killed mid-pipeline with "Tool iteration limit (25) reached",
>   leaving draft results unpublished and synthesis never invoked.
> - `FUNCTION_TIMEOUT=900` — Sinas applies this (default 300s) as the timeout
>   for ALL tool executions, including `call_agent` sub-agent invocations.
>   SGR's deep-search sub-agents run 5–10 minutes, so at the default the
>   orchestrator's call times out and it re-dispatches duplicate sub-searches
>   while the originals are still running.
> - `AGENT_JOB_TIMEOUT=1800` — keeps sub-agent jobs alive for the longest
>   expected deep-search run.

## Quick start (local dev)

```bash
cp .env.example .env       # fill in SINAS_URL and SGR_DATABASE_URL
docker-compose up          # starts Postgres + SGR backend + frontend dev server
```

Or run pieces separately:

```bash
# backend
poetry install
poetry run alembic upgrade head
poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

# frontend (separate terminal)
cd frontend && npm install && npm run dev
```

Install the package into your Sinas instance:

```bash
sinas package install ./package/sinas-grounded-research.yaml
```

The installer will prompt for three values:

| Variable | Type | What to give it |
|---|---|---|
| `SGR_URL` | text | URL where SGR is reachable from inside the Sinas containers — `http://host.docker.internal:8080` for local docker-compose, or the deployed URL |
| `PRIMARY_LLM` | LLM provider | Provider for capable agents (synthesis, search orchestration, validation). Pick a strong reasoning model. |
| `CHEAP_LLM` | LLM provider | Provider for ingestion enrichers (classifier, extractor, summarizer, etc.). Pick a fast/cheap model. |

Or supply them non-interactively:

```bash
sinas package install ./package/sinas-grounded-research.yaml \
  --var SGR_URL=http://host.docker.internal:8080 \
  --var PRIMARY_LLM=<provider-id-or-name> \
  --var CHEAP_LLM=<provider-id-or-name>
```

## Single-image deploy

```bash
docker build -t sinas-grounded-research .
docker run -p 8080:8080 \
  -e SGR_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/sgr \
  -e SINAS_URL=https://sinas.example.com \
  sinas-grounded-research
```

The image runs migrations on boot, then serves the API at `/api/*` and the SPA on every other path.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `SGR_DATABASE_URL` | yes | Postgres connection string (asyncpg driver) |
| `SINAS_URL` | yes | Base URL of the Sinas instance |
| `SGR_AUTH_MODE` | no | `sinas` (default — per-user bearer tokens) or `simplified` (single admin API key) |
| `SINAS_API_KEY` | iff `simplified` | Sinas API key SGR uses for all Sinas callbacks; the user it resolves to (via `/auth/me`) becomes the single admin owner |
| `SGR_PORT` | no | Default `8080` |
| `SGR_LOG_LEVEL` | no | Default `INFO` |
| `SGR_CORS_ORIGINS` | no | Comma-separated origins for CORS |

See `.env.example`.

## Repository layout

```
backend/             FastAPI app, SQLAlchemy models, Alembic migrations
frontend/            Vite + React admin UI
package/             Sinas Package YAML (sinas-grounded-research.yaml) and skills
Dockerfile           Single-image build (multi-stage)
docker-compose.yml   Local dev environment
```
