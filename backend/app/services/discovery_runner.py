"""Discovery — no-worker design.

Lifecycle:
  pending → scanning → consolidating → completed | failed

Flow:
  - `POST /discovery/runs` calls `submit_scan(...)` inside the request handler.
    The user's bearer is in scope, so we hit Sinas's batch endpoint as the
    originating user. The scan batch_id is persisted on the run; the run
    transitions to "scanning".
  - `GET /discovery/runs/{id}` calls `progress(...)` which live-fetches batch
    status from Sinas and drives the run forward atomically:
      * scan batch terminal → submit consolidate batch (single-item agent batch)
      * consolidate batch terminal → mark run completed, store final counts
  - No background workers, no persisted user tokens. The polling is driven
    by the UI's progress requests, which always carry a fresh user bearer.

See ADR docs/adrs/2026-05-14-stateful-filter-on-result.md? No — this is the
batch-submission design from sinas/.../2026-05-14-external-app-bulk-enqueue.md.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from sinas import SinasClient
from sinas.exceptions import SinasAPIError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import (
    ConfigProposal,
    DiscoveryCandidate,
    DiscoveryRun,
    DiscoveryRunUnit,
    Document,
)
from app.schemas.ingestion import RunFilter

log = logging.getLogger(__name__)


# Per-kind agent assignment.
KIND_TO_AGENT = {
    "document_class": ("sgr", "discovery-agent"),
    "entity_type": ("sgr", "discovery-agent"),
    "relationship_definition": ("sgr", "discovery-agent"),
    "dossier_class": ("sgr", "discovery-agent"),
    "document_class_property": ("sgr", "discovery-agent"),
}

CONSOLIDATOR_AGENT = ("sgr", "discovery-consolidator-agent")


_TERMINAL_BATCH_STATUSES = {"completed", "partial", "failed", "cancelled"}


# ─────────────────────────────────────────────────────────────
# Document selection (shared with front-matter suggest)
# ─────────────────────────────────────────────────────────────


async def _select_documents(
    session: AsyncSession,
    f: RunFilter,
    parent_class_id: uuid.UUID | None,
    sample_size: int | None,
) -> list[uuid.UUID]:
    stmt = select(Document.id)
    if f.document_ids:
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
        # Discovery and FM-suggest read staged docs unconditionally — that's
        # their primary use case. The `include_staged` filter knob is ignored
        # on this path. Users wanting to scope to already-processed docs
        # should use document_class_ids (which excludes staged implicitly,
        # since staged docs have no class) or staged_only=true for the
        # opposite scope.
    if f.created_since:
        stmt = stmt.where(Document.created_at >= f.created_since)
    if f.created_until:
        stmt = stmt.where(Document.created_at <= f.created_until)
    if parent_class_id is not None:
        stmt = stmt.where(Document.document_class_id == parent_class_id)
    rows = list((await session.execute(stmt)).scalars().all())

    if sample_size is not None and len(rows) > sample_size:
        rng = random.Random(42)
        rows = rng.sample(rows, sample_size)
    return rows


async def expand_filter(
    session: AsyncSession,
    f: RunFilter,
    parent_class_id: uuid.UUID | None,
    sample_size: int | None,
) -> tuple[int, bool]:
    """Returns (count_to_scan, was_sampled). Called by the API to preview."""
    docs = await _select_documents(session, f, parent_class_id, sample_size)
    return len(docs), sample_size is not None and len(docs) >= sample_size


async def materialize_run(session: AsyncSession, run: DiscoveryRun) -> int:
    """Insert DiscoveryRunUnit rows for the selected docs. Returns count."""
    f = RunFilter(**(run.filter or {}))
    doc_ids = await _select_documents(session, f, run.parent_class_id, run.sample_size)
    now = datetime.now(timezone.utc)
    units = [
        DiscoveryRunUnit(
            run_id=run.id,
            document_id=doc_id,
            status="pending",
            attempts=0,
            created_at=now,
        )
        for doc_id in doc_ids
    ]
    session.add_all(units)
    return len(units)


# ─────────────────────────────────────────────────────────────
# Submission (called from POST /discovery/runs)
# ─────────────────────────────────────────────────────────────


def _build_scan_inputs(run: DiscoveryRun, doc_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    # input_variables are rendered into the agent's system prompt via Jinja
    # (see sinas message_service.py). The discovery-agent's system prompt
    # references {{ run_id }}, {{ kind }}, etc., so the LLM gets the run
    # context as part of its instructions rather than via the user message.
    return [
        {
            "input_variables": {
                "run_id": str(run.id),
                "kind": run.kind,
                "mode": run.mode,
                "document_id": str(doc_id),
                **(
                    {"parent_class_id": str(run.parent_class_id)}
                    if run.parent_class_id is not None
                    else {}
                ),
            },
            "message": (
                f"Read document {doc_id} and submit any candidate {run.kind}s "
                "you find via submit_discovery_candidate. Be conservative "
                "with confidence."
            ),
        }
        for doc_id in doc_ids
    ]


async def submit_scan(
    session: AsyncSession, run: DiscoveryRun, client: SinasClient
) -> None:
    """Submit the scan batch to Sinas. Called from the request handler that
    holds the user's bearer. Updates the run row in-place; caller commits."""
    units = list(
        (
            await session.execute(
                select(DiscoveryRunUnit)
                .where(DiscoveryRunUnit.run_id == run.id)
                .order_by(DiscoveryRunUnit.created_at)
            )
        ).scalars().all()
    )
    if not units:
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        return

    namespace, agent_name = KIND_TO_AGENT[run.kind]
    inputs = _build_scan_inputs(run, [u.document_id for u in units])

    result = await asyncio.to_thread(
        client.agents.submit_batch,
        namespace=namespace,
        name=agent_name,
        inputs=inputs,
        trigger_id_prefix=f"sgr:discovery:{run.id}",
    )

    batch_id = result["batch_id"]
    execution_ids = result.get("execution_ids") or []
    chat_ids = result.get("chat_ids") or []

    run.sinas_batch_ids = {**(run.sinas_batch_ids or {}), "scan": batch_id}
    run.status = "scanning"
    run.started_at = datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    for idx, u in enumerate(units):
        u.status = "running"
        u.started_at = now
        u.attempts += 1
        if idx < len(execution_ids):
            u.sinas_execution_id = execution_ids[idx]
        if idx < len(chat_ids):
            u.chat_id = chat_ids[idx]


