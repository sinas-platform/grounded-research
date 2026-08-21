"""Retrieval — introspect, draft results, claim/answer publishing.

The current draft filter is persisted on `Result.filter` and mutated via
the `/retrieval/results/{id}/filter/*` endpoints (see
`app.api.v1.result_filter` and ADR `2026-05-14-stateful-filter-on-result`).
The stateless `/retrieval/introspect` endpoint still exists for ad-hoc
queries (UI, scripts) that don't want to materialize a Result first.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.schemas.runtime import (
    SgrFilter,
    IntrospectIn,
    IntrospectOut,
    MatchingDocumentOut,
    MatchingDocumentsIn,
    MatchingDocumentsOut,
)
from app.services.introspect import (
    introspect_with_filter,
    matching_document_summaries,
)

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.post("/introspect", response_model=IntrospectOut)
async def introspect(
    payload: IntrospectIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Stateless introspect — pass a filter inline. Kept for ad-hoc/UI use.
    The agent loop should prefer `/results/{id}/introspect`, which uses the
    persisted filter and avoids re-emitting it on every iteration."""
    f = payload.filter or SgrFilter()
    return await introspect_with_filter(session, caller, f, payload.fields, payload.top_k)


@router.post("/matching-documents", response_model=MatchingDocumentsOut)
async def matching_documents(
    payload: MatchingDocumentsIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Enumerate the documents matching the filter.

    Companion to `/introspect`: introspect answers HOW MANY documents match and
    their value distribution; this answers WHICH ones, so the agent can add them
    to a Result. Each match carries its filename, class, and a summary preview,
    so the caller can tell what a document is without a follow-up get_document.
    Same filter body and query logic, capped by `limit` (default 50, max 200)."""
    f = payload.filter or SgrFilter()
    rows = await matching_document_summaries(session, caller, f, payload.limit)
    documents = [
        MatchingDocumentOut(
            id=r[0],
            filename=r[1],
            document_class_id=r[2],
            document_class_name=r[3],
            summary=r[4],
        )
        for r in rows
    ]
    return MatchingDocumentsOut(
        document_ids=[d.id for d in documents],
        documents=documents,
    )


# ───────────────────── draft result mutation operations ─────────────────────
class CreateResultIn(BaseModel):
    query: str
    invoked_skill_names: list[str] | None = None
    parent_result_id: uuid.UUID | None = None


class AddFilesIn(BaseModel):
    document_ids: list[uuid.UUID]
    reason: str | None = None
    added_by_agent: str | None = None


class TraceIn(BaseModel):
    sequence: int
    agent: str
    action: str
    parameters: dict | None = None
    outcome: dict | None = None


@router.post(
    "/results",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def create_draft_result(
    payload: CreateResultIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    from app.models import Result

    row = Result(
        query=payload.query,
        invoked_skill_names=payload.invoked_skill_names,
        parent_result_id=payload.parent_result_id,
        owner_id=caller.user_id,
        roles=caller.roles or [],
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id, "status": row.status}


@router.post(
    "/results/{result_id}/files",
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def add_files_to_result(
    result_id: uuid.UUID,
    payload: AddFilesIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    from datetime import datetime, timezone

    from app.models import ResultDocument, ResultTrace
    from app.services.result_filter import (
        AUTO_TRACE_AGENT,
        _next_trace_sequence,
        load_visible_result,
    )

    await load_visible_result(session, caller, result_id, for_write=True)
    for did in payload.document_ids:
        session.add(
            ResultDocument(
                result_id=result_id,
                document_id=did,
                reason=payload.reason,
                added_by_agent=payload.added_by_agent,
            )
        )
    # Server-enforced trace — adds were the one document mutation without one
    # (remove/clear/merge all trace), which let attachments happen silently.
    next_seq = await _next_trace_sequence(session, result_id)
    session.add(
        ResultTrace(
            result_id=result_id,
            sequence=next_seq,
            agent=AUTO_TRACE_AGENT,
            action="add_files_to_result",
            parameters={
                "document_ids": [str(d) for d in payload.document_ids],
                "reason": payload.reason,
                "added_by_agent": payload.added_by_agent,
            },
            outcome={"added": len(payload.document_ids)},
            occurred_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return {"added": len(payload.document_ids), "trace_sequence": next_seq}


class ExpandStep(BaseModel):
    relationship: str
    direction: str = Field(pattern="^(out|in)$")


class ExpandResultGraphIn(BaseModel):
    steps: list[ExpandStep] = Field(min_length=1, max_length=4)


@router.post(
    "/results/{result_id}/expand",
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def expand_result_graph(
    result_id: uuid.UUID,
    payload: ExpandResultGraphIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Caller-declared deterministic graph expansion — see
    services.result_filter.expand_result_graph."""
    from app.services.result_filter import expand_result_graph as svc

    return await svc(
        session, caller, result_id,
        steps=[st.model_dump() for st in payload.steps],
    )


class MergeResultsIn(BaseModel):
    child_result_ids: list[uuid.UUID] = Field(min_length=1)


@router.post(
    "/results/{result_id}/merge",
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def merge_results(
    result_id: uuid.UUID,
    payload: MergeResultsIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Deterministically merge child results into this parent: union of the
    children's documents (dedup, provenance preserved), child linkage, and a
    server-enforced trace. See services.result_filter.merge_results."""
    from app.services.result_filter import merge_results as merge_results_svc

    return await merge_results_svc(session, caller, result_id, payload.child_result_ids)


@router.post(
    "/results/{result_id}/trace",
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def append_trace(
    result_id: uuid.UUID,
    payload: TraceIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    from datetime import datetime, timezone

    from app.models import ResultTrace
    from app.services.result_filter import load_visible_result

    await load_visible_result(session, caller, result_id, for_write=True)
    session.add(
        ResultTrace(
            result_id=result_id,
            sequence=payload.sequence,
            agent=payload.agent,
            action=payload.action,
            parameters=payload.parameters,
            outcome=payload.outcome,
            occurred_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return {"ok": True}


@router.post(
    "/results/{result_id}/publish",
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def publish_result(
    result_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    from datetime import datetime, timezone

    from sqlalchemy import select as sa_select

    from app.models import Document, ResultDocument, ResultTrace
    from app.schemas.runtime import SgrFilter
    from app.services.introspect import apply_sgr_filter
    from app.services.result_filter import (
        AUTO_TRACE_AGENT,
        _next_trace_sequence,
        load_visible_result,
    )
    from app.services.visibility import visible_clause

    result = await load_visible_result(session, caller, result_id, for_write=True)

    # Reproducibility metric, recorded at the moment of publication: how much
    # of the attached set the persisted filter actually reproduces. Explicit
    # pins (citation-chain includes) are legitimate, but the drift between
    # "what the filter says" and "what is attached" must be measured and
    # visible, or the filter degrades into a decorative receipt.
    attached: set = set(
        (
            await session.execute(
                sa_select(ResultDocument.document_id).where(
                    ResultDocument.result_id == result_id
                )
            )
        ).scalars()
    )
    coverage: float | None = None
    if attached:
        f = SgrFilter(**(result.filter or {}))
        read_all = await caller.has_permission("sgr.documents.read:all")
        stmt = sa_select(Document.id).where(
            visible_clause(Document, caller, read_all=read_all)
        )
        stmt = apply_sgr_filter(stmt, f)
        selected = set((await session.execute(stmt)).scalars())
        coverage = round(len(attached & selected) / len(attached), 3)

    next_seq = await _next_trace_sequence(session, result_id)
    session.add(
        ResultTrace(
            result_id=result_id,
            sequence=next_seq,
            agent=AUTO_TRACE_AGENT,
            action="publish_result",
            parameters={},
            outcome={
                "attached_documents": len(attached),
                "filter_coverage": coverage,
            },
            occurred_at=datetime.now(timezone.utc),
        )
    )
    result.status = "published"
    result.published_at = datetime.now(timezone.utc)
    await session.commit()
    return {
        "id": result.id,
        "status": result.status,
        "attached_documents": len(attached),
        "filter_coverage": coverage,
    }
