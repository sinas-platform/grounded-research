"""Entities — read views, proposal review, unresolved-mention resolution.

Mirrors `relationships.py`. Created when EntityType.creation_mode is `review`
or `closed`, the rows here hold mentions the extractor couldn't auto-promote.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models import (
    Entity,
    EntityMention,
    EntityProposal,
    UnresolvedEntityMention,
)
from app.schemas.runtime import (
    EntityProposalOut,
    UnresolvedEntityMentionOut,
)

router = APIRouter(prefix="/entities", tags=["entities"])


class EntitySummary(BaseModel):
    id: uuid.UUID
    entity_type_id: uuid.UUID
    canonical_form: str


@router.get("", response_model=list[EntitySummary])
async def list_entities(
    entity_type_id: uuid.UUID | None = None,
    q: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Entity)
    if entity_type_id is not None:
        stmt = stmt.where(Entity.entity_type_id == entity_type_id)
    if q:
        stmt = stmt.where(Entity.canonical_form.ilike(f"%{q}%"))
    stmt = stmt.order_by(Entity.canonical_form).limit(limit)
    return (await session.execute(stmt)).scalars().all()


class EntityDocumentRow(BaseModel):
    id: uuid.UUID
    filename: str
    summary: str | None = None
    document_class_id: uuid.UUID | None = None
    mention_count: int


@router.get("/{entity_id}/documents", response_model=list[EntityDocumentRow])
async def list_documents_by_entity(
    entity_id: uuid.UUID,
    limit: int = 25,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """List documents that mention `entity_id`. Visibility-scoped.

    Returns one row per document with the count of mentions of this entity
    in that document. Pagination is offset-based.
    """
    from sqlalchemy import func as sa_func

    from app.models import Document, EntityMention
    from app.services.visibility import visible_clause

    read_all = await caller.has_permission("sgr.documents.read:all")
    stmt = (
        select(
            Document.id,
            Document.filename,
            Document.summary,
            Document.document_class_id,
            sa_func.count(EntityMention.id).label("mention_count"),
        )
        .join(EntityMention, EntityMention.document_id == Document.id)
        .where(EntityMention.entity_id == entity_id)
        .where(EntityMention.status == "active")
        .where(visible_clause(Document, caller, read_all=read_all))
        .group_by(Document.id)
        .order_by(sa_func.count(EntityMention.id).desc(), Document.filename)
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    return [
        EntityDocumentRow(
            id=r.id,
            filename=r.filename,
            summary=r.summary,
            document_class_id=r.document_class_id,
            mention_count=int(r.mention_count),
        )
        for r in rows
    ]


@router.get("/proposals", response_model=list[EntityProposalOut])
async def list_entity_proposals(
    status_filter: str = "pending",
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(
            select(EntityProposal)
            .where(EntityProposal.status == status_filter)
            .order_by(EntityProposal.created_at.desc())
        )
    ).scalars().all()
    return rows


class EntityProposalDecision(BaseModel):
    approve: bool


@router.post(
    "/proposals/{proposal_id}/decision",
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def decide_entity_proposal(
    proposal_id: uuid.UUID,
    payload: EntityProposalDecision,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    proposal = await session.get(EntityProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    if proposal.status != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"proposal already {proposal.status}"
        )
    proposal.status = "approved" if payload.approve else "rejected"
    proposal.reviewed_at = datetime.now(timezone.utc)
    proposal.reviewed_by = caller.user_id

    promoted_id: uuid.UUID | None = None
    if payload.approve:
        entity = Entity(
            entity_type_id=proposal.entity_type_id,
            canonical_form=proposal.canonical_form,
            extra_metadata=proposal.extra_metadata,
        )
        session.add(entity)
        await session.flush()
        proposal.promoted_entity_id = entity.id
        promoted_id = entity.id

    await session.commit()
    return {"status": proposal.status, "promoted_entity_id": promoted_id}


class UnresolvedRow(UnresolvedEntityMentionOut):
    """The row plus what a reviewer needs to actually decide: the sentence the
    mention sits in, and which document it came from. Deciding what "the
    Commission" refers to is impossible from a surface form and a UUID — the
    adjudicator gets this context, and so should the person."""

    filename: str | None = None
    context: str | None = None


@router.get("/unresolved", response_model=list[UnresolvedRow])
async def list_unresolved_entity_mentions(
    status_filter: str = "unresolved",
    entity_type_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    from app.models import Document, DocumentVersion
    from app.services.entity_resolver import _slice

    stmt = select(UnresolvedEntityMention).where(
        UnresolvedEntityMention.status == status_filter
    )
    if entity_type_id is not None:
        stmt = stmt.where(UnresolvedEntityMention.entity_type_id == entity_type_id)
    if document_id is not None:
        stmt = stmt.where(UnresolvedEntityMention.document_id == document_id)
    stmt = stmt.order_by(UnresolvedEntityMention.created_at.desc())
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()

    # one content fetch per distinct document, not per mention
    doc_ids = {r.document_id for r in rows}
    docs: dict[uuid.UUID, tuple[str | None, str]] = {}
    if doc_ids:
        for did, fn, content in (await session.execute(
            select(Document.id, Document.filename, DocumentVersion.content_md)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(Document.id.in_(doc_ids))
        )).all():
            docs[did] = (fn, content or "")

    out = []
    for r in rows:
        fn, content = docs.get(r.document_id, (None, ""))
        item = UnresolvedRow.model_validate(r, from_attributes=True)
        item.filename = fn
        item.context = _slice(content, r.span) if content else None
        out.append(item)
    return out


class MatchToExistingBody(BaseModel):
    entity_id: uuid.UUID
    add_alias: bool = True


@router.post(
    "/unresolved/{mention_id}/match",
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def match_unresolved_to_entity(
    mention_id: uuid.UUID,
    payload: MatchToExistingBody,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = await session.get(UnresolvedEntityMention, mention_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unresolved mention not found")
    if row.status != "unresolved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {row.status}")
    entity = await session.get(Entity, payload.entity_id)
    if entity is None or entity.entity_type_id != row.entity_type_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "target entity not found or has a different entity type",
        )

    # Mentions arrive before resolution, so the mention this row describes
    # already exists with a null entity — link THAT one. Creating a fresh row
    # here would leave two mentions on one span, one of them orphaned.
    existing_mention = (
        await session.execute(
            select(EntityMention).where(
                EntityMention.document_id == row.document_id,
                EntityMention.span == row.span,
                EntityMention.entity_id.is_(None),
            ).limit(1)
        )
    ).scalars().first()
    if existing_mention is not None:
        existing_mention.entity_id = entity.id
        existing_mention.link_method = "manual"
        existing_mention.link_confidence = 1.0
        existing_mention.link_evidence = {"resolved_by_review": True}
    else:
        session.add(
            EntityMention(
                document_id=row.document_id,
                document_version_id=row.document_version_id,
                entity_id=entity.id,
                span=row.span,
                confidence=row.confidence,
            )
        )
    if payload.add_alias and row.mention_text != entity.canonical_form:
        from app.models import EntityAlias

        existing = (
            await session.execute(
                select(EntityAlias).where(
                    EntityAlias.entity_id == entity.id,
                    EntityAlias.alias == row.mention_text,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(EntityAlias(entity_id=entity.id, alias=row.mention_text))

    row.status = "resolved"
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by = caller.user_id
    row.resolved_entity_id = entity.id

    # Also resolve any other open mentions of the same surface form in this
    # type — and link THEIR mentions as well. Flipping status alone would
    # report hundreds resolved while the mentions stayed unlinked.
    siblings = (
        await session.execute(
            select(UnresolvedEntityMention).where(
                UnresolvedEntityMention.entity_type_id == row.entity_type_id,
                UnresolvedEntityMention.mention_text == row.mention_text,
                UnresolvedEntityMention.status == "unresolved",
                UnresolvedEntityMention.id != row.id,
            )
        )
    ).scalars().all()
    for sib in siblings:
        sib_mention = (
            await session.execute(
                select(EntityMention).where(
                    EntityMention.document_id == sib.document_id,
                    EntityMention.span == sib.span,
                    EntityMention.entity_id.is_(None),
                ).limit(1)
            )
        ).scalars().first()
        if sib_mention is not None:
            sib_mention.entity_id = entity.id
            sib_mention.link_method = "manual"
            sib_mention.link_confidence = 1.0
            sib_mention.link_evidence = {"resolved_by_review": True,
                                         "via": str(row.id)}
    await session.execute(
        update(UnresolvedEntityMention)
        .where(
            UnresolvedEntityMention.entity_type_id == row.entity_type_id,
            UnresolvedEntityMention.mention_text == row.mention_text,
            UnresolvedEntityMention.status == "unresolved",
            UnresolvedEntityMention.id != row.id,
        )
        .values(
            status="resolved",
            resolved_at=datetime.now(timezone.utc),
            resolved_by=caller.user_id,
            resolved_entity_id=entity.id,
        )
    )

    await session.commit()
    return {"status": "resolved", "entity_id": entity.id,
            "also_resolved": len(siblings)}


@router.post(
    "/unresolved/{mention_id}/promote",
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def promote_unresolved_to_entity(
    mention_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Create a new Entity from this mention. Only safe to call if the operator
    has accepted that the EntityType's closed set should grow."""
    row = await session.get(UnresolvedEntityMention, mention_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unresolved mention not found")
    if row.status != "unresolved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {row.status}")

    entity = Entity(
        entity_type_id=row.entity_type_id,
        canonical_form=row.mention_text,
    )
    session.add(entity)
    await session.flush()
    session.add(
        EntityMention(
            document_id=row.document_id,
            document_version_id=row.document_version_id,
            entity_id=entity.id,
            span=row.span,
            confidence=row.confidence,
        )
    )
    row.status = "resolved"
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by = caller.user_id
    row.resolved_entity_id = entity.id
    await session.commit()
    return {"status": "resolved", "entity_id": entity.id}


@router.post(
    "/unresolved/{mention_id}/dismiss",
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def dismiss_unresolved(
    mention_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = await session.get(UnresolvedEntityMention, mention_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unresolved mention not found")
    if row.status != "unresolved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"already {row.status}")
    row.status = "dismissed"
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by = caller.user_id
    await session.commit()
    return {"status": "dismissed"}