# ─────────────────────────────────────────────────────────────
# Progress (called from GET /discovery/runs/{id})
# ─────────────────────────────────────────────────────────────


async def _reconcile_units_from_batch(
    client: SinasClient, run_id: uuid.UUID, batch_id: str
) -> None:
    """Drill into per-execution status and propagate to unit rows."""
    offset = 0
    while True:
        executions = await asyncio.to_thread(
            client.batches.list_executions, batch_id, limit=500, offset=offset
        )
        if not executions:
            break
        async with AsyncSessionLocal() as session:
            for execution in executions:
                exec_id = execution.get("execution_id") or execution.get("id")
                if exec_id is None:
                    continue
                unit = (
                    await session.execute(
                        select(DiscoveryRunUnit)
                        .where(DiscoveryRunUnit.sinas_execution_id == exec_id)
                        .where(DiscoveryRunUnit.run_id == run_id)
                    )
                ).scalar_one_or_none()
                if unit is None:
                    continue
                exec_status = (execution.get("status") or "").upper()
                ok = exec_status == "COMPLETED"
                unit.status = "succeeded" if ok else "failed"
                if not ok:
                    unit.error = execution.get("error") or f"sinas: {exec_status}"
                unit.chat_id = execution.get("chat_id") or unit.chat_id
                unit.completed_at = datetime.now(timezone.utc)
            await session.commit()
        if len(executions) < 500:
            break
        offset += 500


