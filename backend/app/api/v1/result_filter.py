"""Stateful Result-filter mutation endpoints.

The agent loop runs against a draft Result whose filter is persisted in
`result.filter`. Each mutation here:
  - validates `filter_version` (optimistic concurrency)
  - applies the change atomically
  - server-writes a `result_trace` row attributed to `"sgr"` with the
    before/after filter and candidate-count delta
  - returns the new version, full filter, count, and trace sequence

See ADR `docs/adrs/2026-05-14-stateful-filter-on-result.md`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.schemas.result_filter import (
    ClearIn,
    DocumentIdsIn,
    DocumentMutationOut,
    EntityFilterValuesIn,
    FieldFilterValuesIn,
    FilterMutationOut,
    IntrospectByResultIn,
    RemoveEntityFilterIn,
    RemoveFilesIn,
    ReplaceFilterIn,
    SetDocumentClassFilterIn,
    SetDossierClassFilterIn,
    SetDossierFilterIn,
    SetEntityFilterIn,
    SetFieldFilterIn,
    SetRegexFilterIn,
    SetTextSearchIn,
)
from app.schemas.runtime import IntrospectOut
from app.services import result_filter as svc

router = APIRouter(
    prefix="/retrieval/results/{result_id}",
    tags=["retrieval-results"],
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)


# ───────────────────────────── scopes ─────────────────────────────


@router.post("/filter/document-class/set", response_model=FilterMutationOut)
async def set_document_class_filter(
    result_id: uuid.UUID,
    body: SetDocumentClassFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.set_document_class_filter(
        session, caller, result_id, body.filter_version, body.document_class_id
    )


@router.post("/filter/document-class/clear", response_model=FilterMutationOut)
async def clear_document_class_filter(
    result_id: uuid.UUID,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.clear_document_class_filter(
        session, caller, result_id, body.filter_version
    )


@router.post("/filter/dossier/set", response_model=FilterMutationOut)
async def set_dossier_filter(
    result_id: uuid.UUID,
    body: SetDossierFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.set_dossier_filter(
        session, caller, result_id, body.filter_version, body.dossier_id
    )


@router.post("/filter/dossier-class/set", response_model=FilterMutationOut)
async def set_dossier_class_filter(
    result_id: uuid.UUID,
    body: SetDossierClassFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.set_dossier_class_filter(
        session, caller, result_id, body.filter_version, body.dossier_class_id
    )


@router.post("/filter/dossier/clear", response_model=FilterMutationOut)
async def clear_dossier_filters(
    result_id: uuid.UUID,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.clear_dossier_filters(
        session, caller, result_id, body.filter_version
    )


# ───────────────────────────── field filters ─────────────────────────────


@router.post("/filter/field-filters/{field}/set", response_model=FilterMutationOut)
async def set_field_filter(
    result_id: uuid.UUID,
    field: str,
    body: SetFieldFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.set_field_filter(
        session, caller, result_id, body.filter_version,
        field, body.op, body.values, body.value, body.join,
    )


@router.post(
    "/filter/field-filters/{field}/extend", response_model=FilterMutationOut
)
async def extend_field_filter_values(
    result_id: uuid.UUID,
    field: str,
    body: FieldFilterValuesIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.extend_field_filter_values(
        session, caller, result_id, body.filter_version, field, body.values
    )


@router.post(
    "/filter/field-filters/{field}/shrink", response_model=FilterMutationOut
)
async def shrink_field_filter_values(
    result_id: uuid.UUID,
    field: str,
    body: FieldFilterValuesIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.shrink_field_filter_values(
        session, caller, result_id, body.filter_version, field, body.values
    )


@router.post(
    "/filter/field-filters/{field}/remove", response_model=FilterMutationOut
)
async def remove_field_filter(
    result_id: uuid.UUID,
    field: str,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.remove_field_filter(
        session, caller, result_id, body.filter_version, field
    )


# ───────────────────────────── entity filters ─────────────────────────────


@router.post("/filter/entity-filters/set", response_model=FilterMutationOut)
async def set_entity_filter(
    result_id: uuid.UUID,
    body: SetEntityFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.set_entity_filter(
        session, caller, result_id, body.filter_version,
        body.entity_type_id, body.entity_ids,
    )


@router.post("/filter/entity-filters/extend", response_model=FilterMutationOut)
async def extend_entity_filter(
    result_id: uuid.UUID,
    body: EntityFilterValuesIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.extend_entity_filter(
        session, caller, result_id, body.filter_version,
        body.entity_type_id, body.entity_ids,
    )


@router.post("/filter/entity-filters/shrink", response_model=FilterMutationOut)
async def shrink_entity_filter(
    result_id: uuid.UUID,
    body: EntityFilterValuesIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.shrink_entity_filter(
        session, caller, result_id, body.filter_version,
        body.entity_type_id, body.entity_ids,
    )


@router.post("/filter/entity-filters/remove", response_model=FilterMutationOut)
async def remove_entity_filter(
    result_id: uuid.UUID,
    body: RemoveEntityFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.remove_entity_filter(
        session, caller, result_id, body.filter_version, body.entity_type_id
    )


# ───────────────────────────── regex filters ─────────────────────────────


@router.post("/filter/regex-filters/{field}/set", response_model=FilterMutationOut)
async def set_regex_filter(
    result_id: uuid.UUID,
    field: str,
    body: SetRegexFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.set_regex_filter(
        session, caller, result_id, body.filter_version, field, body.pattern
    )


@router.post("/filter/regex-filters/{field}/remove", response_model=FilterMutationOut)
async def remove_regex_filter(
    result_id: uuid.UUID,
    field: str,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.remove_regex_filter(
        session, caller, result_id, body.filter_version, field
    )


# ───────────────────────────── text search ─────────────────────────────


@router.post("/filter/text-search/set", response_model=FilterMutationOut)
async def set_text_search(
    result_id: uuid.UUID,
    body: SetTextSearchIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.set_text_search(
        session, caller, result_id, body.filter_version, body.query
    )


@router.post("/filter/text-search/clear", response_model=FilterMutationOut)
async def clear_text_search(
    result_id: uuid.UUID,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.clear_text_search(
        session, caller, result_id, body.filter_version
    )


# ───────────────────────────── explicit includes/excludes ─────────────────────────────


@router.post("/filter/includes/add", response_model=FilterMutationOut)
async def add_explicit_includes(
    result_id: uuid.UUID,
    body: DocumentIdsIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.add_explicit_includes(
        session, caller, result_id, body.filter_version, body.document_ids
    )


@router.post("/filter/includes/remove", response_model=FilterMutationOut)
async def remove_explicit_includes(
    result_id: uuid.UUID,
    body: DocumentIdsIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.remove_explicit_includes(
        session, caller, result_id, body.filter_version, body.document_ids
    )


@router.post("/filter/includes/clear", response_model=FilterMutationOut)
async def clear_explicit_includes(
    result_id: uuid.UUID,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.clear_explicit_includes(
        session, caller, result_id, body.filter_version
    )


@router.post("/filter/excludes/add", response_model=FilterMutationOut)
async def add_explicit_excludes(
    result_id: uuid.UUID,
    body: DocumentIdsIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.add_explicit_excludes(
        session, caller, result_id, body.filter_version, body.document_ids
    )


@router.post("/filter/excludes/remove", response_model=FilterMutationOut)
async def remove_explicit_excludes(
    result_id: uuid.UUID,
    body: DocumentIdsIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.remove_explicit_excludes(
        session, caller, result_id, body.filter_version, body.document_ids
    )


@router.post("/filter/excludes/clear", response_model=FilterMutationOut)
async def clear_explicit_excludes(
    result_id: uuid.UUID,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.clear_explicit_excludes(
        session, caller, result_id, body.filter_version
    )


# ───────────────────────────── whole-filter ops ─────────────────────────────


@router.post("/filter/clear", response_model=FilterMutationOut)
async def clear_filter(
    result_id: uuid.UUID,
    body: ClearIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.clear_filter(session, caller, result_id, body.filter_version)


@router.post("/filter/replace", response_model=FilterMutationOut)
async def replace_filter(
    result_id: uuid.UUID,
    body: ReplaceFilterIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await svc.replace_filter(
        session, caller, result_id, body.filter_version, body.filter
    )


# ───────────────────────────── introspect against persisted filter ─────────────────────────────


@router.post("/introspect", response_model=IntrospectOut)
async def introspect_by_result(
    result_id: uuid.UUID,
    body: IntrospectByResultIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Run introspect using the persisted filter on this result. Optional
    `overlay` lets the agent probe "what would these clauses change?"
    without committing — overlay is NOT persisted."""
    return await svc.introspect_by_result(
        session, caller, result_id, body.fields, body.top_k, body.overlay
    )


