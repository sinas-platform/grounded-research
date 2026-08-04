"""Ingestion — no-worker design with classifier-first serialization.

Flow:
  - `POST /ingestion/runs` calls `submit_run(...)` inside the request handler.
    If `classifier` is among the requested stages, ONLY the classifier batch
    is submitted at this point; other stages wait. Otherwise all stage
    batches submit at once. Run transitions to "running".
  - `GET /ingestion/runs/{id}` calls `progress(...)` which:
      * Fetches live status for every submitted stage batch.
      * If classifier batch is terminal AND secondary stages haven't been
        submitted yet, atomically claim the transition and submit the
        secondary batches.
      * If every submitted batch is terminal, reconciles units and marks
        the run completed.
  - No background worker, no persisted user token. Each transition is
    driven by a polling GET that carries a fresh user bearer.

See Sinas's bulk-enqueue ADR for the underlying mechanics.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sinas import SinasClient
from sinas.exceptions import SinasAPIError
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import (
    Document,
    EntityMention,
    IngestionRun,
    IngestionRunUnit,
    PropertyValue,
)
from app.schemas.ingestion import RunFilter

log = logging.getLogger(__name__)


# Per-stage assignment. The former classifier/summarizer/property/entity
# agent stages are replaced by the in-process one-shot path (classify +
# metadata + verbatim mentions + resolution — see ingestion_oneshot and
# entity_resolver). Relationship extraction and dossier assignment stay
# agentic: they need graph context.
STAGES: dict[str, dict[str, Any]] = {
    "oneshot": {"label": "Classify + extract + resolve (one-shot)", "inprocess": True},
    "relationship_extractor": {
        "agent": ("grove", "relationship-extractor-agent"),
        "label": "Relationship extraction",
    },
    "dossier_assigner": {
        "agent": ("grove", "dossier-assigner-agent"),
        "label": "Dossier assignment",
    },
}

# Old stage names accepted for API back-compat; all collapse to one-shot.
_STAGE_ALIASES = {
    "classifier": "oneshot",
    "summarizer": "oneshot",
    "property_extractor": "oneshot",
    "entity_extractor": "oneshot",
}


def normalize_stages(stages: list[str]) -> list[str]:
    out: list[str] = []
    for st in stages:
        st = _STAGE_ALIASES.get(st, st)
        if st not in out:
            out.append(st)
    return out


# Stages that depend on the one-shot pass having run (class + entities in
# place). When oneshot is among the requested stages, these wait for it.
_ONESHOT_DEPENDENT_STAGES = {
    "relationship_extractor",
    "dossier_assigner",
}

_TERMINAL_BATCH_STATUSES = {"completed", "partial", "failed", "cancelled"}

# JSONB sentinel inside `sinas_batch_ids` that marks "secondary stages
# already submitted" — used to win the race when two GETs try to fire the
# secondary wave at the same time.
_SECONDARY_CLAIMED_KEY = "_secondary_claimed"


# ─────────────────────────────────────────────────────────────
# Document selection (kept as-is for back-compat with existing API)
# ─────────────────────────────────────────────────────────────


async def _wipe_for_stage(session: AsyncSession, document_id: uuid.UUID, stage: str) -> None:
    """Delete stale auto-extracted artifacts so a rerun doesn't duplicate.
    Manually-authored / locked entries are preserved."""
    if stage == "oneshot":
        await session.execute(
            delete(PropertyValue)
            .where(PropertyValue.document_id == document_id)
            .where(PropertyValue.method == "auto")
            .where(PropertyValue.locked.is_(False))
        )
        await session.execute(
            delete(EntityMention).where(EntityMention.document_id == document_id)
        )


async def _select_documents(session: AsyncSession, f: RunFilter) -> list[uuid.UUID]:
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
    """Insert IngestionRunUnit rows for every (doc, stage) pair. Returns count."""
    f = RunFilter(**(run.filter or {}))
    doc_ids = await _select_documents(session, f)
    now = datetime.now(timezone.utc)
    units = [
        IngestionRunUnit(
            run_id=run.id,
            document_id=doc_id,
            stage=stage,
            status="pending",
            attempts=0,
            created_at=now,
        )
        for doc_id in doc_ids
        for stage in run.stages
    ]
    session.add_all(units)
    return len(units)


# ─────────────────────────────────────────────────────────────
# Batch submission
# ─────────────────────────────────────────────────────────────


# Explicit per-stage instruction. Each agent already carries a detailed system
# prompt; the message just has to tell it to do its job on this document, in
# plain imperative form. The earlier generic "Reprocess <id> for stage X" wording
# read as orchestration jargon — the classifier replied conversationally ("I have
# no reprocess function") instead of acting. {d} is filled with the document id.
_STAGE_MESSAGE: dict[str, str] = {
    "relationship_extractor": (
        "Extract relationships for document {d}. Read its content and recorded "
        "entities, then record the relationships the text supports."
    ),
    "dossier_assigner": (
        "Assign document {d} to dossiers. Inspect its class, properties and "
        "entities, then link it to the dossiers it belongs to."
    ),
}


def _build_stage_inputs(stage: str, doc_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    instruction = _STAGE_MESSAGE.get(stage)
    if instruction is None:
        instruction = f"Process document {{d}} for the '{stage}' stage."
    return [
        {
            "input_variables": {"document_id": str(d), "stage": stage},
            "message": (
                instruction.format(d=d)
                + " Work only on this document; do not invoke other agents."
            ),
        }
        for d in doc_ids
    ]


async def _submit_stage(
    session: AsyncSession,
    run: IngestionRun,
    stage: str,
    units: list[IngestionRunUnit],
    client: SinasClient,
) -> None:
    """Wipe + submit one stage's batch. Mutates the units (status, batch_id,
    execution_id) and `run.sinas_batch_ids`."""
    if not units:
        return
    stage_cfg = STAGES.get(stage)
    if stage_cfg is None:
        log.error("unknown stage '%s' on run %s", stage, run.id)
        return
    namespace, agent_name = stage_cfg["agent"]

    # Pre-wipe stale artifacts.
    for u in units:
        try:
            await _wipe_for_stage(session, u.document_id, stage)
        except Exception as exc:  # noqa: BLE001
            log.warning("wipe failed for doc %s stage %s: %s", u.document_id, stage, exc)
    await session.flush()

    doc_ids = [u.document_id for u in units]
    try:
        result = await asyncio.to_thread(
            client.agents.submit_batch,
            namespace=namespace,
            name=agent_name,
            inputs=_build_stage_inputs(stage, doc_ids),
            trigger_id_prefix=f"grove:ingest:{run.id}:{stage}",
        )
    except SinasAPIError as exc:
        now = datetime.now(timezone.utc)
        for u in units:
            u.status = "failed"
            u.error = f"batch submit: {exc}"
            u.completed_at = now
            run.done_units += 1
            run.failed_units += 1
        return

    batch_id = result["batch_id"]
    execution_ids = result.get("execution_ids") or []
    chat_ids = result.get("chat_ids") or []
    now = datetime.now(timezone.utc)
    for idx, u in enumerate(units):
        u.status = "running"
        u.started_at = now
        u.attempts += 1
        if idx < len(execution_ids):
            u.sinas_execution_id = execution_ids[idx]
        if idx < len(chat_ids):
            u.chat_id = chat_ids[idx]
    run.sinas_batch_ids = {**(run.sinas_batch_ids or {}), stage: batch_id}



async def _run_oneshot_inprocess(run_id: uuid.UUID, doc_ids: list[uuid.UUID]) -> None:
    """Execute the one-shot stage in-process: per document, wipe stale
    artifacts, run classify+extract, then resolve mentions. Unit rows and
    run counters update per document, so progress is live."""
    from app.services.entity_resolver import resolve_unlinked
    from app.services.ingestion_oneshot import oneshot_ingest

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
                log.info("oneshot run %s stopped (status=%s)", run_id,
                         run.status if run else "gone")
                return
        error: str | None = None
        try:
            async with AsyncSessionLocal() as session:
                await _wipe_for_stage(session, did, "oneshot")
                await session.commit()
            reports = await oneshot_ingest([did], write=True, concurrency=1)
            rep = reports[0] if reports else {}
            if rep.get("error"):
                error = str(rep["error"])[:500]
            else:
                await resolve_unlinked([did], write=True)
        except Exception as exc:  # noqa: BLE001 — per-doc isolation
            error = str(exc)[:500]
        async with AsyncSessionLocal() as session:
            unit = (
                await session.execute(
                    select(IngestionRunUnit)
                    .where(IngestionRunUnit.run_id == run_id)
                    .where(IngestionRunUnit.document_id == did)
                    .where(IngestionRunUnit.stage == "oneshot")
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


async def submit_run(
    session: AsyncSession, run: IngestionRun, client: SinasClient
) -> None:
    """Submit batches for the run. If classifier is in stages, submit ONLY
    classifier (secondary stages wait for classifier-terminal in `progress`).
    Otherwise submit all stage batches.

    Called from the request handler holding the user's bearer; caller commits.
    """
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

    by_stage: dict[str, list[IngestionRunUnit]] = {}
    for u in units:
        by_stage.setdefault(u.stage, []).append(u)

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)

    if "oneshot" in by_stage:
        # Phase 1: the in-process one-shot pass. Agentic stages
        # (relationships, dossiers) stay pending; the progress endpoint
        # fires them when every oneshot unit is terminal.
        now = datetime.now(timezone.utc)
        for u in by_stage["oneshot"]:
            u.status = "running"
            u.started_at = now
            u.attempts += 1
        run.sinas_batch_ids = {**(run.sinas_batch_ids or {}), "oneshot": "inprocess"}
        doc_ids = [u.document_id for u in by_stage["oneshot"]]
        asyncio.get_event_loop().create_task(_run_oneshot_inprocess(run.id, doc_ids))
    else:
        # No one-shot stage — submit the agentic batches directly.
        for stage, stage_units in by_stage.items():
            if stage in STAGES:
                await _submit_stage(session, run, stage, stage_units, client)


# ─────────────────────────────────────────────────────────────
# Progress (called from GET /ingestion/runs/{id})
# ─────────────────────────────────────────────────────────────


# A unit can come back COMPLETED while the agent's final reply is actually an
# LLM provider error returned as plain text. The agent catches the error (a 429
# rate limit, or a 400 spend/usage cap) and returns it as its message, so the
# Sinas execution still looks successful. Detect that signature in the chat
# transcript and treat the unit as failed, so it isn't silently marked succeeded
# (and can be re-run). No retry here, just correct status. Costs one chat fetch
# per completed unit during reconciliation. Markers match case-insensitively.
_RATE_LIMIT_MARKERS = (
    "rate_limit_error",
    "error code: 429",
    "429 -",
    # Account spend / usage cap. Anthropic returns this as a 400, which the
    # 429 markers above miss. This is what caused silent empty-success
    # ingestion: the unit reported COMPLETED but wrote no data.
    "usage limits",
    "api usage limits",
    "you have reached your specified api usage limits",
    # Context-length overflow. A doc larger than the model context window
    # returns a 400 ("prompt is too long: N tokens > 200000 maximum"). The
    # message differs from the usage-cap one above, so without these markers
    # an oversized doc would also be marked succeeded with no data written.
    "prompt is too long",
    "too many tokens",
)


async def _agent_reply_is_rate_limited(client: SinasClient, chat_id: str | None) -> bool:
    if not chat_id:
        return False
    try:
        chat = await asyncio.to_thread(client.chats.get, chat_id)
    except SinasAPIError:
        return False  # can't read the chat — don't override the reported status
    for msg in reversed(chat.get("messages") or []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            text = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = content or ""
        low = text.lower()
        return any(marker in low for marker in _RATE_LIMIT_MARKERS)
    return False


async def _reconcile_stage_units(
    client: SinasClient,
    run_id: uuid.UUID,
    stage: str,
    batch_id: str,
) -> None:
    """Drill into per-execution status and mark unit rows + run counters."""
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
                        select(IngestionRunUnit)
                        .where(IngestionRunUnit.sinas_execution_id == exec_id)
                        .where(IngestionRunUnit.run_id == run_id)
                        .where(IngestionRunUnit.stage == stage)
                    )
                ).scalar_one_or_none()
                if unit is None or unit.status in ("succeeded", "failed"):
                    continue  # already reconciled
                exec_status = (execution.get("status") or "").upper()
                ok = exec_status == "COMPLETED"
                chat_id = execution.get("chat_id") or unit.chat_id
                rate_limited = ok and await _agent_reply_is_rate_limited(client, chat_id)
                if rate_limited:
                    ok = False
                unit.status = "succeeded" if ok else "failed"
                if not ok:
                    unit.error = (
                        "agent hit an LLM rate limit (429) — marked failed for re-run"
                        if rate_limited
                        else execution.get("error") or f"sinas: {exec_status}"
                    )
                unit.chat_id = chat_id
                unit.completed_at = datetime.now(timezone.utc)
                run = await session.get(IngestionRun, run_id)
                if run is not None:
                    run.done_units += 1
                    if not ok:
                        run.failed_units += 1
            await session.commit()
        if len(executions) < 500:
            break
        offset += 500


async def _claim_secondary_submission(run_id: uuid.UUID) -> bool:
    """Atomic CAS on sinas_batch_ids: add `_secondary_claimed: true` only if
    not already present. Returns True if THIS call won the race."""
    # `jsonb ? key` returns true if the JSON object has the key. We update
    # only when the key is absent.
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "UPDATE ingestion_run "
                "SET sinas_batch_ids = sinas_batch_ids || jsonb_build_object(:k, true) "
                "WHERE id = :id AND NOT (sinas_batch_ids ? :k)"
            ),
            {"k": _SECONDARY_CLAIMED_KEY, "id": str(run_id)},
        )
        await session.commit()
        return result.rowcount > 0


async def _submit_secondary_stages(run_id: uuid.UUID, client: SinasClient) -> None:
    """Submit batches for every non-classifier stage that has pending units."""
    async with AsyncSessionLocal() as session:
        run = await session.get(IngestionRun, run_id)
        if run is None:
            return
        pending_units = list(
            (
                await session.execute(
                    select(IngestionRunUnit)
                    .where(IngestionRunUnit.run_id == run_id)
                    .where(IngestionRunUnit.status == "pending")
                    .where(IngestionRunUnit.stage != "oneshot")
                    .order_by(IngestionRunUnit.created_at)
                )
            ).scalars().all()
        )
        if not pending_units:
            return
        by_stage: dict[str, list[IngestionRunUnit]] = {}
        for u in pending_units:
            by_stage.setdefault(u.stage, []).append(u)
        for stage, units in by_stage.items():
            if stage in STAGES:
                await _submit_stage(session, run, stage, units, client)
        await session.commit()


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


async def progress(run: IngestionRun, client: SinasClient) -> dict[str, Any]:
    """Drive the run forward and return a per-stage status snapshot.

    Called from `GET /ingestion/runs/{id}`. The caller's bearer is forwarded
    to Sinas via the provided client.
    """
    batch_ids = dict(run.sinas_batch_ids or {})
    # Strip out internal sentinels for display.
    visible_batches = {k: v for k, v in batch_ids.items() if not k.startswith("_")}

    snapshot: dict[str, Any] = {
        "status": run.status,
        "stages": {},  # stage_name -> Sinas batch status dict
    }

    # Pull live status for every submitted stage. The in-process oneshot
    # stage has no Sinas batch — its status comes from the unit rows.
    oneshot_terminal = False
    for stage, batch_id in visible_batches.items():
        if stage not in STAGES:
            continue
        if stage == "oneshot":
            async with AsyncSessionLocal() as s_:
                counts = dict(
                    (
                        await s_.execute(
                            select(IngestionRunUnit.status, func.count())
                            .where(IngestionRunUnit.run_id == run.id)
                            .where(IngestionRunUnit.stage == "oneshot")
                            .group_by(IngestionRunUnit.status)
                        )
                    ).all()
                )
            total = sum(counts.values())
            done = counts.get("succeeded", 0) + counts.get("failed", 0)
            oneshot_terminal = total > 0 and done == total
            snapshot["stages"]["oneshot"] = {
                "status": "completed" if oneshot_terminal else "running",
                "total": total,
                "succeeded": counts.get("succeeded", 0),
                "failed": counts.get("failed", 0),
            }
            continue
        try:
            status = await asyncio.to_thread(client.batches.get, batch_id)
        except SinasAPIError as exc:
            snapshot["stages"][stage] = {"error": str(exc)}
            continue
        snapshot["stages"][stage] = status
        if status.get("status") in _TERMINAL_BATCH_STATUSES:
            await _reconcile_stage_units(client, run.id, stage, batch_id)

    # If oneshot just turned terminal, fire the agentic secondary stages.
    if oneshot_terminal:
        if True:
            secondary_pending = any(
                stage not in visible_batches and stage in run.stages
                for stage in _ONESHOT_DEPENDENT_STAGES
            )
            if secondary_pending:
                won = await _claim_secondary_submission(run.id)
                if won:
                    await _submit_secondary_stages(run.id, client)
                # Refresh and re-snapshot the new stages.
                async with AsyncSessionLocal() as s:
                    refreshed = await s.get(IngestionRun, run.id)
                    if refreshed is not None:
                        batch_ids = dict(refreshed.sinas_batch_ids or {})
                        visible_batches = {
                            k: v for k, v in batch_ids.items() if not k.startswith("_")
                        }
                for stage, batch_id in visible_batches.items():
                    if stage in STAGES and stage not in snapshot["stages"]:
                        try:
                            snapshot["stages"][stage] = await asyncio.to_thread(
                                client.batches.get, batch_id
                            )
                        except SinasAPIError as exc:
                            snapshot["stages"][stage] = {"error": str(exc)}

    # Mark run terminal if every unit has reached a terminal status.
    await _mark_run_terminal_if_done(run.id)
    async with AsyncSessionLocal() as s:
        refreshed = await s.get(IngestionRun, run.id)
        if refreshed is not None:
            snapshot["status"] = refreshed.status

    return snapshot
