"""Query runs — one-call, server-supervised question pipelines.

POST /query-runs        start a run (returns immediately; pipeline runs as a
                        background task — see services/query_runner)
GET  /query-runs        list runs (visibility-scoped)
GET  /query-runs/{id}   status, stage telemetry, artifact ids
POST /query-runs/{id}/resume   re-launch a failed run from its last good stage
GET  /query-runs/{id}/activity  per-agent tool-call streams for run inspection
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import Response, APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models._common import now_utc
from app.models.query import QueryRun
from app.schemas.common import OwnedOut
from app.services.query_runner import _Sinas, run_pipeline
from app.services import run_export
from app.services.visibility import visible_clause

router = APIRouter(prefix="/query-runs", tags=["query-runs"])

# Keep strong references so tasks aren't garbage-collected mid-run.
_tasks: set[asyncio.Task] = set()


def _launch(run_id: uuid.UUID) -> None:
    task = asyncio.create_task(run_pipeline(run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


class QueryRunIn(BaseModel):
    question: str = Field(min_length=8)
    # Identifier for the LOGICAL question — reruns share it (not unique).
    # A benchmark run is born as e.g. "benchmark-q16"; an external caller
    # may pass its own request id.
    reference: str | None = Field(default=None, max_length=200)
    # The benchmark's name for the question, e.g.
    # "Q16 — Dawn raids — personal data". Free text: the number and the topic
    # live in the benchmark question set, so nothing here can derive it.
    title: str | None = Field(default=None, max_length=300)
    # What differs in THIS version, e.g. "Round 3 — citation and coverage
    # fixes". Free text, and not inferred from the tags.
    change_note: str | None = Field(default=None, max_length=500)
    # Named groupings, e.g. ["round-3"]: a batch is born tagged.
    tags: list[str] = Field(default_factory=list, max_length=20)
    mode: str = Field(default="full", pattern="^(full|retrieval|synthesis)$")
    effort: str = Field(default="medium", pattern="^(low|medium|high)$")
    subqueries: list[str] | None = None  # skip decomposition when provided
    # required for mode="synthesis": the published result to answer from
    parent_result_id: uuid.UUID | None = None
    run_discovery: bool = False


class QueryRunOut(OwnedOut):
    question: str
    reference: str | None = None
    title: str | None = None
    change_note: str | None = None
    tags: list[str] = []
    mode: str = "full"
    effort: str = "medium"
    status: str
    subqueries: list[str] | None = None
    parent_result_id: uuid.UUID | None = None
    answer_id: uuid.UUID | None = None
    error: str | None = None
    telemetry: dict[str, Any] = {}
    # wall-clock bounds of the run itself — the only reliable elapsed time for
    # outcomes that write no closing stage telemetry (e.g. partial)
    started_at: datetime | None = None
    completed_at: datetime | None = None


@router.post(
    "",
    response_model=QueryRunOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def create_query_run(
    payload: QueryRunIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    if payload.mode == "synthesis" and payload.parent_result_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "mode=synthesis requires parent_result_id"
        )
    run = QueryRun(
        question=payload.question,
        reference=(payload.reference or None),
        title=((payload.title or "").strip()[:300] or None),
        change_note=((payload.change_note or "").strip()[:500] or None),
        tags=[t.strip()[:100] for t in payload.tags if t.strip()],
        mode=payload.mode,
        effort=payload.effort,
        subqueries=payload.subqueries,
        parent_result_id=payload.parent_result_id,
        run_discovery=payload.run_discovery,
        owner_id=caller.user_id,
        roles=list(caller.roles or []),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    _launch(run.id)
    return run


@router.get("", response_model=list[QueryRunOut])
async def list_query_runs(
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
    limit: int = 20,
    reference: str | None = None,
    tag: str | None = None,
):
    read_all = await caller.has_permission("sgr.results.read:all")
    stmt = (
        select(QueryRun)
        .where(visible_clause(QueryRun, caller, read_all=read_all))
        .order_by(QueryRun.created_at.desc())
        .limit(limit)
    )
    if reference:
        stmt = stmt.where(QueryRun.reference == reference)
    if tag:
        stmt = stmt.where(QueryRun.tags.contains([tag]))
    rows = (await session.execute(stmt)).scalars().all()
    return rows


async def _visible_run_or_404(
    run_id: uuid.UUID, session: AsyncSession, caller: CallerIdentity
) -> QueryRun:
    read_all = await caller.has_permission("sgr.results.read:all")
    row = (
        await session.execute(
            select(QueryRun)
            .where(QueryRun.id == run_id)
            .where(visible_clause(QueryRun, caller, read_all=read_all))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "query run not found")
    return row


class ExportIn(BaseModel):
    run_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


def _ndjson(docs: list[dict]) -> Response:
    body = "\n".join(json.dumps(d, ensure_ascii=False) for d in docs)
    return Response(
        content=body + ("\n" if body else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition":
                 'attachment; filename="sgr-review-export.jsonl"'},
    )


@router.post("/export")
async def export_selected_runs(
    payload: ExportIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """The named runs as self-contained sgr-review/1 documents, one JSON
    line each. Visibility applies: a run the caller cannot read is silently
    absent from the export rather than an error, so a mixed selection
    degrades the way the list view does."""
    read_all = await caller.has_permission("sgr.results.read:all")
    runs = await run_export.select_runs(
        session, run_ids=payload.run_ids,
        visible=visible_clause(QueryRun, caller, read_all=read_all))
    return _ndjson([await run_export.export_run(session, r) for r in runs])


@router.get("/export/by-tag")
async def export_runs_by_tag(
    tag: str,
    latest_per_reference: bool = True,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Every run carrying the tag — by default reduced to the newest
    completed run per reference, which is "one version per question" for a
    tagged round."""
    read_all = await caller.has_permission("sgr.results.read:all")
    runs = await run_export.select_runs(
        session, tag=tag, latest_per_reference=latest_per_reference,
        visible=visible_clause(QueryRun, caller, read_all=read_all))
    return _ndjson([await run_export.export_run(session, r) for r in runs])