@router.get("/candidates")
async def list_candidates(
    result_id: uuid.UUID,
    limit: int = 10,
    offset: int = 0,
    order_by: str = "filename",
    direction: str = "asc",
    include_toc: bool = False,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Page through documents matching the result's persisted filter.

    Lets the agent look at the actual candidates (filename, summary, optional
    ToC, class) before deciding what to `add_files_to_result`. Visibility-scoped.
    Response is `{rows, has_more, next_offset}`; call again with `next_offset`
    while `has_more`.

    Order_by: `filename` | `created_at` | `updated_at` |
    `classification_confidence` | `property:<name>`. Default 10/page;
    keep responses tool-call-result friendly.
    """
    return await svc.list_candidates_by_result(
        session, caller, result_id,
        limit=limit, offset=offset, overlay=None,
        order_by=order_by, direction=direction, include_toc=include_toc,
    )


# ───────────────────────────── draft-result document mutations ─────────────────────────────


@router.post("/files/remove", response_model=DocumentMutationOut)
async def remove_files_from_result(
    result_id: uuid.UUID,
    body: RemoveFilesIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    remaining, seq = await svc.remove_files_from_result(
        session, caller, result_id, body.document_ids, body.reason
    )
    return DocumentMutationOut(document_count=remaining, trace_sequence=seq)


@router.post("/files/clear", response_model=DocumentMutationOut)
async def clear_result_files(
    result_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    remaining, seq = await svc.clear_result_files(session, caller, result_id)
    return DocumentMutationOut(document_count=remaining, trace_sequence=seq)
