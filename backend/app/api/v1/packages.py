"""SgrPackage import/export endpoints.

A SgrPackage is a single YAML file describing the full SGR-side domain
config (document classes, entity types, relationship definitions, dossier
classes, playbooks). Teams keep the file in their own repo and apply it
idempotently against a SGR deployment.

Playbook content is SGR-owned (see migration 0014); package operations
are pure SGR writes — no Sinas token required.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_permission
from app.db import get_session
from app.schemas.package import (
    PackageImportResult,
    PackageValidateResult,
)
from app.services import package as package_service

router = APIRouter(prefix="/packages", tags=["packages"])


@router.post("/validate", response_model=PackageValidateResult)
async def validate_package(yaml_text: str = Body(..., media_type="application/x-yaml")):
    return package_service.validate(yaml_text)


@router.post(
    "/import",
    response_model=PackageImportResult,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def import_package(
    yaml_text: str = Body(..., media_type="application/x-yaml"),
    prune: bool = Query(True, description="Delete managed resources missing from the manifest"),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await package_service.apply(yaml_text, session=session, prune=prune)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get(
    "/{name}/export",
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def export_package(
    name: str,
    version: str = Query("0.0.0"),
    session: AsyncSession = Depends(get_session),
):
    yaml_text = await package_service.export_package(
        name, session=session, version=version
    )
    return Response(content=yaml_text, media_type="application/x-yaml")
