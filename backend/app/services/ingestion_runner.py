"""Ingestion — one in-process pipeline, no stages, no batches, no polling.

A run selects documents (RunFilter) and parts (extract, ground, resolve,
relationships, dossiers — default all). `POST /ingestion/runs` materializes
one unit per document and spawns a single in-process worker task that runs
the selected parts per document, in order, updating unit rows and run
counters as it goes — progress is live from the database, and a GET on the
run is a plain read.

History: this replaced a stage architecture (classifier/summarizer/
property/entity/relationship extractor agents, later a one-shot stage plus
agentic secondary stages) whose secondary batches were submitted from the
GET handler — unpolled runs sat unsubmitted forever, and the agentic
relationship stage cost ~12 LLM calls per document. Every pass is now a
tool-less call driven by this worker.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import (
    Document,
    EntityMention,
    IngestionRun,
    IngestionRunUnit,
    PropertyValue,
)
from app.schemas.ingestion import ALL_PARTS, RunFilter

log = logging.getLogger(__name__)

# The pipeline's parts, in execution order, with UI labels.
PARTS: dict[str, str] = {
    "extract": "Classify + metadata + entity mentions",
    "ground": "Grounding gate (hide unsupported names)",
    "resolve": "Entity resolution (link mentions)",
    "relationships": "Relationship extraction",
    "dossiers": "Dossier assignment",
}

# The single value the unit rows carry in their legacy `stage` column.
UNIT_STAGE = "pipeline"


async def _wipe_extracted_artifacts(
    session: AsyncSession, document_id: uuid.UUID
) -> None:
    """Delete stale auto-extracted artifacts so an extract rerun doesn't
    duplicate. Manually-authored / locked entries are preserved. Only the
    extract part wipes — subset reruns build on what exists."""
    await session.execute(
        delete(PropertyValue)
        .where(PropertyValue.document_id == document_id)
        .where(PropertyValue.method == "auto")
        .where(PropertyValue.locked.is_(False))
    )
    await session.execute(
        delete(EntityMention).where(EntityMention.document_id == document_id)
    )


# ─────────────────────────────────────────────────────────────
# Document selection
# ─────────────────────────────────────────────────────────────


async def _select_documents(session: AsyncSession, f: RunFilter) -> list[uuid.UUID]:
    stmt = select(Document.id)
    # `is not None`, not truthiness: an explicit empty list means ZERO
    # documents, never "no filter". A caller that builds document_ids from
    # an upstream step that produced nothing must get an empty (refused)
    # run — treating [] as falsy once selected the entire corpus for a
    # full-pipeline rerun (5 Aug, cancelled at 53 of 1,975 documents).
    if f.document_ids is not None:
        stmt = stmt.where(Document.id.in_(f.document_ids))
    if f.staged_only:
        stmt = stmt.where(Document.staged.is_(True))
    else:
        class_clauses = []
        if f.document_class_ids:
            class_clauses.append(Document.document_class_id.in_(f.document_class_ids))
        if f.include_unclassified:
            class_clauses.append(Document.document_class_id.is_(None))
        if f.max_classification_confidence is not None:
            class_clauses.append(
                Document.classification_confidence <= f.max_classification_confidence
            )
        if class_clauses:
            stmt = stmt.where(or_(*class_clauses))
        if not f.include_staged:
            stmt = stmt.where(Document.staged.is_(False))
    if f.created_since:
        stmt = stmt.where(Document.created_at >= f.created_since)
    if f.created_until:
        stmt = stmt.where(Document.created_at <= f.created_until)
    if f.limit is not None and f.limit > 0:
        stmt = stmt.order_by(Document.created_at).limit(f.limit)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def expand_filter(session: AsyncSession, f: RunFilter) -> list[uuid.UUID]:
    """Used by the API to preview document counts before committing a run."""
    return await _select_documents(session, f)


async def materialize_run(session: AsyncSession, run: IngestionRun) -> int:
    """Insert one IngestionRunUnit per selected document. Returns count."""
    f = RunFilter(**(run.filter or {}))
    doc_ids = await _select_documents(session, f)
    now = datetime.now(timezone.utc)
    units = [
        IngestionRunUnit(
            run_id=run.id,
            document_id=doc_id,
            stage=UNIT_STAGE,
            status="pending",
            attempts=0,
            created_at=now,
        )
        for doc_id in doc_ids
    ]
    session.add_all(units)
    return len(units)


# ─────────────────────────────────────────────────────────────
# The worker
# ─────────────────────────────────────────────────────────────


async def _run_pipeline_inprocess(
    run_id: uuid.UUID,
    doc_ids: list[uuid.UUID],
    parts: tuple[str, ...] = ALL_PARTS,
) -> None:
    """Per document, run the selected parts in order — extract (wipes
    stale artifacts first), ground, resolve, relationships, dossiers.
    Unit rows and run counters update per document, so progress is live.
    Subset runs (e.g. only relationships after a config change) skip the
    wipe and the unselected passes."""
    from app.services.dossier_oneshot import assign_dossiers
    from app.services.entity_resolver import resolve_unlinked
    from app.services.grounding_gate import ground_documents
    from app.services.ingestion_oneshot import oneshot_ingest
    from app.services.relationship_oneshot import extract_relationships

    # The task is created before the creating transaction commits
    # (runs.py submits inline, then commits), so the run row may not be
    # visible yet on this fresh session. Large runs lose that race every
    # time: the first cancellation check read None and the whole run sat
    # in "running" forever with no worker. Wait for visibility briefly
    # before treating the run as gone.
    for _ in range(10):
        async with AsyncSessionLocal() as session:
            if await session.get(IngestionRun, run_id) is not None:
                break
        await asyncio.sleep(1)

    for did in doc_ids:
        # honor cancellation between documents
        async with AsyncSessionLocal() as session:
            run = await session.get(IngestionRun, run_id)
            if run is None or run.status in ("cancelled", "failed"):
                log.info("pipeline run %s stopped (status=%s)", run_id,
                         run.status if run else "gone")
                return
        error: str | None = None
        try:
            if "extract" in parts:
                async with AsyncSessionLocal() as session:
                    await _wipe_extracted_artifacts(session, did)
                    await session.commit()
                reports = await oneshot_ingest([did], write=True, concurrency=1)
                rep = reports[0] if reports else {}
                if rep.get("error"):
                    error = str(rep["error"])[:500]
            if error is None:
                if "ground" in parts:
                    # gate before resolving: hallucinated names must not
                    # reach the resolver or mint entities
                    await ground_documents([did], write=True)
                if "resolve" in parts:
                    await resolve_unlinked([did], write=True)
                if "relationships" in parts:
                    # one tool-less call per document against the
                    # resolved mentions
                    await extract_relationships([did], write=True)
                if "dossiers" in parts:
                    # free no-op when no dossier classes are configured
                    await assign_dossiers([did], write=True)
        except Exception as exc:  # noqa: BLE001 — per-doc isolation
            error = str(exc)[:500]
        async with AsyncSessionLocal() as session:
            unit = (
                await session.execute(
                    select(IngestionRunUnit)
                    .where(IngestionRunUnit.run_id == run_id)
                    .where(IngestionRunUnit.document_id == did)
                )
            ).scalar_one_or_none()
            run = await session.get(IngestionRun, run_id)
            if unit is not None and unit.status == "running":
                unit.status = "failed" if error else "succeeded"
                unit.error = error
                unit.completed_at = datetime.now(timezone.utc)
                if run is not None:
                    run.done_units += 1
                    if error:
                        run.failed_units += 1
            await session.commit()
    await _mark_run_terminal_if_done(run_id)


async def submit_run(session: AsyncSession, run: IngestionRun) -> None:
    """Mark units running and spawn the in-process worker. Called from the
    request handler; caller commits."""
    units = list(
        (
            await session.execute(
                select(IngestionRunUnit)
                .where(IngestionRunUnit.run_id == run.id)
                .order_by(IngestionRunUnit.created_at)
            )
        ).scalars().all()
    )
    if not units:
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        return

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    for u in units:
        u.status = "running"
        u.started_at = now
        u.attempts += 1
    run.sinas_batch_ids = {}
    parts = tuple(run.stages or ALL_PARTS)
    doc_ids = [u.document_id for u in units]
    asyncio.get_event_loop().create_task(
        _run_pipeline_inprocess(run.id, doc_ids, parts)
    )


async def _mark_run_terminal_if_done(run_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(IngestionRun, run_id)
        if run is None or run.status != "running":
            return
        pending = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(IngestionRunUnit)
                    .where(IngestionRunUnit.run_id == run_id)
                    .where(IngestionRunUnit.status.in_(["pending", "running"]))
                )
            ).scalar_one()
        )
        if pending == 0:
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
            await session.commit()


async def progress_snapshot(run: IngestionRun) -> dict[str, Any]:
    """Read-only progress: unit counts by status. The worker advances all
    state; a GET never drives anything."""
    async with AsyncSessionLocal() as session:
        counts = dict(
            (
                await session.execute(
                    select(IngestionRunUnit.status, func.count())
                    .where(IngestionRunUnit.run_id == run.id)
                    .group_by(IngestionRunUnit.status)
                )
            ).all()
        )
    return {
        "status": run.status,
        "parts": list(run.stages or ALL_PARTS),
        "units": counts,
    }
