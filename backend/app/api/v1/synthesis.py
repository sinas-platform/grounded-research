"""Synthesis — claim drafting, evidence binding, faithfulness validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models import Answer, AnswerClaim, ClaimEvidence
from app.schemas.runtime import (
    ClaimEvidenceIn,
    ClaimEvidenceOut,
    ClaimOut,
    ClaimWithEvidenceOut,
)
from app.services.result_filter import load_visible_result
from app.services.visibility import visible_clause

router = APIRouter(prefix="/synthesis", tags=["synthesis"])


async def _writable_answer_or_404(
    answer_id: uuid.UUID, session: AsyncSession, caller: CallerIdentity
) -> Answer:
    # write:own means the answer must be visible to the caller; write:all lifts that.
    write_all = await caller.has_permission("sgr.answers.write:all")
    stmt = (
        select(Answer)
        .where(Answer.id == answer_id)
        .where(visible_clause(Answer, caller, read_all=write_all))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "answer not found")
    return row


async def _writable_claim_or_404(
    claim_id: uuid.UUID, session: AsyncSession, caller: CallerIdentity
) -> AnswerClaim:
    write_all = await caller.has_permission("sgr.answers.write:all")
    stmt = (
        select(AnswerClaim)
        .join(Answer, Answer.id == AnswerClaim.answer_id)
        .where(AnswerClaim.id == claim_id)
        .where(visible_clause(Answer, caller, read_all=write_all))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "claim not found")
    return row


class StartAnswerIn(BaseModel):
    question: str
    source_result_id: uuid.UUID | None = None
    source_dossier_id: uuid.UUID | None = None


@router.post(
    "/answers",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def start_answer(
    payload: StartAnswerIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    if payload.source_result_id is None and payload.source_dossier_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "must specify source_result_id or source_dossier_id"
        )

    # If a source result is specified, inherit its owner+roles for the answer.
    # The result must be visible to the caller — otherwise an arbitrary id
    # would leak its existence and let the caller mint answers owned by
    # someone else.
    owner_id, roles = caller.user_id, list(caller.roles or [])
    if payload.source_result_id:
        result = await load_visible_result(
            session, caller, payload.source_result_id, for_write=False
        )
        owner_id = result.owner_id
        roles = list(result.roles or [])

    row = Answer(
        source_result_id=payload.source_result_id,
        source_dossier_id=payload.source_dossier_id,
        question=payload.question,
        owner_id=owner_id,
        roles=roles,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id, "status": row.status}


class DraftClaimIn(BaseModel):
    sequence: int
    claim_text: str
    claim_type: str | None = None
    # Evidence bound in the same call — one round-trip per claim instead of
    # draft_claim + one bind_evidence call per span. bind_evidence stays for
    # adding evidence to an already-drafted claim.
    evidence: list[ClaimEvidenceIn] = []


@router.post(
    "/answers/{answer_id}/claims",
    response_model=ClaimWithEvidenceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def draft_claim(
    answer_id: uuid.UUID,
    payload: DraftClaimIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    await _writable_answer_or_404(answer_id, session, caller)
    # Idempotent on (answer_id, sequence): a re-posted claim (agent retry or
    # remediation restatement) REPLACES the earlier row and its evidence
    # instead of duplicating it.
    existing = (
        await session.execute(
            select(AnswerClaim).where(
                AnswerClaim.answer_id == answer_id,
                AnswerClaim.sequence == payload.sequence,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await session.execute(
            ClaimEvidence.__table__.delete().where(
                ClaimEvidence.claim_id == existing.id
            )
        )
        existing.claim_text = payload.claim_text
        existing.claim_type = payload.claim_type
        row = existing
    else:
        row = AnswerClaim(
            answer_id=answer_id,
            **payload.model_dump(exclude={"evidence"}),
        )
        session.add(row)
    await session.flush()  # claim id for the evidence FKs
    ev_rows = [
        ClaimEvidence(
            claim_id=row.id,
            document_id=ev.document_id,
            document_version_id=ev.document_version_id,
            span=ev.span.model_dump(),
            stance=ev.stance,
            relevance=ev.relevance,
        )
        for ev in payload.evidence
    ]
    session.add_all(ev_rows)
    await session.commit()
    await session.refresh(row)
    for ev_row in ev_rows:
        await session.refresh(ev_row)
    return ClaimWithEvidenceOut(
        **ClaimOut.model_validate(row).model_dump(),
        evidence=[ClaimEvidenceOut.model_validate(e) for e in ev_rows],
    )


@router.post(
    "/claims/{claim_id}/evidence",
    response_model=ClaimEvidenceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def bind_evidence(
    claim_id: uuid.UUID,
    payload: ClaimEvidenceIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    await _writable_claim_or_404(claim_id, session, caller)
    row = ClaimEvidence(
        claim_id=claim_id,
        document_id=payload.document_id,
        document_version_id=payload.document_version_id,
        span=payload.span.model_dump(),
        stance=payload.stance,
        relevance=payload.relevance,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


class UpdateClaimIn(BaseModel):
    claim_text: str
    claim_type: str | None = None


@router.patch(
    "/claims/{claim_id}",
    response_model=ClaimOut,
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def update_claim(
    claim_id: uuid.UUID,
    payload: UpdateClaimIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Rewrite a claim's text — the remedy when validation shows the claim
    overreaches what its spans support. Existing evidence stays bound but is
    reset to unvalidated, since verdicts were judged against the old text."""
    row = await _writable_claim_or_404(claim_id, session, caller)
    row.claim_text = payload.claim_text
    if payload.claim_type is not None:
        row.claim_type = payload.claim_type
    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(ClaimEvidence)
        .where(ClaimEvidence.claim_id == claim_id)
        .values(validated=False, validation_reasoning=None)
    )
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/claims/{claim_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def delete_claim(
    claim_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Drop a claim (and its evidence bindings) — the remedy when no span in
    the corpus can support it, e.g. figures redacted in the source. Removing
    the claim also unblocks publish_answer, which gates on every remaining
    row being validated."""
    row = await _writable_claim_or_404(claim_id, session, caller)
    from sqlalchemy import delete as sa_delete

    await session.execute(
        sa_delete(ClaimEvidence).where(ClaimEvidence.claim_id == claim_id)
    )
    await session.delete(row)
    await session.commit()


class ValidateAnswerIn(BaseModel):
    pending_only: bool = True


@router.post(
    "/answers/{answer_id}/validate",
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def validate_answer_evidence(
    answer_id: uuid.UUID,
    payload: ValidateAnswerIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Judge the answer's (pending) evidence rows as parallel stateless
    LLM calls and record verdicts — see services.faithfulness."""
    from app.services.faithfulness import validate_answer_evidence as svc

    await _writable_answer_or_404(answer_id, session, caller)
    return await svc(session, caller, answer_id, pending_only=payload.pending_only)


class ValidationVerdict(BaseModel):
    validated: bool
    reasoning: str


@router.post(
    "/evidence/{evidence_id}/verdict",
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def record_validation_verdict(
    evidence_id: uuid.UUID,
    payload: ValidationVerdict,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    write_all = await caller.has_permission("sgr.answers.write:all")
    row = (
        await session.execute(
            select(ClaimEvidence)
            .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
            .join(Answer, Answer.id == AnswerClaim.answer_id)
            .where(ClaimEvidence.id == evidence_id)
            .where(visible_clause(Answer, caller, read_all=write_all))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence not found")
    row.validated = payload.validated
    row.validation_reasoning = payload.reasoning
    await session.commit()
    return {"ok": True}


class VerdictEntry(ValidationVerdict):
    evidence_id: uuid.UUID


class BulkVerdictsIn(BaseModel):
    verdicts: list[VerdictEntry]


@router.post(
    "/answers/{answer_id}/verdicts",
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def record_validation_verdicts(
    answer_id: uuid.UUID,
    payload: BulkVerdictsIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Record verdicts for many evidence rows of one answer in a single call.

    All-or-nothing: every evidence_id must belong to the answer, or the whole
    batch is rejected — a partial write would let a caller believe rows were
    validated that weren't.
    """
    await _writable_answer_or_404(answer_id, session, caller)
    if not payload.verdicts:
        return {"ok": True, "updated": 0}
    rows = (
        await session.execute(
            select(ClaimEvidence)
            .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
            .where(AnswerClaim.answer_id == answer_id)
            .where(ClaimEvidence.id.in_([v.evidence_id for v in payload.verdicts]))
        )
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    missing = [str(v.evidence_id) for v in payload.verdicts if v.evidence_id not in by_id]
    if missing:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"evidence not found on this answer: {', '.join(missing)}",
        )
    for v in payload.verdicts:
        row = by_id[v.evidence_id]
        row.validated = v.validated
        row.validation_reasoning = v.reasoning
    await session.commit()
    return {"ok": True, "updated": len(payload.verdicts)}


@router.post(
    "/answers/{answer_id}/publish",
    dependencies=[Depends(require_permission("sgr.answers.write:own"))],
)
async def publish_answer(
    answer_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = await _writable_answer_or_404(answer_id, session, caller)

    # Refuse to publish if any claim has unvalidated evidence
    bad = (
        await session.execute(
            select(ClaimEvidence)
            .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
            .where(AnswerClaim.answer_id == answer_id)
            .where(ClaimEvidence.validated.is_(False))
            .limit(1)
        )
    ).scalar_one_or_none()
    if bad is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "answer has unvalidated evidence — run faithfulness validation first",
        )

    row.status = "published"
    row.published_at = datetime.now(timezone.utc)
    await session.commit()
    return {"id": row.id, "status": row.status}
