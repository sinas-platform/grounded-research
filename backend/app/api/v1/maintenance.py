"""Operator maintenance surface: corpus completeness and entity dedup.

Completeness is data-derived (the same truth the operations guide's gate
query gives) — unit/run statuses are bookkeeping and are deliberately not
consulted here. Dedup apply runs as a background task; report is
synchronous but can take minutes on large corpora.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller
from app.db import AsyncSessionLocal, get_session

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_log = logging.getLogger("sgr.maintenance")

_dedup_jobs: dict[str, dict] = {}


@router.get("/completeness")
async def completeness(
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Registered vs content vs entities vs relationships, per document
    class plus a total row. The pre-batch gate: run this before firing any
    question batch and before treating a corpus as done."""
    _ = caller
    rows = (
        await session.execute(text("""
            SELECT coalesce(dc.name, '(unclassified)') AS document_class,
              count(*) AS registered,
              count(*) FILTER (WHERE coalesce(length(dv.content_md),0) > 0) AS with_content,
              count(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM entity_mention m WHERE m.document_id = d.id)) AS with_entities,
              count(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM relationship r WHERE r.evidence_document_id = d.id)) AS with_relationships
            FROM document d
            LEFT JOIN document_class dc ON dc.id = d.document_class_id
            LEFT JOIN document_version dv ON dv.id = d.current_version_id
            GROUP BY 1 ORDER BY 2 DESC"""))
    ).mappings().all()
    out = [dict(r) for r in rows]
    total = {
        "document_class": "(total)",
        "registered": sum(r["registered"] for r in out),
        "with_content": sum(r["with_content"] for r in out),
        "with_entities": sum(r["with_entities"] for r in out),
        "with_relationships": sum(r["with_relationships"] for r in out),
    }
    return {"classes": out, "total": total}


class DedupApplyIn(BaseModel):
    mode: str = Field(pattern="^(exact|llm)$")
    tighten: bool = True
    types: list[str] | None = None


@router.post("/resolve-replay")
async def resolve_replay(
    dry_run: bool = False,
    caller: CallerIdentity = Depends(get_caller),
):
    """Replay the unresolved-relationship queue against learned keys, then
    rematerialize annotations for the entities that gained edges. Idempotent
    and LLM-free; also runs automatically when an ingestion run completes."""
    from app.services.key_replay import run as replay_run

    return await replay_run(write=not dry_run)


@router.post("/dedup/report")
async def dedup_report(caller: CallerIdentity = Depends(get_caller)):
    """Candidate counts + samples; changes nothing. Loads every active
    entity — expect minutes on large corpora."""
    _ = caller
    from app.entity_dedup import find_pairs

    exact, fuzzy = await find_pairs()
    return {
        "exact_pairs": len(exact),
        "fuzzy_pairs": len(fuzzy),
        "fuzzy_samples": [
            {"type": t, "jaccard": j, "a": a.canonical_form[:120],
             "b": b.canonical_form[:120]}
            for a, b, j, t in fuzzy[:25]
        ],
    }


@router.post("/dedup/apply")
async def dedup_apply(
    body: DedupApplyIn, caller: CallerIdentity = Depends(get_caller)
):
    """Start a merge pass in the background; poll /dedup/jobs/{id}.
    Exact mode merges identical normalized forms; llm mode judges the
    tightened fuzzy pairs. Both repoint relationship edges afterwards."""
    _ = caller
    job_id = str(uuid.uuid4())
    _dedup_jobs[job_id] = {"status": "running", "mode": body.mode}

    async def run() -> None:
        try:
            from app.entity_dedup import run_apply

            summary = await run_apply(
                mode=body.mode, tighten=body.tighten, types=body.types)
            _dedup_jobs[job_id].update(status="completed", **summary)
        except Exception as exc:  # noqa: BLE001
            _log.exception("dedup job %s failed", job_id)
            _dedup_jobs[job_id].update(status="failed", error=str(exc)[:500])

    asyncio.create_task(run())
    return {"job_id": job_id, "status": "running"}


@router.get("/dedup/jobs/{job_id}")
async def dedup_job(job_id: str, caller: CallerIdentity = Depends(get_caller)):
    _ = caller
    return _dedup_jobs.get(job_id) or {"status": "unknown"}


async def repoint_merged_relationships() -> int:
    """Repoint relationship endpoints from merged-away entities to their
    terminal survivors (merge chains resolved). The merge itself repoints
    mentions; edges were historically missed — this closes that gap."""
    async with AsyncSessionLocal() as session:
        n = (await session.execute(text("""
            WITH RECURSIVE chain AS (
              SELECT id AS orig, merged_into_id AS next FROM entity
              WHERE merged_into_id IS NOT NULL
              UNION ALL
              SELECT c.orig, e.merged_into_id FROM chain c
              JOIN entity e ON e.id = c.next WHERE e.merged_into_id IS NOT NULL
            ), survivor AS (
              SELECT DISTINCT ON (orig) orig, next AS final FROM chain
              ORDER BY orig, next
            ), s_upd AS (
              UPDATE relationship r SET source_id = s.final
              FROM survivor s WHERE r.source_id = s.orig RETURNING 1
            ), t_upd AS (
              UPDATE relationship r SET target_id = s.final
              FROM survivor s WHERE r.target_id = s.orig RETURNING 1
            )
            SELECT (SELECT count(*) FROM s_upd) + (SELECT count(*) FROM t_upd)
        """))).scalar()
        await session.commit()
    return int(n or 0)
