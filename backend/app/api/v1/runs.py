"""Ingestion runs — bulk reprocessing endpoints + per-doc reprocess sugar.

A run = documents (RunFilter) × parts (default: the full pipeline). One
in-process worker drives everything; GET is a plain read — nothing is
advanced by polling.
"""

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
    ALL_PARTS,
    Part,
    RunCreateIn,
    RunCreateOut,
    RunFilter,
    RunOut,
    RunUnitOut,
)
from app.services.ingestion_runner import (
    PARTS,
    expand_filter,
    materialize_run,
    submit_run,
)

router = APIRouter(prefix="/ingestion", tags=["ingestion-runs"])


class PartDescOut(BaseModel):
    key: str
    label: str


@router.get("/parts", response_model=list[PartDescOut])
async def list_parts() -> list[PartDescOut]:
    """The pipeline's parts, in execution order — for run-trigger UIs."""
    return [PartDescOut(key=k, label=v) for k, v in PARTS.items()]


def _resolve_parts(parts: list[str] | None) -> list[str]:
    """Dedupe, keep canonical execution order, default to all."""
    if not parts:
        return list(ALL_PARTS)
    return [p for p in ALL_PARTS if p in set(parts)]


async def _create_and_submit(
    session: AsyncSession,
    caller: CallerIdentity,
    *,
    parts: list[str],
    run_filter: RunFilter,
    batch: bool = False,
) -> tuple[IngestionRun, int]:
    doc_ids = await expand_filter(session, run_filter)
    if not doc_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "filter selected zero documents — refusing to create an empty run",
        )
    run = IngestionRun(
        status="pending",
        stages=parts,  # legacy column name; holds the parts list
        filter=run_filter.model_dump(mode="json"),
        total_units=len(doc_ids),
        done_units=0,
        failed_units=0,
        started_by=caller.user_id,
        created_at=datetime.now(timezone.utc),
        sinas_batch_ids={"mode": "provider"} if batch else None,
    )
    session.add(run)
    await session.flush()
    await materialize_run(session, run)
    await submit_run(session, run)
    await session.commit()
    return run, len(doc_ids)


@router.post(
    "/runs",
    response_model=RunCreateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_run(
    payload: RunCreateIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    parts = _resolve_parts(payload.parts)
    doc_ids = await expand_filter(session, payload.filter)

    if payload.dry_run:
        return RunCreateOut(
            run_id=None,
            document_count=len(doc_ids),
            unit_count=len(doc_ids),
            status="would_start",
        )

    if not doc_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "filter selected zero documents — refusing to create an empty run",
        )

    run, count = await _create_and_submit(
        session, caller, parts=parts, run_filter=payload.filter,
        batch=payload.batch,
    )
    return RunCreateOut(
        run_id=run.id,
        document_count=count,
        unit_count=count,
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
):
    """Plain read — the worker advances all state."""
    row = await session.get(IngestionRun, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
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
    parts: list[Part] | None = None


@router.post(
    "/documents/{doc_id}/reprocess",
    response_model=RunCreateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def reprocess_document(
    doc_id: uuid.UUID,
    payload: ReprocessOneIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Reprocess a single document — creates a 1-doc ingestion run."""
    run, count = await _create_and_submit(
        session,
        caller,
        parts=_resolve_parts(payload.parts),
        run_filter=RunFilter(document_ids=[doc_id]),
    )
    return RunCreateOut(
        run_id=run.id,
        document_count=count,
        unit_count=count,
        status="started",
    )
