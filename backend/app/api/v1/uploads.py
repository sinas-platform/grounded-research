"""Single-file upload — registers directly with SGR, the system of
record. This used to proxy the file into a Sinas collection whose
post-upload function called back into SGR: an extra hop, a 9.6s-per-file
function execution, and an identity scheme (collection_file_id) that
bypassed content-hash dedup. SGR-native now, same write path as bulk."""

from __future__ import annotations

import uuid

from fastapi import (APIRouter, Depends, File, Form, HTTPException,
                     UploadFile, status)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller
from app.db import get_session
from app.models import DocumentClass

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    file: UploadFile = File(...),
    source: str | None = Form(
        default=None,
        description="Connector/source name; the filename becomes the "
                    "external_ref (the source's natural key) when set."),
    document_class: str | None = Form(
        default=None,
        description="Source-declared class name; applied on create, and on "
                    "re-upload only when the document has no class yet."),
    staged: bool = Form(
        default=False,
        description="If true, document is parked and the auto-pipeline "
                    "doesn't fire. Used for the discovery upload flow."),
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "uploads must be UTF-8 text (markdown)")

    declared_class_id: uuid.UUID | None = None
    if document_class is not None:
        declared = (await session.execute(
            select(DocumentClass).where(DocumentClass.name == document_class)
        )).scalar_one_or_none()
        if declared is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown document class {document_class!r}")
        declared_class_id = declared.id

    from app.services.document_registry import register_document

    reg = await register_document(
        session, filename=file.filename or "upload.md", content=content,
        owner_id=caller.user_id, roles=caller.roles,
        source=source, staged=staged,
        document_class_id=declared_class_id)
    await session.commit()

    if reg.outcome in ("created", "new_version") and not staged:
        from app.api.v1.bulk import _spawn

        _spawn(uuid.uuid4().hex[:12], [str(reg.document.id)],
               "extract,resolve,relationships")

    return {
        "status": reg.outcome,
        "document_id": str(reg.document.id) if reg.document else None,
        "version": reg.version,
        "staged": staged,
    }
