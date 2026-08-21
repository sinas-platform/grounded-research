"""Relationships — read views and proposal review."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models import (
    Entity,
    Relationship,
    RelationshipDefinition,
    RelationshipProposal,
    UnresolvedRelationship,
)
from app.schemas.runtime import (
    RelationshipOut,
    RelationshipProposalOut,
    UnresolvedRelationshipOut,
)

router = APIRouter(prefix="/relationships", tags=["relationships"])


@router.get("", response_model=list[RelationshipOut])
async def list_relationships(
    relationship_definition_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    evidence_document_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Relationship)
    if relationship_definition_id is not None:
        stmt = stmt.where(Relationship.relationship_definition_id == relationship_definition_id)
    if source_id is not None:
        stmt = stmt.where(Relationship.source_id == source_id)
    if target_id is not None:
        stmt = stmt.where(Relationship.target_id == target_id)
    if evidence_document_id is not None:
        stmt = stmt.where(Relationship.evidence_document_id == evidence_document_id)
    return (await session.execute(stmt)).scalars().all()


class RelationshipProposalRow(RelationshipProposalOut):
    """A proposal plus the names behind its ids. A reviewer decides on
    "<source> → <definition> → <target>", not on three UUIDs, and joining
    here beats three lookups per row from the client."""

    definition_name: str | None = None
    source_name: str | None = None
    target_name: str | None = None


@router.get("/proposals", response_model=list[RelationshipProposalRow])
async def list_proposals(
    status_filter: str = "pending",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    src = aliased(Entity)
    tgt = aliased(Entity)
    rows = (
        await session.execute(
            select(
                RelationshipProposal,
                RelationshipDefinition.name,
                src.canonical_form,
                tgt.canonical_form,
            )
            .outerjoin(
                RelationshipDefinition,
                RelationshipDefinition.id
                == RelationshipProposal.relationship_definition_id,
            )
            .outerjoin(src, src.id == RelationshipProposal.source_id)
            .outerjoin(tgt, tgt.id == RelationshipProposal.target_id)
            .where(RelationshipProposal.status == status_filter)
            .order_by(RelationshipProposal.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        RelationshipProposalRow.model_validate(
            {
                **RelationshipProposalOut.model_validate(
                    p, from_attributes=True
                ).model_dump(),
                "definition_name": dname,
                "source_name": sname,
                "target_name": tname,
            }
        )
        for p, dname, sname, tname in rows
    ]


@router.get("/unresolved", response_model=list[UnresolvedRelationshipOut])
async def list_unresolved(
    status_filter: str = "unresolved",
    source_id: uuid.UUID | None = None,
    target_key: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(UnresolvedRelationship).where(
        UnresolvedRelationship.status == status_filter
    )
    if source_id is not None:
        stmt = stmt.where(UnresolvedRelationship.source_id == source_id)
    if target_key is not None:
        stmt = stmt.where(UnresolvedRelationship.target_key == target_key)
    stmt = stmt.order_by(UnresolvedRelationship.created_at.desc())
    return (await session.execute(stmt)).scalars().all()


class ProposalDecision(BaseModel):
    approve: bool


@router.post(
    "/proposals/{proposal_id}/decision",
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def decide_proposal(
    proposal_id: uuid.UUID,
    payload: ProposalDecision,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    proposal = await session.get(RelationshipProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    proposal.status = "approved" if payload.approve else "rejected"
    proposal.reviewed_at = datetime.now(timezone.utc)
    proposal.reviewed_by = caller.user_id

    if payload.approve:
        rel = Relationship(
            relationship_definition_id=proposal.relationship_definition_id,
            source_id=proposal.source_id,
            target_id=proposal.target_id,
            current_state_id=proposal.suggested_state_id,
            evidence_document_id=proposal.evidence_document_id,
            evidence_span=proposal.evidence_span,
            confidence=proposal.confidence,
            last_verified_at=datetime.now(timezone.utc),
        )
        session.add(rel)

    await session.commit()
    return {"status": proposal.status}
