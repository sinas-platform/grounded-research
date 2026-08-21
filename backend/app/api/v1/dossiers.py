"""Dossiers — only meaningful when dossier_class rows exist."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models import Dossier, DossierDocument
from app.schemas.runtime import DossierDocumentIn, DossierDocumentOut, DossierIn, DossierOut
from app.services.visibility import visible_clause

router = APIRouter(prefix="/dossiers", tags=["dossiers"])


@router.get("", response_model=list[DossierOut])
async def list_dossiers(
    dossier_class_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    read_all = await caller.has_permission("sgr.dossiers.read:all")
    stmt = select(Dossier).where(visible_clause(Dossier, caller, read_all=read_all))
    if dossier_class_id is not None:
        stmt = stmt.where(Dossier.dossier_class_id == dossier_class_id)
    return (await session.execute(stmt.order_by(Dossier.created_at.desc()))).scalars().all()


@router.post(
    "",
    response_model=DossierOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.dossiers.write:own"))],
)
async def create_dossier(
    payload: DossierIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = Dossier(
        **payload.model_dump(), owner_id=caller.user_id, roles=caller.roles or []
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.get("/{dossier_id}", response_model=DossierOut)
async def get_dossier(
    dossier_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    read_all = await caller.has_permission("sgr.dossiers.read:all")
    stmt = (
        select(Dossier)
        .where(Dossier.id == dossier_id)
        .where(visible_clause(Dossier, caller, read_all=read_all))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dossier not found")
    return row


@router.post(
    "/{dossier_id}/documents",
    response_model=DossierDocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.dossiers.write:own"))],
)
async def assign_dossier_document(
    dossier_id: uuid.UUID,
    payload: DossierDocumentIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = DossierDocument(
        dossier_id=dossier_id,
        document_id=payload.document_id,
        role=payload.role,
        added_by=caller.user_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.get("/{dossier_id}/documents", response_model=list[DossierDocumentOut])
async def list_dossier_documents(
    dossier_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    rows = (
        await session.execute(
            select(DossierDocument).where(DossierDocument.dossier_id == dossier_id)
        )
    ).scalars().all()
    return rows
