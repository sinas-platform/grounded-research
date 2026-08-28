"""Source obligations — a run-scoped ledger of documents the answer owes.

The gate names an unused source; until now that message lived for one
revision round. To land in the answer, one round had to win every stage in
sequence — extraction finds the passage, the reviser writes the claim, the
judge accepts it, the final sweep keeps it — and if any stage failed, the
run forgot the debt and the next round re-rolled every die. Measured on one
question re-run three times: the demanded judgment survived to the final
answer once.

The ledger makes the debt state instead of a message:

- recorded once, when the gate first names the document;
- fed to every revision round until discharged, not one;
- discharged exactly two ways, both recorded — the document is cited by a
  surviving claim (checked live against the claims table, so a claim
  deleted later REOPENS the debt), or the reviser waives it with a
  rationale it can only give after reading the document's passages;
- capped: after MAX_FEEDS failed attempts the system waives it itself,
  visibly, so a run still terminates.

State lives in query_run.telemetry (the cancel flag's precedent): no
migration, survives restarts, and the runner is the only writer per run.
"""

from __future__ import annotations

import functools
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db import AsyncSessionLocal
from app.models import AnswerClaim, ClaimEvidence, Document
from app.models.query import QueryRun

_KEY = "source_obligations"
MAX_FEEDS = 3
_log = logging.getLogger(__name__)


def _best_effort(neutral):
    """The ledger is bookkeeping: it must never fail the run it serves.

    A storage fault degrades the run to pre-ledger behavior (this round's
    gate reply still feeds revision) instead of failing it — logged, so a
    silent ledger is visible in the run's logs rather than invisible.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapped(*a, **k):
            try:
                return await fn(*a, **k)
            except Exception:  # noqa: BLE001
                _log.warning("obligation ledger %s failed", fn.__name__,
                             exc_info=True)
                return neutral() if callable(neutral) else neutral
        return wrapped
    return deco


async def _load(run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if run is None:  # unit tests and dry paths run gates without a row
            return {}
        return dict(((run.telemetry or {}).get(_KEY)) or {})


async def _store(run_id: uuid.UUID, entries: dict[str, dict[str, Any]]) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if run is None:
            return
        tel = dict(run.telemetry or {})
        tel[_KEY] = entries
        run.telemetry = tel
        flag_modified(run, "telemetry")
        await session.commit()


@_best_effort(None)
async def record(run_id: uuid.UUID, doc: str, note: str) -> None:
    """A document the answer owes. First note wins; recording is idempotent."""
    doc = (doc or "").strip()
    if not doc:
        return
    entries = await _load(run_id)
    if doc not in entries:
        entries[doc] = {"note": (note or "")[:400], "fed": 0, "waived": None}
        await _store(run_id, entries)


@_best_effort(None)
async def note_fed(run_id: uuid.UUID, docs: list[str]) -> None:
    entries = await _load(run_id)
    changed = False
    for doc in docs:
        if doc in entries:
            entries[doc]["fed"] = int(entries[doc].get("fed") or 0) + 1
            changed = True
    if changed:
        await _store(run_id, entries)


@_best_effort(None)
async def waive(run_id: uuid.UUID, doc: str, rationale: str,
                by: str = "reviser") -> None:
    entries = await _load(run_id)
    if doc in entries and not entries[doc].get("waived"):
        entries[doc]["waived"] = {"by": by, "rationale": (rationale or "")[:400]}
        await _store(run_id, entries)


@_best_effort(list)
async def unmet(run_id: uuid.UUID, answer_id: uuid.UUID) -> list[dict[str, Any]]:
    """Obligations neither waived nor satisfied, oldest first.

    Satisfaction is computed, never stored: an obligation is met while — and
    only while — a surviving claim cites the document. A claim deleted by a
    later stage silently reopens the debt, which is the point.
    """
    entries = await _load(run_id)
    live = {d: e for d, e in entries.items() if not e.get("waived")}
    if not live:
        return []
    async with AsyncSessionLocal() as session:
        cited = set(
            (
                await session.execute(
                    select(Document.filename)
                    .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
                    .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
                    .where(AnswerClaim.answer_id == answer_id)
                )
            ).scalars().all()
        )
    return [
        {"doc": d, "note": e.get("note") or "", "fed": int(e.get("fed") or 0)}
        for d, e in live.items() if d not in cited
    ]
