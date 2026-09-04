"""Result reads — for the admin UI and downstream synthesis."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.annotations import _load_definitions
from app.auth import CallerIdentity, get_caller
from app.db import get_session
from app.models import Document, DocumentClass, Result, ResultDocument, ResultTrace
from app.schemas.common import TraceOut
from app.schemas.runtime import ResultDocumentCompactOut, ResultDocumentOut, ResultOut
from app.services.annotations import annotations_for_documents
from app.services.document_identity import document_title_subquery
from app.services.introspect import SUMMARY_PREVIEW_CHARS
from app.services.result_filter import load_visible_result
from app.services.visibility import visible_clause

router = APIRouter(prefix="/results", tags=["results"])


@router.get("", response_model=list[ResultOut])
async def list_results(
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
    limit: int = 50,
    offset: int = 0,
):
    read_all = await caller.has_permission("sgr.results.read:all")
    stmt = (
        select(Result)
        .where(visible_clause(Result, caller, read_all=read_all))
        .order_by(Result.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return (await session.execute(stmt)).scalars().all()


@router.get("/{result_id}", response_model=ResultOut)
async def get_result(
    result_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await load_visible_result(session, caller, result_id, for_write=False)


@router.get(
    "/{result_id}/documents",
    response_model=list[ResultDocumentOut] | list[ResultDocumentCompactOut],
)
async def get_result_documents(
    result_id: uuid.UUID,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    compact: bool = False,
    annotate: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated annotation names to attach per document, "
                "computed for the case entity the document is the full text "
                "of (e.g. annotate=issuing_body,authority_tier)."
            )
        ),
    ] = None,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    await load_visible_result(session, caller, result_id, for_write=False)
    """Attached documents with identifying fields joined in (filename, class
    name, summary preview), so a reader doesn't need a get_document call per
    row to learn what each attachment is.

    Defaults return the full rows, unpaged, as before. `limit`/`offset` page
    through large results (ordered by rank, then id, so pages are stable);
    `compact=true` returns only the identity fields, so a caller that needs
    just "which documents, in what order" gets many more rows per response.
    `annotate=` attaches derived graph fields (see the /annotations router)
    per row; unknown names are a 404.
    """
    stmt = (
        select(
            ResultDocument,
            Document.filename,
            DocumentClass.name,
            func.left(Document.summary, SUMMARY_PREVIEW_CHARS),
            Document.external_ref,
            document_title_subquery(),
        )
        .join(Document, Document.id == ResultDocument.document_id)
        .outerjoin(DocumentClass, DocumentClass.id == Document.document_class_id)
        .where(ResultDocument.result_id == result_id)
        .order_by(ResultDocument.rank.nulls_last(), ResultDocument.id)
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).all()

    annotations_by_doc: dict[uuid.UUID, dict] = {}
    if annotate is not None and rows:
        names = [n.strip() for n in annotate.split(",") if n.strip()]
        definitions = await _load_definitions(session, names or None)
        annotations_by_doc = await annotations_for_documents(
            session, [rd.document_id for rd, *_ in rows], definitions
        )

    if compact:
        return [
            ResultDocumentCompactOut(
                document_id=rd.document_id,
                filename=filename,
                document_class_name=class_name,
                rank=rd.rank,
                annotations=annotations_by_doc.get(rd.document_id),
            )
            for rd, filename, class_name, *_ in rows
        ]
    return [
        ResultDocumentOut(
            **ResultDocumentOut.model_validate(rd).model_dump(
                exclude={
                    "filename",
                    "document_class_name",
                    "summary",
                    "annotations",
                    "title",
                    "external_ref",
                }
            ),
            filename=filename,
            document_class_name=class_name,
            summary=summary,
            title=title,
            external_ref=external_ref,
            annotations=annotations_by_doc.get(rd.document_id),
        )
        for rd, filename, class_name, summary, external_ref, title in rows
    ]


@router.get("/{result_id}/trace", response_model=list[TraceOut])
async def get_trace(
    result_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    await load_visible_result(session, caller, result_id, for_write=False)
    rows = (
        await session.execute(
            select(ResultTrace)
            .where(ResultTrace.result_id == result_id)
            .order_by(ResultTrace.sequence)
        )
    ).scalars().all()
    return rows
