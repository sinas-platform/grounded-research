"""Annotations API: list definitions, compute values, recompute materialized.

Response surfacing on result/answer endpoints (?annotate=) builds on the
compute service; this router is the definition-level surface.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.identity import CallerIdentity, get_caller
from app.db import get_session
from app.models import AnnotationDefinition
from app.services.annotations import (
    AnnotationConfigError,
    OrderKey,
    compute_annotations,
    materialize,
    order_subjects,
)

router = APIRouter(prefix="/annotations", tags=["annotations"])


class AnnotationDefinitionOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    path: str
    reduce: str | dict
    materialize: bool
    subject_ref_type: str

    model_config = {"from_attributes": True}


class AnnotationComputeOut(BaseModel):
    subject_id: uuid.UUID
    annotations: dict[str, dict | None]


class RecomputeIn(BaseModel):
    subject_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    names: list[str] | None = None


class RecomputeOut(BaseModel):
    written: int
    subjects: int


async def _load_definitions(
    session: AsyncSession, names: list[str] | None
) -> list[AnnotationDefinition]:
    stmt = select(AnnotationDefinition).order_by(AnnotationDefinition.name)
    if names:
        stmt = stmt.where(AnnotationDefinition.name.in_(names))
    rows = (await session.execute(stmt)).scalars().all()
    if names:
        missing = sorted(set(names) - {r.name for r in rows})
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"unknown annotation(s): {', '.join(missing)}",
            )
    return list(rows)


@router.get("", response_model=list[AnnotationDefinitionOut])
async def list_annotations(
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return (
        await session.execute(
            select(AnnotationDefinition).order_by(AnnotationDefinition.name)
        )
    ).scalars().all()


@router.get("/compute", response_model=AnnotationComputeOut)
async def compute_for_subject(
    subject_id: uuid.UUID,
    names: str | None = Query(
        default=None, description="Comma-separated annotation names; all when omitted."
    ),
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Compute annotations for one subject on demand (nothing is stored)."""
    name_list = [n.strip() for n in names.split(",") if n.strip()] if names else None
    definitions = await _load_definitions(session, name_list)
    if not definitions:
        return AnnotationComputeOut(subject_id=subject_id, annotations={})
    try:
        computed = await compute_annotations(session, [subject_id], definitions)
    except AnnotationConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return AnnotationComputeOut(subject_id=subject_id, annotations=computed[subject_id])


@router.post("/recompute", response_model=RecomputeOut)
async def recompute(
    body: RecomputeIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Recompute and store materialized annotations for the given subjects
    (the backfill entry point). Non-materialized definitions are skipped."""
    definitions = await _load_definitions(session, body.names)
    try:
        written = await materialize(session, body.subject_ids, definitions)
    except AnnotationConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await session.commit()
    return RecomputeOut(written=written, subjects=len(body.subject_ids))


class OrderKeyIn(BaseModel):
    by: str = Field(description="Dotted path into annotation values, e.g. 'authority_tier.depth'.")
    direction: str = Field(default="asc", pattern="^(asc|desc)$")


class OrderIn(BaseModel):
    """Generic, deterministic ordering over annotation values. Any domain
    rule (e.g. which jurisdiction ranks first for this question) lives in
    the caller's `precedence` list — Grove only sorts."""

    subject_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    group_by: str | None = Field(
        default=None,
        description="Dotted path whose value buckets subjects, e.g. 'jurisdiction.value.name'.",
    )
    precedence: list[str] | None = Field(
        default=None,
        description="Bucket values that come first, in this order; the rest follow alphabetically.",
    )
    then_by: list[OrderKeyIn] = Field(default_factory=list)
    names: list[str] | None = Field(
        default=None, description="Annotations to compute; all when omitted."
    )


class OrderOut(BaseModel):
    ordered: list[uuid.UUID]
    annotations: dict[uuid.UUID, dict[str, dict | None]]


@router.post("/order", response_model=OrderOut)
async def order(
    body: OrderIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Compute annotations for the subjects and return them in sorted order.
    Deterministic: equal inputs always produce equal output (final tie-break
    is the subject id). Used as the agent-facing ordering tool and by result
    readers."""
    definitions = await _load_definitions(session, body.names)
    try:
        computed = await compute_annotations(session, body.subject_ids, definitions)
    except AnnotationConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    ordered = order_subjects(
        body.subject_ids,
        computed,
        group_by=body.group_by,
        precedence=body.precedence,
        then_by=[OrderKey(by=k.by, direction=k.direction) for k in body.then_by],
    )
    return OrderOut(ordered=ordered, annotations=computed)
