# SGR — agent notes

Project-specific instructions for AI assistants and human contributors working on SGR. 

## Project shape (the absolute minimum)

- Stateful, multi-agent document indexing + retrieval. Open-source. Apache-2-ish license.
- FastAPI + asyncpg + Postgres (backend), Vite + React + TypeScript (frontend), single Docker image.
- Runs **on top of Sinas** (separate open-source project): Sinas provides the agent runtime, function execution, file storage, RBAC, queues. SGR is a Sinas package — see `package/sinas-grounded-research.yaml`.
- The SGR backend owns the domain (document classes, properties, entities, relationships, dossiers, results, answers). The Sinas agents drive ingestion and retrieval by calling SGR's connector.

Read `README.md` for setup.

## ADRs

ADRs live in `docs/adrs/` with the `YYYY-MM-DD-slug.md` filename pattern; see `docs/adrs/README.md` for criteria and `docs/adrs/template.md` for the format.

## Things to know before editing

**Single repo, two deploy artifacts.** The Docker image bakes the frontend build into the backend's static dir. Editing frontend code without rebuilding the image leaves the running container with stale UI. For dev iteration, prefer the local-dev workflow (Vite dev server + local uvicorn) over docker-compose rebuilds.

**Migrations are tracked.** Don't gitignore `backend/app/alembic/versions/`. Every schema change needs an Alembic revision. Numbered sequentially (currently up to 0006).

**SGR depends on Sinas being reachable.** Local dev requires Sinas running on `host.docker.internal:8000` (or wherever `SINAS_URL` points) for agent invocations and auth. Documents are Grove's own record: uploads register directly with SGR (content-hash + source-identity dedup); there is no Sinas collection or post-upload function.

**Authentication has two modes.** `SGR_AUTH_MODE=sinas` (default, per-user bearer tokens proxied to Sinas) and `SGR_AUTH_MODE=simplified` (single admin API key). The frontend's `/api/v1/me` endpoint reflects which.

## Sinas is a shared platform

The Sinas codebase is shared with other deployments. Avoid patching Sinas to solve SGR-specific problems unless you've established that the problem is genuinely shared and the CTO has agreed. SGR-side workarounds beat Sinas-side patches that no one else asked for.

If you must change Sinas (we did this session for the worker `/tmp` issue), the change should be defensible as a general improvement — not a SGR-specific tweak.

## Don't

- Don't commit per-machine agent state (e.g., `.claude/settings.local.json`, `.cursor/state`). Project-shared agent config is fine to commit.
- Don't add deployment-specific logic to SGR, and don't name a deployment anywhere in the repo — not in code, comments, docs, or commit messages. SGR is the open-source platform; deployments configure it via the package + admin UI.
- Don't bypass the discovery proposal queue when writing schema. The approval workflow exists because the LLM gets it wrong sometimes; manual config writes are fine, but pipeline-driven config writes go through proposals.