@router.get("/{run_id}", response_model=QueryRunOut)
async def get_query_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    return await _visible_run_or_404(run_id, session, caller)


@router.post(
    "/{run_id}/resume",
    response_model=QueryRunOut,
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def resume_query_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    run = await _visible_run_or_404(run_id, session, caller)
    if run.status not in ("failed",):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"run is {run.status}; only failed runs resume"
        )
    run.status = "pending"
    run.error = None
    await session.commit()
    await session.refresh(run)
    _launch(run.id)
    return run


@router.post(
    "/{run_id}/cancel",
    response_model=QueryRunOut,
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def cancel_query_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Ask a running query run to stop, and stop paying for it.

    Records the request rather than killing the task: the pipeline observes it
    at its next checkpoint, then finalises through the same path as a partial
    outcome so the run reaches a terminal status with its sinas chats torn
    down. The response therefore still shows the in-flight status — the run
    flips to `cancelled` when the pipeline next looks, not here.

    Idempotent on an already-cancelling run; 409 on one that has finished,
    where there is nothing left to stop.
    """
    run = await _visible_run_or_404(run_id, session, caller)
    if run.status in ("published", "partial", "failed", "cancelled"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"run is {run.status}; only an unfinished run can be cancelled",
        )
    telemetry = dict(run.telemetry or {})
    entry = dict(telemetry.get("cancel") or {})
    entry.update(
        {
            "requested": True,
            "requested_at": entry.get("requested_at") or now_utc().isoformat(),
            "requested_by": str(caller.user_id),
        }
    )
    telemetry["cancel"] = entry
    run.telemetry = telemetry
    await session.commit()
    await session.refresh(run)
    return run


class AgentAction(BaseModel):
    name: str
    args: str = ""


class SearchActivityOut(BaseModel):
    subquery: str
    chat_id: str | None = None
    result_id: str | None = None
    actions: list[AgentAction] = []


class QueryRunActivityOut(BaseModel):
    searches: list[SearchActivityOut] = []
    synthesis: SearchActivityOut | None = None


_TOOL_PREFIXES = ("connector__sgr__api__", "call_agent_sgr__", "call_agent_")


def _short_name(raw: str) -> str:
    for p in _TOOL_PREFIXES:
        if raw.startswith(p):
            rest = raw[len(p):]
            return f"{rest} →" if p.startswith("call_agent") else rest
    return raw


def _actions_from_messages(messages: list[dict]) -> list[AgentAction]:
    out: list[AgentAction] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name") or ""
            if not name:
                continue
            args = fn.get("arguments") or ""
            if not isinstance(args, str):
                args = json.dumps(args)
            if len(args) > 120:
                args = args[:120] + "…"
            out.append(AgentAction(name=_short_name(name), args=args))
    return out


class QueryRunMetaIn(BaseModel):
    """Editable identity/grouping of an existing run. A field omitted is
    left alone; an explicit null clears the reference, title or change
    note; tags
    REPLACE the run's tags wholesale (read-modify-write is the client's
    loop)."""
    reference: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=300)
    change_note: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=20)


@router.patch(
    "/{run_id}",
    response_model=QueryRunOut,
    dependencies=[Depends(require_permission("sgr.results.write:own"))],
)
async def update_query_run_meta(
    run_id: uuid.UUID,
    payload: QueryRunMetaIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Stamp identity after the fact — a run done before its reference was
    known ("benchmark-q16") gets it here, from the UI or a script.

    This is the path a re-export of an earlier round goes through. Without
    `title` here the column was only reachable by writing to the database
    directly, which is how the first round-3 export was produced and is not
    something the product should require."""
    run = await _visible_run_or_404(run_id, session, caller)
    fields = payload.model_dump(exclude_unset=True)
    if "reference" in fields:
        run.reference = (fields["reference"] or "").strip()[:200] or None
    if "title" in fields:
        run.title = (fields["title"] or "").strip()[:300] or None
    if "change_note" in fields:
        run.change_note = (fields["change_note"] or "").strip()[:500] or None
    if fields.get("tags") is not None:
        run.tags = [t.strip()[:100] for t in fields["tags"] if t.strip()][:20]
    await session.commit()
    await session.refresh(run)
    return run


