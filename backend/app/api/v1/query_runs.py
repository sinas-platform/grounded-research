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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models.query import QueryRun
from app.schemas.common import OwnedOut
from app.services.query_runner import _Sinas, run_pipeline
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
    mode: str = Field(default="full", pattern="^(full|retrieval|synthesis)$")
    effort: str = Field(default="medium", pattern="^(low|medium|high)$")
    subqueries: list[str] | None = None  # skip decomposition when provided
    # required for mode="synthesis": the published result to answer from
    parent_result_id: uuid.UUID | None = None
    run_discovery: bool = False


class QueryRunOut(OwnedOut):
    question: str
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
):
    read_all = await caller.has_permission("sgr.results.read:all")
    rows = (
        await session.execute(
            select(QueryRun)
            .where(visible_clause(QueryRun, caller, read_all=read_all))
            .order_by(QueryRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
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
