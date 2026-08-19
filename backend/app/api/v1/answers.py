"""Answer reads."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.annotations import _load_definitions
from app.auth import CallerIdentity, get_caller
from app.db import get_session
from app.models import Answer, AnswerClaim, ClaimEvidence
from app.services.annotations import annotations_for_documents
from app.schemas.runtime import (
    AnswerOut,
    ClaimEvidenceOut,
    ClaimOut,
    ClaimWithEvidenceOut,
)
from app.services.visibility import visible_clause

router = APIRouter(prefix="/answers", tags=["answers"])


async def _visible_answer_or_404(
    answer_id: uuid.UUID, session: AsyncSession, caller: CallerIdentity
) -> Answer:
    read_all = await caller.has_permission("sgr.answers.read:all")
    stmt = (
        select(Answer)
        .where(Answer.id == answer_id)
        .where(visible_clause(Answer, caller, read_all=read_all))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "answer not found")
    return row


@router.get("", response_model=list[AnswerOut])
async def list_answers(
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
    limit: int = 50,
    offset: int = 0,
):
    read_all = await caller.has_permission("sgr.answers.read:all")
    stmt = (
        select(Answer)
        .where(visible_clause(Answer, caller, read_all=read_all))
        .order_by(Answer.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return (await session.execute(stmt)).scalars().all()


@router.get("/{answer_id}", response_model=AnswerOut)
async def get_answer(
    answer_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await _visible_answer_or_404(answer_id, session, caller)


@router.get("/{answer_id}/claims", response_model=list[ClaimOut])
async def get_answer_claims(
    answer_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    await _visible_answer_or_404(answer_id, session, caller)
    rows = (
        await session.execute(
            select(AnswerClaim)
            .where(AnswerClaim.answer_id == answer_id)
            .order_by(AnswerClaim.sequence)
        )
    ).scalars().all()
    for i, row in enumerate(rows, start=1):
        row.display_number = i
    return rows


@router.get("/{answer_id}/evidence", response_model=list[ClaimWithEvidenceOut])
async def get_answer_evidence(
    answer_id: uuid.UUID,
    pending_only: bool = False,
    annotate: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated annotation names to attach per evidence row, "
                "computed for the case entity the evidence document is the "
                "full text of (e.g. annotate=issuing_body,authority_tier)."
            )
        ),
    ] = None,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Every claim of the answer with its evidence rows nested, in claim
    order — the whole evidence set in one call, instead of get_answer_claims
    plus one get_claim_evidence call per claim.

    `pending_only=true` returns only unvalidated evidence rows (claims whose
    rows are all validated are omitted entirely). A validator should
    enumerate fully once, then use pending_only for progress re-checks — the
    response shrinks toward empty instead of re-paying the full payload."""
    await _visible_answer_or_404(answer_id, session, caller)
    claims = (
        await session.execute(
            select(AnswerClaim)
            .where(AnswerClaim.answer_id == answer_id)
            .order_by(AnswerClaim.sequence)
        )
    ).scalars().all()
    for i, c in enumerate(claims, start=1):
        c.display_number = i
    ev_stmt = (
        select(ClaimEvidence)
        .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
        .where(AnswerClaim.answer_id == answer_id)
    )
    if pending_only:
        ev_stmt = ev_stmt.where(ClaimEvidence.validated.is_(False))
    evidence = (await session.execute(ev_stmt)).scalars().all()
    by_claim: dict[uuid.UUID, list[ClaimEvidence]] = {}
    for row in evidence:
        by_claim.setdefault(row.claim_id, []).append(row)

    annotations_by_doc: dict[uuid.UUID, dict] = {}
    if annotate is not None and evidence:
        names = [n.strip() for n in annotate.split(",") if n.strip()]
        definitions = await _load_definitions(session, names or None)
        annotations_by_doc = await annotations_for_documents(
            session, list({e.document_id for e in evidence}), definitions
        )

    def _ev_out(e: ClaimEvidence) -> ClaimEvidenceOut:
        out = ClaimEvidenceOut.model_validate(e)
        out.annotations = annotations_by_doc.get(e.document_id)
        return out

    return [
        ClaimWithEvidenceOut(
            **ClaimOut.model_validate(c).model_dump(),
            evidence=[_ev_out(e) for e in by_claim.get(c.id, [])],
        )
        for c in claims
        if not pending_only or c.id in by_claim
    ]


@router.get("/claims/{claim_id}/evidence", response_model=list[ClaimEvidenceOut])
async def get_claim_evidence(
    claim_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    read_all = await caller.has_permission("sgr.answers.read:all")
    claim = (
        await session.execute(
            select(AnswerClaim)
            .join(Answer, Answer.id == AnswerClaim.answer_id)
            .where(AnswerClaim.id == claim_id)
            .where(visible_clause(Answer, caller, read_all=read_all))
        )
    ).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "claim not found")
    rows = (
        await session.execute(
            select(ClaimEvidence).where(ClaimEvidence.claim_id == claim_id)
        )
    ).scalars().all()
    return rows