@router.get("/{run_id}/activity", response_model=QueryRunActivityOut)
async def get_query_run_activity(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Tool-call streams of the run's agents, read from their Sinas chats.

    Read-only inspection: safe to poll while a run is live. Chats that are
    unreachable simply contribute an empty action list."""
    run = await _visible_run_or_404(run_id, session, caller)
    sinas = _Sinas()
    result_by_subquery: dict[str, Any] = (
        (run.telemetry or {}).get("search", {}).get("results", {})
    )
    searches: list[SearchActivityOut] = []
    for subquery, meta in (run.searches or {}).items():
        chat_id = (meta or {}).get("chat_id")
        actions = await sinas.chat_messages(chat_id) if chat_id else []
        searches.append(
            SearchActivityOut(
                subquery=subquery,
                chat_id=chat_id,
                result_id=result_by_subquery.get(subquery),
                actions=_actions_from_messages(actions),
            )
        )
    synthesis = None
    if run.synthesis_chat_id:
        msgs = await sinas.chat_messages(run.synthesis_chat_id)
        synthesis = SearchActivityOut(
            subquery="synthesis",
            chat_id=run.synthesis_chat_id,
            actions=_actions_from_messages(msgs),
        )
    return QueryRunActivityOut(searches=searches, synthesis=synthesis)
