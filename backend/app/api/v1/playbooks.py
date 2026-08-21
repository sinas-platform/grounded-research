"""Playbooks — markdown guidance for retrieval and synthesis agents.

Lives entirely in SGR. The deep-search and synthesis agents fetch
applicable playbooks via the SGR connector (`list_applicable_playbooks`
+ `get_playbook`) at runtime; nothing is stored in Sinas.

A playbook's `kind` is `retrieval` or `synthesis`. Scope rows attach a
playbook to document and/or dossier classes; zero scope rows means
"applies everywhere."
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_permission
from app.db import get_session
from app.models import Playbook, PlaybookScope
from app.schemas.config import (
    PlaybookIn,
    PlaybookOut,
    PlaybookScopeIn,
    PlaybookScopeOut,
)

router = APIRouter(prefix="/playbooks", tags=["playbooks"])

PlaybookKind = Literal["retrieval", "synthesis"]


# ─────────────────────────── CRUD ───────────────────────────
@router.get("/{kind}", response_model=list[PlaybookOut])
async def list_playbooks(kind: PlaybookKind, session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(Playbook).where(Playbook.kind == kind).order_by(Playbook.name))
    ).scalars().all()
    return rows


@router.get("/{kind}/{name}", response_model=PlaybookOut)
async def get_playbook(
    kind: PlaybookKind, name: str, session: AsyncSession = Depends(get_session)
):
    row = (
        await session.execute(
            select(Playbook).where(Playbook.kind == kind, Playbook.name == name)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "playbook not found")
    return row


@router.put(
    "/{kind}/{name}",
    response_model=PlaybookOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def upsert_playbook(
    kind: PlaybookKind,
    name: str,
    payload: PlaybookIn,
    session: AsyncSession = Depends(get_session),
):
    if payload.kind != kind or payload.name != name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "path kind/name must match payload"
        )
    existing = (
        await session.execute(
            select(Playbook).where(Playbook.kind == kind, Playbook.name == name)
        )
    ).scalar_one_or_none()
    if existing is None:
        row = Playbook(
            kind=kind, name=name, description=payload.description, content=payload.content
        )
        session.add(row)
    else:
        row = existing
        row.description = payload.description
        row.content = payload.content
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/{kind}/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_playbook(
    kind: PlaybookKind, name: str, session: AsyncSession = Depends(get_session)
):
    row = (
        await session.execute(
            select(Playbook).where(Playbook.kind == kind, Playbook.name == name)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "playbook not found")
    await session.delete(row)
    await session.commit()


# ─────────────────────────── scope ───────────────────────────
@router.get("/{kind}/{name}/scope", response_model=list[PlaybookScopeOut])
async def get_playbook_scope(
    kind: PlaybookKind, name: str, session: AsyncSession = Depends(get_session)
):
    pb = (
        await session.execute(
            select(Playbook).where(Playbook.kind == kind, Playbook.name == name)
        )
    ).scalar_one_or_none()
    if pb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "playbook not found")
    rows = (
        await session.execute(
            select(PlaybookScope).where(PlaybookScope.playbook_id == pb.id)
        )
    ).scalars().all()
    return rows


@router.post(
    "/{kind}/{name}/scope",
    response_model=PlaybookScopeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def add_playbook_scope(
    kind: PlaybookKind,
    name: str,
    payload: PlaybookScopeIn,
    session: AsyncSession = Depends(get_session),
):
    pb = (
        await session.execute(
            select(Playbook).where(Playbook.kind == kind, Playbook.name == name)
        )
    ).scalar_one_or_none()
    if pb is None or payload.playbook_id != pb.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "payload.playbook_id must match the playbook addressed by the path",
        )
    row = PlaybookScope(
        playbook_id=pb.id,
        document_class_id=payload.document_class_id,
        dossier_class_id=payload.dossier_class_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/scope/{scope_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_playbook_scope(
    scope_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = await session.get(PlaybookScope, scope_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scope row not found")
    await session.delete(row)
    await session.commit()


# ─────────────────────────── runtime lookup ───────────────────────────
class ApplicablePlaybookOut(BaseModel):
    """Minimal shape the agents consume.

    The agent calls `list_applicable_playbooks` to see what's in scope for the
    current context, then `get_playbook` to fetch the body of any it wants.
    """

    id: uuid.UUID
    kind: str
    name: str
    description: str


@router.get("/{kind}/applicable/list", response_model=list[ApplicablePlaybookOut])
async def list_applicable_playbooks(
    kind: PlaybookKind,
    document_class_id: uuid.UUID | None = None,
    dossier_class_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Return playbooks applicable to the given scope.

    A playbook with NO scope rows is universally applicable. A playbook with
    scope rows is applicable when at least one row matches the requested
    document_class_id and/or dossier_class_id. A scope row with both class
    refs NULL is treated as universally applicable (package-importer sentinel
    for "everywhere"-scoped playbooks).
    """
    all_pbs = (
        await session.execute(select(Playbook).where(Playbook.kind == kind))
    ).scalars().all()
    if not all_pbs:
        return []
    scope_rows = (
        await session.execute(
            select(PlaybookScope).where(
                PlaybookScope.playbook_id.in_([p.id for p in all_pbs])
            )
        )
    ).scalars().all()
    by_id: dict[uuid.UUID, list[PlaybookScope]] = {}
    for s in scope_rows:
        by_id.setdefault(s.playbook_id, []).append(s)

    out: list[ApplicablePlaybookOut] = []
    for pb in all_pbs:
        scopes = by_id.get(pb.id, [])
        applies = False
        if not scopes:
            applies = True
        else:
            for s in scopes:
                if s.document_class_id is None and s.dossier_class_id is None:
                    applies = True
                    break
                if (
                    document_class_id is not None
                    and s.document_class_id == document_class_id
                ):
                    applies = True
                    break
                if (
                    dossier_class_id is not None
                    and s.dossier_class_id == dossier_class_id
                ):
                    applies = True
                    break
        if applies:
            out.append(
                ApplicablePlaybookOut(
                    id=pb.id, kind=pb.kind, name=pb.name, description=pb.description
                )
            )
    return out