async def _claim_consolidate_transition(run_id: uuid.UUID) -> bool:
    """Atomically transition the run into consolidating phase. Returns True if
    THIS call won the race (and should submit the consolidator), False if
    another GET already did."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(DiscoveryRun)
            .where(DiscoveryRun.id == run_id)
            .where(DiscoveryRun.consolidating_at.is_(None))
            .values(status="consolidating", consolidating_at=now)
        )
        await session.commit()
        return result.rowcount > 0


async def _submit_consolidator(
    run_id: uuid.UUID, kind: str, client: SinasClient
) -> None:
    """Submit a single-item batch to the consolidator. Stored on the run as
    `sinas_batch_ids["consolidate"]`."""
    namespace, agent_name = CONSOLIDATOR_AGENT
    result = await asyncio.to_thread(
        client.agents.submit_batch,
        namespace=namespace,
        name=agent_name,
        inputs=[
            {
                "input_variables": {"run_id": str(run_id), "kind": kind},
                "message": (
                    "Consolidate the candidates for this run. Cluster semantic "
                    "duplicates, choose canonical names, and submit deduplicated "
                    "proposals via submit_consolidated_proposal. Each proposal "
                    "must list the supporting candidate ids it folds in."
                ),
            }
        ],
        trigger_id_prefix=f"sgr:discovery:{run_id}:consolidate",
    )
    batch_id = result["batch_id"]
    async with AsyncSessionLocal() as session:
        run = await session.get(DiscoveryRun, run_id)
        if run is not None:
            run.sinas_batch_ids = {
                **(run.sinas_batch_ids or {}),
                "consolidate": batch_id,
            }
            await session.commit()


async def _mark_completed(run_id: uuid.UUID) -> None:
    """Final reconcile when consolidate batch is terminal: update aggregate
    counts and set status to completed."""
    async with AsyncSessionLocal() as session:
        run = await session.get(DiscoveryRun, run_id)
        if run is None or run.status == "completed":
            return
        cand_count = (
            await session.execute(
                select(func.count())
                .select_from(DiscoveryCandidate)
                .where(DiscoveryCandidate.run_id == run_id)
            )
        ).scalar_one()
        run.candidate_count = int(cand_count)
        prop_count = (
            await session.execute(
                select(func.count())
                .select_from(ConfigProposal)
                .where(ConfigProposal.discovery_run_id == run_id)
            )
        ).scalar_one()
        run.proposal_count = int(prop_count)
        scanned = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(DiscoveryRunUnit)
                    .where(DiscoveryRunUnit.run_id == run_id)
                    .where(DiscoveryRunUnit.status.in_(["succeeded", "failed"]))
                )
            ).scalar_one()
        )
        failed = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(DiscoveryRunUnit)
                    .where(DiscoveryRunUnit.run_id == run_id)
                    .where(DiscoveryRunUnit.status == "failed")
                )
            ).scalar_one()
        )
        run.scanned_docs = scanned
        run.failed_docs = failed
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        await session.commit()


async def progress(run: DiscoveryRun, client: SinasClient) -> dict[str, Any]:
    """Drive the run forward and return a status snapshot.

    Called from `GET /discovery/runs/{id}`. The caller's bearer is forwarded
    to Sinas via the provided client.
    """
    batch_ids = dict(run.sinas_batch_ids or {})
    scan_batch_id = batch_ids.get("scan")
    consolidate_batch_id = batch_ids.get("consolidate")

    snapshot: dict[str, Any] = {
        "status": run.status,
        "scan": None,
        "consolidate": None,
    }

    if scan_batch_id:
        scan_status = await asyncio.to_thread(client.batches.get, scan_batch_id)
        snapshot["scan"] = scan_status
        scan_terminal = scan_status.get("status") in _TERMINAL_BATCH_STATUSES

        if scan_terminal and run.status == "scanning":
            # Reconcile unit rows once.
            await _reconcile_units_from_batch(client, run.id, scan_batch_id)
            # Atomically claim the consolidate transition.
            won = await _claim_consolidate_transition(run.id)
            if won:
                try:
                    await _submit_consolidator(run.id, run.kind, client)
                except SinasAPIError as exc:
                    async with AsyncSessionLocal() as s:
                        r = await s.get(DiscoveryRun, run.id)
                        if r is not None:
                            r.status = "failed"
                            r.error = f"consolidate submit: {exc}"
                            r.completed_at = datetime.now(timezone.utc)
                            await s.commit()
                    snapshot["status"] = "failed"
                    return snapshot
            # Refresh run state for the rest of this call.
            async with AsyncSessionLocal() as s:
                run = await s.get(DiscoveryRun, run.id)
                if run is None:
                    return snapshot
                batch_ids = dict(run.sinas_batch_ids or {})
                consolidate_batch_id = batch_ids.get("consolidate")
                snapshot["status"] = run.status

    if consolidate_batch_id:
        cons_status = await asyncio.to_thread(client.batches.get, consolidate_batch_id)
        snapshot["consolidate"] = cons_status
        if (
            cons_status.get("status") in _TERMINAL_BATCH_STATUSES
            and run.status != "completed"
        ):
            await _mark_completed(run.id)
            snapshot["status"] = "completed"

    return snapshot
