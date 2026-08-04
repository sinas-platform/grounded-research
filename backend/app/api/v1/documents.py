"""Document and document-version reads with visibility filtering."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models import (
    Document,
    DocumentVersion,
    Dossier,
    DossierDocument,
    Entity,
    EntityMention,
    EntityType,
    PropertyValue,
)
from app.schemas.runtime import (
    DocumentOut,
    DocumentPatch,
    DocumentVersionOut,
    EntityMentionWithEntityOut,
    PropertyValueOut,
)
from app.services.visibility import visible_clause

router = APIRouter(prefix="/documents", tags=["documents"])


async def _document_visibility(model, caller: CallerIdentity):
    read_all = await caller.has_permission("grove.documents.read:all")
    base = visible_clause(model, caller, read_all=read_all)
    if read_all or caller.is_admin:
        return base
    # Documents reachable through a visible dossier (cascading access)
    from sqlalchemy import or_

    dossier_subq = (
        select(DossierDocument.document_id)
        .join(Dossier, Dossier.id == DossierDocument.dossier_id)
        .where(visible_clause(Dossier, caller, read_all=False))
    )
    return or_(base, model.id.in_(dossier_subq))


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    document_class_id: uuid.UUID | None = Query(default=None),
    unclassified_only: bool = Query(default=False),
    staged_only: bool = Query(default=False),
    include_staged: bool = Query(
        default=False,
        description="If true, staged docs appear alongside the rest. Default excludes them.",
    ),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    stmt = select(Document).where(await _document_visibility(Document, caller))
    if staged_only:
        stmt = stmt.where(Document.staged.is_(True))
    elif unclassified_only:
        stmt = stmt.where(Document.document_class_id.is_(None))
        if not include_staged:
            stmt = stmt.where(Document.staged.is_(False))
    else:
        if document_class_id is not None:
            stmt = stmt.where(Document.document_class_id == document_class_id)
        if not include_staged:
            stmt = stmt.where(Document.staged.is_(False))
    stmt = stmt.order_by(desc(Document.created_at)).limit(limit).offset(offset)
    return (await session.execute(stmt)).scalars().all()


class DocumentCountsOut(BaseModel):
    total: int
    unclassified: int
    staged: int
    by_class: dict[str, int]  # document_class_id (str) -> count, unstaged only


@router.get("/counts", response_model=DocumentCountsOut)
async def get_document_counts(
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    visibility = await _document_visibility(Document, caller)
    # Staged is its own bucket — count separately so by_class reflects only
    # processed docs (the user-facing dimension).
    staged_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Document)
                .where(visibility)
                .where(Document.staged.is_(True))
            )
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(Document.document_class_id, func.count())
            .where(visibility)
            .where(Document.staged.is_(False))
            .group_by(Document.document_class_id)
        )
    ).all()
    by_class: dict[str, int] = {}
    unclassified = 0
    for class_id, count in rows:
        if class_id is None:
            unclassified = int(count)
        else:
            by_class[str(class_id)] = int(count)
    return DocumentCountsOut(
        total=unclassified + sum(by_class.values()) + staged_count,
        unclassified=unclassified,
        staged=staged_count,
        by_class=by_class,
    )


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    stmt = (
        select(Document)
        .where(Document.id == doc_id)
        .where(await _document_visibility(Document, caller))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return row


@router.patch(
    "/{doc_id}",
    response_model=DocumentOut,
    dependencies=[Depends(require_permission("grove.documents.write:own"))],
)
async def update_document(
    doc_id: uuid.UUID,
    payload: DocumentPatch,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = await session.get(Document, doc_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    write_all = await caller.has_permission("grove.documents.write:all")
    if not (write_all or caller.is_admin or row.owner_id == caller.user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your document")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.get("/{doc_id}/versions", response_model=list[DocumentVersionOut])
async def list_document_versions(
    doc_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    # Visibility check on the document itself
    parent = (
        await session.execute(
            select(Document)
            .where(Document.id == doc_id)
            .where(await _document_visibility(Document, caller))
        )
    ).scalar_one_or_none()
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    rows = (
        await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == doc_id)
            .order_by(DocumentVersion.version)
        )
    ).scalars().all()
    return rows


# Max lines returned per call. Keeps tool responses small enough for agents
# to handle multiple chunks across turns without blowing the context window.
_READ_MAX_LINES = 500


@router.get("/{doc_id}/versions/{version}/content")
async def read_document_content(
    doc_id: uuid.UUID,
    version: int,
    line_from: int | None = Query(default=None, ge=1),
    line_to: int | None = Query(default=None, ge=1),
    numbered: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    parent = (
        await session.execute(
            select(Document)
            .where(Document.id == doc_id)
            .where(await _document_visibility(Document, caller))
        )
    ).scalar_one_or_none()
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    dv = (
        await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == doc_id)
            .where(DocumentVersion.version == version)
        )
    ).scalar_one_or_none()
    if dv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    if dv.content_md is None:
        return {
            "document_id": doc_id,
            # The version's UUID, not just its number: writers that reference
            # this exact version (mentions, property values) need the id, and
            # this response is the only place a reading agent can get it.
            "document_version_id": dv.id,
            "version": version,
            "line_from": None,
            "line_to": None,
            "total_lines": 0,
            "returned_lines": 0,
            "next_line": None,
            "is_truncated": False,
            "content": "",
            "extracted": False,
            "note": "version exists but no extracted text — post-upload extraction may have failed or hasn't run yet",
        }

    lines = dv.content_md.splitlines()
    total = len(lines)

    # Resolve the requested window, capped to _READ_MAX_LINES so a single
    # call never returns more than that. The agent paginates by calling
    # again with line_from = response.next_line.
    start_1 = line_from or 1
    requested_end_1 = line_to if line_to is not None else total
    end_1 = min(requested_end_1, start_1 + _READ_MAX_LINES - 1, total)

    # Convert to 0-based slice and extract.
    start_0 = start_1 - 1
    if start_0 >= total:
        slice_lines: list[str] = []
        actual_start_1 = start_1
        actual_end_1 = start_1 - 1  # empty
    else:
        slice_lines = lines[start_0:end_1]
        actual_start_1 = start_1
        actual_end_1 = start_0 + len(slice_lines)

    next_line: int | None
    if actual_end_1 >= total:
        next_line = None
    else:
        next_line = actual_end_1 + 1

    # With numbered=true every line carries its absolute line number, so a
    # reader can cite line_from/line_to spans by copying visible numbers,
    # never by counting (LLM line-counting is unreliable; UI readers keep
    # the clean text).
    body = "\n".join(slice_lines)
    if numbered:
        body = "\n".join(
            f"{actual_start_1 + i}│{ln}" for i, ln in enumerate(slice_lines)
        )

    return {
        "document_id": doc_id,
        # See the empty-content branch: the UUID a writer must reference.
        "document_version_id": dv.id,
        "version": version,
        "line_from": actual_start_1,
        "line_to": actual_end_1,
        "total_lines": total,
        "returned_lines": len(slice_lines),
        "next_line": next_line,
        "is_truncated": next_line is not None,
        "content": body,
        "extracted": True,
    }


@router.get("/{doc_id}/property-values", response_model=list[PropertyValueOut])
async def list_property_values(
    doc_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    parent = (
        await session.execute(
            select(Document)
            .where(Document.id == doc_id)
            .where(await _document_visibility(Document, caller))
        )
    ).scalar_one_or_none()
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    rows = (
        await session.execute(
            select(PropertyValue).where(PropertyValue.document_id == doc_id)
        )
    ).scalars().all()
    return rows


@router.get(
    "/{doc_id}/entity-mentions", response_model=list[EntityMentionWithEntityOut]
)
async def list_entity_mentions(
    doc_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    parent = (
        await session.execute(
            select(Document)
            .where(Document.id == doc_id)
            .where(await _document_visibility(Document, caller))
        )
    ).scalar_one_or_none()
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    rows = (
        await session.execute(
            select(EntityMention, Entity, EntityType)
            .join(Entity, Entity.id == EntityMention.entity_id)
            .join(EntityType, EntityType.id == Entity.entity_type_id)
            .where(EntityMention.document_id == doc_id)
            .where(EntityMention.status == "active")
            .order_by(EntityMention.created_at)
        )
    ).all()
    return [
        EntityMentionWithEntityOut(
            id=mention.id,
            created_at=mention.created_at,
            updated_at=mention.updated_at,
            document_id=mention.document_id,
            document_version_id=mention.document_version_id,
            entity_id=mention.entity_id,
            span=mention.span,
            confidence=mention.confidence,
            entity_canonical_form=entity.canonical_form,
            entity_type_id=entity.entity_type_id,
            entity_type_name=etype.name,
        )
        for mention, entity, etype in rows
    ]
