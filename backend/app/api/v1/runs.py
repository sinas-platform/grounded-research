"""Ingestion runs — bulk reprocessing endpoints + per-doc reprocess sugar."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models import IngestionRun, IngestionRunUnit
from app.schemas.ingestion import (
    RunCreateIn,
    RunCreateOut,
    RunFilter,
    RunOut,
    RunUnitOut,
    Stage,
)
from app.services.ingestion_runner import (
    STAGES,
    expand_filter,
    materialize_run,
    normalize_stages,
    progress as ingestion_progress,
    submit_run,
)
from sinas import SinasClient
from app.config import get_settings

router = APIRouter(prefix="/ingestion", tags=["ingestion-runs"])


class StageDescOut(BaseModel):
    key: str
    label: str


@router.get("/stages", response_model=list[StageDescOut])
async def list_stages() -> list[StageDescOut]:
    return [StageDescOut(key=k, label=v["label"]) for k, v in STAGES.items()]


@router.post(
    "/runs",
    response_model=RunCreateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.admin:all"))],
)
async def create_run(
    payload: RunCreateIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    doc_ids = await expand_filter(session, payload.filter)
    stages = normalize_stages(list(payload.stages))
    unit_count = len(doc_ids) * len(stages)

    if payload.dry_run:
        return RunCreateOut(
            run_id=None,
            document_count=len(doc_ids),
            unit_count=unit_count,
            status="would_start",
        )

    if not doc_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "filter selected zero documents — refusing to create an empty run",
        )

    run = IngestionRun(
        status="pending",
        stages=stages,
        filter=payload.filter.model_dump(mode="json"),
        total_units=unit_count,
        done_units=0,
        failed_units=0,
        started_by=caller.user_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(run)
    await session.flush()
    await materialize_run(session, run)
    # Submit batches inline using the caller's bearer. No worker; subsequent
    # transitions (e.g. secondary stages after classifier) are driven by
    # GET /ingestion/runs/{id}.
    client = SinasClient(base_url=get_settings().sinas_url, token=caller.sinas_token)
    await submit_run(session, run, client)
    await session.commit()

    return RunCreateOut(
        run_id=run.id,
        document_count=len(doc_ids),
        unit_count=unit_count,
        status="started",
    )


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(
            select(IngestionRun)
            .order_by(IngestionRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return rows


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Returns the run row, **after** advancing its state from Sinas.

    Each GET fetches live batch status for every submitted stage; if
    classifier is in stages and has just turned terminal, secondary stages
    are submitted here. This is the only place run state advances — there
    is no background worker.
    """
    row = await session.get(IngestionRun, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if row.status in ("completed", "failed", "cancelled"):
        return row
    client = SinasClient(base_url=get_settings().sinas_url, token=caller.sinas_token)
    try:
        await ingestion_progress(row, client)
    except Exception as exc:  # noqa: BLE001
        import logging as _log
        _log.getLogger(__name__).warning(
            "ingestion progress fetch failed for run %s: %s", run_id, exc
        )
    await session.refresh(row)
    return row


@router.get("/runs/{run_id}/units", response_model=list[RunUnitOut])
async def list_run_units(
    run_id: uuid.UUID,
    status_filter: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(IngestionRunUnit)
        .where(IngestionRunUnit.run_id == run_id)
        .order_by(IngestionRunUnit.created_at)
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(IngestionRunUnit.status == status_filter)
    rows = (await session.execute(stmt)).scalars().all()
    return rows


# ────────────────────── per-document syntactic sugar ──────────────────────
class ReprocessOneIn(BaseModel):
    stages: list[Stage]


@router.post(
    "/documents/{doc_id}/reprocess",
    response_model=RunCreateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.admin:all"))],
)
async def reprocess_document(
    doc_id: uuid.UUID,
    payload: ReprocessOneIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Reprocess a single document — creates a 1-doc ingestion run."""
    run = IngestionRun(
        status="pending",
        stages=stages,
        filter=RunFilter(document_ids=[doc_id]).model_dump(mode="json"),
        total_units=len(payload.stages),
        done_units=0,
        failed_units=0,
        started_by=caller.user_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(run)
    await session.flush()
    await materialize_run(session, run)
    client = SinasClient(base_url=get_settings().sinas_url, token=caller.sinas_token)
    await submit_run(session, run, client)
    await session.commit()
    return RunCreateOut(
        run_id=run.id,
        document_count=1,
        unit_count=len(payload.stages),
        status="started",
    )
