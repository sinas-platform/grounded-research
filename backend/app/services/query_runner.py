"""Server-supervised question pipeline (the query-side ingestion_runner).

The choreography of a question — retrieve, synthesize, validate, publish —
lives HERE, in code, with per-stage state checkpointed on the QueryRun row.
Agents are consulted only for judgment, via stateless one-shot invokes:

  retrieve    the retrieval-first engine (app/retrieval_first): schema-aware
              plan + deterministic channels, in-process
  synthesize  sgr/retrieval-planner-agent (argument plan, draft, revisions)
              and sgr/passage-extractor-agent (verbatim grounding extracts)
  verdicts    the stateless evidence-check fan-out (services/faithfulness),
              then sgr/answer-gate-agent judging the surviving answer

Every transition lands in QueryRun.telemetry. A failed run can be resumed:
completed stages short-circuit off the persisted state.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select

from app.services import claim_naming, obligations
from app.auth import CallerIdentity
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import AnswerClaim, ClaimEvidence, Document, DocumentClass, DocumentVersion, Result, ResultDocument
from app.models.query import QueryRun

MAX_VALIDATE_ROUNDS = 4
# A round that reduced the failed count earns extra rounds, up to this cap —
# converging runs finish instead of dying at an arbitrary budget.
HARD_VALIDATE_ROUNDS = 8
# After drops, the surviving answer must still ANSWER THE QUESTION — judged
# holistically by the answer-gate agent, with at most this many remediation
# cycles before the run ends partial. One cycle meant a single attempt and
# then surrender: the gate would name exactly what was missing and the run
# would go partial rather than go and find it.
# effort buys persistence: how many times the run may act on the gate's
# verdict before it settles for a partial. Never 1 — one cycle means a single
# attempt and then surrender, with the gate having named exactly what was
# missing.
EFFORT_GATE_CYCLES = {"low": 2, "medium": 3, "high": 5}
ANSWER_GATE_CYCLES = EFFORT_GATE_CYCLES["medium"]
MIN_CLAIMS = 6
# The synthesis playbook targets about 12 claims and says not to exceed 12.
# Remediation appends, and with several cycles it walked straight past that —
# one answer reached 29 claims. Additions stop here; the gate can still have
# claims dropped or rewritten when it objects.
MAX_CLAIMS = 14
# Hard per-run spend ceiling in USD, summed over the run's synthesis chat
# (which also carries remediation traffic — empirically where runaway spend
# lives; run 3d7f39d3 burned $23.81 there hunting unanchorable evidence).
# Checked on every supervision poll; tripping it fails the run loudly.
RUN_COST_CAP_USD = get_settings().sgr_run_cost_cap_usd

_log = __import__("logging").getLogger("sgr.query_runner")


class PartialOutcome(Exception):
    """A run that cannot deliver a fully validated answer for a SEMANTIC
    reason (budget ceiling, unanchorable coverage, stalled drafting) — as
    opposed to an infrastructure failure, which stays `failed` and is
    retryable. Terminates the run in status `partial` with a client-facing
    note over the stored retrieval; never silently substitutes for an
    answerable question (the trip points all sit AFTER validation has had
    its chances)."""

    def __init__(self, cause: str, explanation: str):
        self.cause = cause
        self.explanation = explanation
        super().__init__(f"{cause}: {explanation}")


class CancelledOutcome(Exception):
    """A run stopped because someone asked it to stop. Terminal, and not a
    failure: nothing went wrong, so it is neither retryable nor an error to
    report.

    Deliberately a plain Exception raised at checkpoints rather than
    `task.cancel()`. `asyncio.CancelledError` derives from BaseException, so
    it would sail past `run_pipeline`'s `except Exception` and leave the run
    stuck in its in-flight status with its sinas chats never torn down —
    cancelling by that route strands exactly what it means to clean up.
    """

    def __init__(self, requested_by: str | None = None):
        self.requested_by = requested_by
        super().__init__("cancelled on request")


async def _cancel_requested(run_id: uuid.UUID) -> bool:
    """Whether a cancel has been recorded for this run.

    Read from telemetry rather than a column: it needs no migration, it is
    already the state the stages read and write, and it survives a restart —
    which an in-process flag would not.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        entry = (run.telemetry or {}).get("cancel")
    return bool(isinstance(entry, dict) and entry.get("requested"))


async def _check_cancel(run_id: uuid.UUID) -> None:
    """Raise if a cancel is pending. Call between units of billable work.

    Placement is the whole design: a checkpoint only stops spend that has not
    happened yet, so these sit before each stage and inside the per-item
    loops, not merely at the top of the pipeline.
    """
    if await _cancel_requested(run_id):
        async with AsyncSessionLocal() as session:
            run = await session.get(QueryRun, run_id)
            entry = (run.telemetry or {}).get("cancel") or {}
        raise CancelledOutcome(requested_by=entry.get("requested_by"))


def _domain_article() -> str:
    """"a legal " / "an " — deployment says what kind of corpus this is."""
    d = get_settings().sgr_domain.strip()
    if not d:
        return "an "
    return f"an {d} " if d[0].lower() in "aeiou" else f"a {d} "


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


# Waits between invoke retries, in seconds. Deliberately long: the Anthropic
# SDK underneath already retries twice with sub-second backoff on anything
# >= 500, so a fast retry here would only repeat what it just exhausted. What
# gets through that and reaches us is an overload lasting longer than a couple
# of seconds, and these are sized for it. Two attempts, not more: an overload
# that survives twenty seconds is not going to yield to a third.
INVOKE_RETRY_WAITS = (5.0, 15.0)


def _is_transient(exc: Exception) -> bool:
    """Whether this invoke failure is worth waiting out.

    Only a 5xx from Sinas and a connection failure. A 5xx is the shape an
    upstream provider overload arrives in — Sinas surfaces it as a 500 — and a
    connection error never carries a judgment about the request.

    Everything else is ours. A 4xx means the call was wrong, and retrying it
    hides the defect while paying for it twice; 429 is deliberately not here,
    because the SDK below already honours `retry-after` and a rate limit we
    still hit after that wants a smaller run, not a slower one.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    # Connect-side only. An invoke is not idempotent — it starts model work
    # and opens a chat — so a failure is safe to repeat exactly when it is
    # certain the request never arrived. A refused connection and a timeout
    # while connecting are that. A read timeout is not: Sinas may have
    # accepted and be running it, and retrying would start the same work
    # twice while only the attempt whose response arrives gets its chat
    # recorded, so the run would pay for both and see one.
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


class _Sinas:
    """Minimal async Sinas client for chat supervision."""

    def __init__(self, run_id: uuid.UUID | None = None) -> None:
        s = get_settings()
        self.base = s.sinas_url
        self.headers = {"Authorization": f"Bearer {s.sinas_api_key}"}
        # Every invoke returns the chat Sinas opened for it, and its usage
        # ledger is keyed by that chat. Recording the id against the run is
        # the whole of the bookkeeping: a run's spend becomes a join, with no
        # change needed on the Sinas side.
        self.run_id = run_id

    async def chat_create(self, agent: str, title: str) -> str:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                f"{self.base}/agents/{agent}/chats",
                headers=self.headers,
                json={"title": title, "keep_alive": True, "job_timeout": 3600},
            )
            r.raise_for_status()
            return r.json()["id"]

    def send_detached(self, chat_id: str, content: str) -> None:
        """Fire the message; observe completion via the DB, never this socket."""

        async def _fire() -> None:
            try:
                async with httpx.AsyncClient(timeout=3600.0) as c:
                    await c.post(
                        f"{self.base}/chats/{chat_id}/messages",
                        headers=self.headers,
                        json={"content": content},
                    )
            except Exception:  # job continues server-side (keep_alive)
                pass

        asyncio.create_task(_fire())

    async def invoke(self, agent: str, message: str) -> str:
        """One agent call, retried past a transient upstream failure.

        Two runs died on the same day partway through validation, after the
        retrieval, the draft and most of the validation rounds were paid for:
        $2.69 between them, both on an Anthropic overload that Sinas surfaced
        as a 500. Nothing in this pipeline retried a network failure — the
        drafter and the gate each retry a malformed reply, which is a
        different thing — so a call that had already been attempted three
        times below ended the run.

        The overload arrives spelled two ways, `OverloadedError` and an
        `APIStatusError` carrying `overloaded_error`, which is why the attempt
        is recorded: the second cost an hour to recognise as the first.
        """
        last: Exception | None = None
        for attempt, wait in enumerate((*INVOKE_RETRY_WAITS, None)):
            try:
                async with httpx.AsyncClient(timeout=600.0) as c:
                    r = await c.post(
                        f"{self.base}/agents/{agent}/invoke",
                        headers=self.headers,
                        json={"message": message},
                    )
                    r.raise_for_status()
                    data = r.json()
                if attempt:
                    await _tele_invoke_retry(self.run_id, agent, attempt,
                                             last, recovered=True)
                await record_llm_call(self.run_id, data.get("chat_id"), agent)
                return data.get("reply", "") or ""
            except Exception as exc:  # noqa: BLE001
                if wait is None or not _is_transient(exc):
                    if attempt:
                        await _tele_invoke_retry(self.run_id, agent, attempt,
                                                 exc, recovered=False)
                    raise
                last = exc
                _log.warning("invoke %s failed (%s); retrying in %.0fs",
                             agent, type(exc).__name__, wait)
                await asyncio.sleep(wait)
                # Checked after the wait, not before it. Fifteen seconds is
                # long enough for an operator to cancel inside one, and
                # without this the loop would start and pay for another model
                # call on a run already cancelled. `_check_cancel` raises, so
                # the cancellation surfaces here rather than at whichever
                # later checkpoint the pipeline reached next.
                if self.run_id is not None:
                    await _check_cancel(self.run_id)
        raise AssertionError("unreachable")  # pragma: no cover

    async def chat_messages(self, chat_id: str) -> list[dict]:
        # 120s: supervision reads must tolerate a Sinas API busy with bulk
        # ingestion (17 Aug: 30s tripped ReadTimeouts at load average 10).
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.get(f"{self.base}/chats/{chat_id}", headers=self.headers)
            if r.status_code != 200:
                return []
            return r.json().get("messages") or []

    async def chat_delete(self, chat_id: str) -> None:
        """Best-effort: a failed delete never raises (teardown must not mask
        the error that triggered it)."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                await c.delete(f"{self.base}/chats/{chat_id}", headers=self.headers)
        except Exception:
            pass


async def _tele_invoke_retry(run_id, agent: str, attempts: int,
                             exc: Exception | None, *, recovered: bool) -> None:
    """Note that an invoke had to be retried, and how it ended.

    Best-effort, like the rest of the bookkeeping. Recorded because the two
    failures that motivated the retry cost an hour to diagnose: the same
    upstream overload reached the logs once as `OverloadedError` and once as
    an `APIStatusError` whose message was `overloaded_error`, and grepping for
    the first found nothing on the second. A run should be able to say it hit
    one without anybody reading Sinas's logs.
    """
    if run_id is None:
        return
    try:
        await _tele(run_id, "invoke_retries", **{f"{agent}_{_iso()}": {
            "attempts": attempts + 1,
            "recovered": recovered,
            "error": f"{type(exc).__name__}: {exc}"[:300] if exc else None,
        }})
    except Exception:  # noqa: BLE001
        _log.warning("could not record invoke retry", exc_info=True)


async def record_llm_call(run_id, chat_id, agent: str | None) -> None:
    """Note that a run made this call. Best-effort: bookkeeping must never be
    the thing that fails a run."""
    if not run_id or not chat_id:
        return
    try:
        from app.models.query import RunLLMCall

        async with AsyncSessionLocal() as session:
            session.add(RunLLMCall(run_id=run_id, chat_id=uuid.UUID(str(chat_id)),
                                   agent=(agent or "")[:200]))
            await session.commit()
    except Exception:  # noqa: BLE001
        pass


async def _run_cost_usd(run_id: uuid.UUID) -> float:
    """What this run has spent, over every call it made."""
    from app.models.query import RunLLMCall

    async with AsyncSessionLocal() as session:
        ids = [str(c) for c in (await session.execute(
            select(RunLLMCall.chat_id).where(RunLLMCall.run_id == run_id)
        )).scalars().all()]
    return await _chats_cost_usd(ids) if ids else 0.0


def _runner_caller(run: QueryRun) -> CallerIdentity:
    s = get_settings()
    return CallerIdentity(
        user_id=run.owner_id,
        roles=list(run.roles or []),
        is_admin=True,  # server-side pipeline acts with operator authority
        sinas_token=s.sinas_api_key,
    )


async def _mark(run_id: uuid.UUID, **fields: Any) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        for k, v in fields.items():
            setattr(run, k, v)
        await session.commit()


async def _tele(run_id: uuid.UUID, stage: str, **detail: Any) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        t = dict(run.telemetry or {})
        entry = dict(t.get(stage) or {})
        entry.update(detail)
        t[stage] = entry
        run.telemetry = t
        await session.commit()


@asynccontextmanager
async def _timed(run_id: uuid.UUID, stage: str) -> AsyncIterator[None]:
    """Record when a stage starts, ends and how long it took.

    Stages recorded their own timings inconsistently — `draft` carried
    started/completed, `retrieval` only completed, `extract` neither — so a
    run's telemetry could not say where its wall clock went. On a measured
    52-minute run the logged model calls accounted for 932s; the remaining
    ~2,800s sat between them, and nothing persisted said which stage was
    holding it. `elapsed_s` is written even when the stage raises, because a
    stage that dies slowly is exactly the one worth timing.
    """
    t0 = time.monotonic()
    await _tele(run_id, stage, started=_iso())
    try:
        yield
    finally:
        await _tele(
            run_id, stage, completed=_iso(), elapsed_s=round(time.monotonic() - t0, 1)
        )


def _chat_ids_for_cleanup(telemetry: dict | None, searches: dict | None) -> list[str]:
    """Chats found in the state the retired chat-based pipeline recorded:
    telemetry entries carrying a chat_id (discovery and its stages) and those
    runs' per-sub-query search chats. Order-stable, deduped.

    On a retrieval-first run this returns nothing, and that is the current
    truth rather than an oversight: measured across the six most recent runs,
    45-94 chats were opened and this found 0 of them every time. The
    authoritative list is `RunLLMCall`, which `_run_cost_usd` already joins.

    Do NOT "fix" this by pointing it at RunLLMCall on its own. Archiving every
    chat a run opened buys nothing — see `_teardown_chats` for why archiving
    stops no work — while hiding the paper trail we want kept in Sinas. Wire
    the real abort here when it lands, and scope it to what can still cost
    something: the detached agents, not the synchronous invokes that finished
    before the call returned.
    """
    ids: list[str] = []
    for entry in (telemetry or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("chat_id"), str):
            ids.append(entry["chat_id"])
    for meta in (searches or {}).values():
        if isinstance(meta, dict) and isinstance(meta.get("chat_id"), str):
            ids.append(meta["chat_id"])
    return list(dict.fromkeys(ids))


async def _teardown_chats(sinas: _Sinas, chat_ids: list[str]) -> None:
    """Archive each chat named, best-effort: one failure never blocks the rest.

    It does NOT stop anything. Sinas's `DELETE /chats/{id}` is a soft-delete —
    it sets `archived = True` and returns; messages and llm_usage rows are
    untouched, and no generation is interrupted. An earlier version of this
    docstring claimed it stopped agents working "for nobody"; it never did.

    Two consequences worth keeping in view:
      * Cancellation saves money only through the checkpoints in the pipeline
        that stop the NEXT call being made. Nothing here contributes to that.
      * The one genuine fire-and-forget — the detached relationship-proposal
        agent — cannot be stopped by any call we currently have. A real abort
        is being added on the Sinas side; this is the seam to wire it into.

    Note also that `llm_usage` deliberately carries no foreign keys, so
    archiving never threatens the cost ledger. The paper trail survives.
    """
    for chat_id in chat_ids:
        try:
            await sinas.chat_delete(chat_id)
        except Exception:
            _log.warning("teardown of chat %s failed", chat_id)


# ── stages ──────────────────────────────────────────────────────────────────


async def _stage_merge(run_id: uuid.UUID, children: list[str]) -> uuid.UUID:
    from app.services.result_filter import merge_results

    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if run.parent_result_id:
            return run.parent_result_id
        caller = _runner_caller(run)
        question = run.question
        await session.commit()

    await _mark(run_id, status="merging")
    child_ids = [uuid.UUID(c) for c in children]
    async with AsyncSessionLocal() as session:
        if len(child_ids) == 1:
            parent_id = child_ids[0]
        else:
            parent = Result(
                query=question,
                invoked_skill_names=["query-run"],
                owner_id=caller.user_id,
                roles=caller.roles or [],
            )
            session.add(parent)
            await session.commit()
            await session.refresh(parent)
            parent_id = parent.id
            summary = await merge_results(session, caller, parent_id, child_ids)
            await _tele(run_id, "merge", **{k: v for k, v in summary.items() if k != "parent_result_id"})
        # publish via the API layer's logic (coverage metric) is HTTP-only;
        # publishing directly here keeps it in-process:
        row = await session.get(Result, parent_id)
        if row.status != "published":
            row.status = "published"
            row.published_at = _now()
            await session.commit()
    await _mark(run_id, parent_result_id=parent_id)
    return parent_id


async def _stage_discovery(run_id: uuid.UUID, sinas: _Sinas) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if not run.run_discovery or (run.telemetry or {}).get("discovery"):
            return
        parent = run.parent_result_id
    chat = await sinas.chat_create("sgr/relationship-discovery-agent", "[query-run] discovery")
    sinas.send_detached(chat, f"Surface relationship proposals for the documents of result {parent}. Write proposals only.")
    await _tele(run_id, "discovery", fired=_iso(), chat_id=chat)


async def _manifest_rows(parent_id: uuid.UUID) -> list[dict]:
    """Per result document, in rank order: what the deployment's config
    derives about it — class, annotation values (issuing body, authority
    tier in a legal deployment; SGR only renders what the config
    declares), retrieval reason, summary. One structure for every surface
    that judges or chooses among documents, so the gate and the reviser see
    the same document the planner saw. Grounding surfaces (drafter,
    extractor, validator spans) never consume this: they get raw text.

    The gate had been judging "is a plainly more authoritative source
    unused?" from a filename and 220 characters of summary — the authority
    annotations existed and reached only the planner."""
    from app.models import AnnotationDefinition
    from app.services.annotations import annotations_for_documents

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Document.id,
                    Document.filename,
                    DocumentClass.name,
                    ResultDocument.reason,
                    Document.summary,
                )
                .join(Document, Document.id == ResultDocument.document_id)
                .outerjoin(DocumentClass, DocumentClass.id == Document.document_class_id)
                .where(ResultDocument.result_id == parent_id)
                .order_by(ResultDocument.rank)
            )
        ).all()

        definitions = list(
            (await session.execute(select(AnnotationDefinition))).scalars()
        )
        per_doc: dict = {}
        if definitions and rows:
            per_doc = await annotations_for_documents(
                session, [r[0] for r in rows], definitions
            )

        # Deployment-defined property values (dates, numbers, whatever the
        # config declares). A gate judging claims against each other needs
        # to see that one cited document predates the instrument another
        # describes — a 2021 commentary on a proposal was cited for
        # deadlines the enacted regulation, in the same answer, contradicts.
        from app.models import DocumentClassProperty, PropertyValue

        props_by_doc: dict = {}
        if rows:
            prop_rows = (await session.execute(
                select(PropertyValue.document_id, DocumentClassProperty.name,
                       PropertyValue.value)
                .join(DocumentClassProperty,
                      DocumentClassProperty.id == PropertyValue.property_id)
                .where(PropertyValue.document_id.in_([r[0] for r in rows]))
            )).all()
            for did, pname, pval in prop_rows:
                if pval is not None:
                    props_by_doc.setdefault(did, []).append((pname, pval))

        result = await session.get(Result, parent_id)
        briefing_by_doc = {
            b.get("document_id"): b
            for b in ((result.filter or {}).get("briefing") or [])
        }

    def _fmt(value) -> str:
        # Reducer outputs wrap entities as {"id": ..., "name": ...} (single
        # values additionally under a "value" key); the name is what the
        # drafting agent needs.
        if isinstance(value, dict):
            if set(value) == {"value"}:
                return _fmt(value["value"])
            if "name" in value:
                return str(value["name"])
            return " ".join(f"{k}={_fmt(v)}" for k, v in value.items())
        return str(value)

    out = []
    for did, fn, cls, reason, summary in rows:
        values = (per_doc.get(did) or {}).get("values") or {}
        ann = "; ".join(
            f"{name}: {_fmt(v)}" for name, v in values.items() if v is not None
        )
        props = "; ".join(
            f"{n}: {_fmt(v)}"[:60] for n, v in (props_by_doc.get(did) or [])[:6]
        )
        out.append({
            "document_id": did, "filename": fn, "class": cls or "",
            "annotations": ann, "properties": props, "reason": (reason or ""),
            "summary": (summary or ""),
            "briefing": briefing_by_doc.get(str(did)),
        })
    return out


def _manifest_line(r: dict) -> str:
    return (f"- {r['filename']} | {r['class'] or '-'} | "
            f"{r['annotations'] or '-'} | {r.get('properties') or '-'} | "
            f"{r['reason'][:120]} | {r['summary'][:200]}")


async def _doc_manifest(parent_id: uuid.UUID) -> str:
    lines = []
    for r in await _manifest_rows(parent_id):
        lines.append(_manifest_line(r))
        brief = r.get("briefing")
        if brief:
            props = brief.get("properties")
            if props:
                lines.append(f"    properties: {json.dumps(props, ensure_ascii=False)[:400]}")
            toc = brief.get("toc")
            if toc:
                lines.append(f"    toc: {str(toc)[:600]}")
    return "\n".join(lines)


_sinas_usage_engine = None


async def _chats_cost_usd(chat_ids: list[str]) -> float:
    """Spend across these Sinas chats, USD, from llm_usage.

    Sinas shares the Postgres server with SGR (different database), so this
    is one cross-database read. Rates are per million tokens. The Anthropic
    figures are the published rate card; the Gemini ones are calibrated
    against this deployment's own ingestion bill, which the published tiers
    reproduce to within a few percent. Gemini used to fall through to the
    Sonnet branch — a 60x over-count on the agents that do most of the work.

    Fails open (0.0): the cap must never be the thing that kills an otherwise
    healthy run on a transient error.
    """
    global _sinas_usage_engine
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings

    if not chat_ids:
        return 0.0
    if _sinas_usage_engine is None:
        url = get_settings().sgr_database_url
        _sinas_usage_engine = create_async_engine(
            url[: url.rfind("/")] + "/sinas", pool_size=2)
    try:
        async with _sinas_usage_engine.connect() as conn:
            row = await conn.execute(
                __import__("sqlalchemy").text("""
                    SELECT coalesce(sum(
                      CASE
                        WHEN model ILIKE '%flash-lite%' THEN
                          prompt_tokens * 0.10 + completion_tokens * 0.40
                        WHEN model ILIKE '%gemini%' THEN
                          prompt_tokens * 0.30 + completion_tokens * 2.50
                        WHEN model ILIKE '%haiku%' THEN
                          (prompt_tokens - cache_read_tokens - cache_write_tokens) * 1.0
                          + cache_write_tokens * 1.25 + cache_read_tokens * 0.10
                          + completion_tokens * 5.0
                        WHEN model ILIKE '%opus%' THEN
                          (prompt_tokens - cache_read_tokens - cache_write_tokens) * 5.0
                          + cache_write_tokens * 6.25 + cache_read_tokens * 0.50
                          + completion_tokens * 25.0
                        ELSE
                          -- sonnet-5 tier (console price table, 24 Aug 2026)
                          (prompt_tokens - cache_read_tokens - cache_write_tokens) * 2.0
                          + cache_write_tokens * 2.50 + cache_read_tokens * 0.20
                          + completion_tokens * 10.0
                      END) / 1e6, 0)
                    FROM llm_usage
                    WHERE chat_id = ANY(CAST(:cids AS uuid[]))
                      AND error IS NULL"""),
                {"cids": chat_ids})
            return float(row.scalar() or 0.0)
    except Exception:  # noqa: BLE001 — fail open by design
        return 0.0


DRAFT_MODE = get_settings().sgr_draft_mode


# Upper bound, in characters, on one unit of numbered text handed to the
# passage extractor. Named and documented in #95; see that comment for its
# provenance, which is unknown.
EXTRACT_DOC_CHAR_CAP = 140_000

# Upper bound on extraction calls per claim. Chunking trades a silent loss of
# a document's tail for calls that cost money, and 26 documents in service
# need ten chunks or more, one of them 27 — without a bound, a single claim
# anchored on one of those would make 27 calls. Four covers every document
# needing four chunks or fewer, which is 1,344 of the 1,632 over the cap; past
# that the tail is still unread, and `rounds_capped` below says when.
EXTRACT_MAX_ROUNDS = 4


def _numbered_lines(content: str) -> list[str]:
    """Every line prefixed with its 1-based number.

    Numbering happens once, over the whole document, before any splitting.
    That is what keeps a line number absolute: chunk boundaries only ever
    slice this list, so line 4,100 carries the prefix `4100:` whichever
    chunk it lands in. `_verify_passage` resolves a claimed range by reading
    those prefixes, so numbering that drifted per chunk would make the
    anti-fabrication check reject good passages.
    """
    return [f"{i + 1}: {line}" for i, line in enumerate(content.splitlines())]


def _span_chars(numbered: list[str], line_from: int, line_to: int) -> int:
    """Length of the joined text for an inclusive 1-based line range."""
    if line_to < line_from:
        return 0
    body = sum(len(numbered[i]) for i in range(line_from - 1, line_to))
    return body + (line_to - line_from)  # the newlines between them


def _toc_starts(toc, total_lines: int) -> list[int]:
    """Section start lines from a document's table of contents, as split
    candidates.

    Deliberately reads `line` only and ignores `line_to`. TOC ranges nest —
    `_close_ranges` ends an entry where the next same-or-higher level starts,
    so a level-1 entry spans its level-2 children — and treating ranges as
    content would emit the same lines once per level. Start lines carry the
    same information for this purpose and cannot double-count: they are just
    the places the document says a new section begins.
    """
    entries = toc if isinstance(toc, list) else (toc or {}).get("entries") or []
    starts = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            n = int(e.get("line"))
        except (TypeError, ValueError):
            continue
        if 1 < n <= total_lines:  # line 1 is the implicit first boundary
            starts.add(n)
    return sorted(starts)


def _line_windows(numbered: list[str], line_from: int, line_to: int,
                  cap: int) -> list[tuple[int, int]]:
    """Split one over-cap span on line boundaries, never mid-line.

    Used for a section that exceeds the cap by itself and for documents whose
    TOC yielded no usable start lines. A window boundary can fall mid-argument
    where a section boundary would not, which is the cost of having no
    structure to follow; it is still the whole document rather than its head.
    """
    out: list[tuple[int, int]] = []
    start = line_from
    used = 0
    for n in range(line_from, line_to + 1):
        one = len(numbered[n - 1])
        if one > cap:
            # A single line longer than the whole budget. Close whatever is
            # open and report the line as dropped: emitting it would blow the
            # cap, and emitting part of it would be the fragment #95 removed.
            if n > start:
                out.append((start, n - 1))
            start, used = n + 1, 0
            continue
        if used and used + 1 + one > cap:
            out.append((start, n - 1))
            start, used = n, one
        else:
            used = used + (1 if used else 0) + one
    if start <= line_to and used:
        out.append((start, line_to))
    return out


def _chunk_numbered(content: str, toc, cap: int) -> tuple[list[dict], dict | None]:
    """Split a document into cap-sized chunks on section boundaries.

    Returns the chunks and, only in the pathological case below, a record of
    what could not be represented at all.

    A document that fits returns exactly one chunk holding the same string the
    uncapped path produced, so the ~28,500 documents under the cap take the
    path they took before this existed.

    Over the cap, sections are packed greedily up to the cap rather than sent
    one per call: the corpus averages 85 sections a document and reaches 400,
    and a call per section would trade a truncation problem for a cost one.
    """
    numbered = _numbered_lines(content)
    total = len(numbered)
    if total == 0:
        return [], None
    joined = "\n".join(numbered)
    if len(joined) <= cap:
        return [{"text": joined, "line_from": 1, "line_to": total,
                 "strategy": "whole"}], None

    starts = _toc_starts(toc, total)
    strategy = "toc" if starts else "lines"

    # Raw segments between consecutive section starts, with any segment that
    # exceeds the cap on its own broken into line windows.
    bounds = [1, *starts, total + 1]
    segments: list[tuple[int, int]] = []
    dropped_lines = 0
    for a, b in zip(bounds, bounds[1:], strict=False):
        lo, hi = a, b - 1
        if hi < lo:
            continue
        if _span_chars(numbered, lo, hi) <= cap:
            segments.append((lo, hi))
        else:
            windows = _line_windows(numbered, lo, hi, cap)
            dropped_lines += (hi - lo + 1) - sum(y - x + 1 for x, y in windows)
            segments.extend(windows)

    # Pack consecutive segments up to the cap.
    chunks: list[dict] = []
    cur_from: int | None = None
    cur_to = 0
    cur_len = 0
    for lo, hi in segments:
        seg = _span_chars(numbered, lo, hi)
        # Only merge segments that actually abut. A dropped over-cap line
        # leaves a hole, and a chunk is emitted as the slice from its first
        # line to its last: merging across the hole would put the dropped
        # line back and blow the cap it was dropped for.
        adjacent = cur_from is not None and lo == cur_to + 1
        if cur_from is not None and (not adjacent or cur_len + 1 + seg > cap):
            chunks.append({"line_from": cur_from, "line_to": cur_to})
            cur_from, cur_to, cur_len = lo, hi, seg
        elif cur_from is None:
            cur_from, cur_to, cur_len = lo, hi, seg
        else:
            cur_to, cur_len = hi, cur_len + 1 + seg
    if cur_from is not None:
        chunks.append({"line_from": cur_from, "line_to": cur_to})

    out = [
        {
            "text": "\n".join(numbered[c["line_from"] - 1: c["line_to"]]),
            "line_from": c["line_from"],
            "line_to": c["line_to"],
            "strategy": strategy,
        }
        for c in chunks
    ]
    record = None
    if dropped_lines:
        kept = sum(len(c["text"]) for c in out)
        record = {
            "numbered_chars": len(joined),
            "cap": cap,
            "dropped_chars": len(joined) - kept,
            "dropped_lines": dropped_lines,
        }
    return out, record

# Retained from #95 for its tests and as the single-string capping
# reference; production extraction now chunks via _chunk_numbered.
def _number_and_cap(content: str, cap: int) -> tuple[str, dict | None]:
    """Number every line, then cut at the last complete line that still fits.

    Returns the numbered text and, when it had to be cut, a record of what was
    lost.

    Cutting on a line boundary rather than mid-string is the point. A slice
    through a line leaves a fragment still carrying its line number, which the
    model cannot tell from a complete line: it can then quote the fragment and
    the quote verifies, because the fragment is genuinely what that line
    contains as far as anything downstream can see. The document simply ends,
    with no marker, in the middle of a sentence.

    Nothing here raises the cap or splits the document. The caller loses
    slightly more text than before, which is the honest direction: what it
    loses, it now knows about.
    """
    lines = content.splitlines()
    numbered = "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines))
    if len(numbered) <= cap:
        return numbered, None

    kept = numbered[:cap]
    boundary = kept.rfind("\n")
    # A first line longer than the cap leaves nothing whole to keep. Sending a
    # fragment would be the failure this function exists to remove, so send
    # nothing and let the record say the whole document was dropped.
    kept = kept[:boundary] if boundary > 0 else ""
    kept_lines = kept.count("\n") + 1 if kept else 0
    return kept, {
        "numbered_chars": len(numbered),
        "cap": cap,
        "dropped_chars": len(numbered) - len(kept),
        "dropped_lines": len(lines) - kept_lines,
    }


async def _fetch_numbered(
    filenames: list[str], cap_chars: int = EXTRACT_DOC_CHAR_CAP
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Numbered content per filename, split into cap-sized chunks.

    Line-numbered so extraction quotes carry verifiable line refs, and split
    on section boundaries rather than cut at the cap. A document over the cap
    used to lose its tail before the extractor saw any of it, so an answer
    that reached for a secondary source may have done so because a
    document's relevant passage was never visible.

    Returns chunks per filename, and one record per document that still could
    not be fully represented — which after this change means only a document
    carrying a single line longer than the whole budget.
    """
    out: dict[str, list[dict]] = {}
    truncated: list[dict] = []
    async with AsyncSessionLocal() as session:
        for fn in filenames:
            row = (
                await session.execute(
                    select(DocumentVersion.content_md, Document.toc)
                    .join(Document, Document.current_version_id == DocumentVersion.id)
                    .where(Document.filename == fn)
                )
            ).first()
            if not row or not row[0]:
                continue
            chunks, cut = _chunk_numbered(row[0], row[1], cap_chars)
            if not chunks:
                continue
            out[fn] = chunks
            if cut is not None:
                truncated.append({"filename": fn, **cut})
    return out, truncated


# Characters that carry a rendering and nothing else. A model copying text
# correctly still types a straight apostrophe where the source has a
# typographic one, and drops the soft hyphens that PDF extraction leaves
# inside hyphenated words. Comparing those literally rejected the quote as
# fabricated, which is the one thing it demonstrably was not: every
# character that says something was identical.
#
# What is folded is only what has no role but rendering, and every entry is
# a substitution except one. A substitution keeps the boundary between two
# tokens; a deletion removes it, and can therefore merge two words into a
# third that is in neither text. The one deletion carries that weight.
#
# Two kinds of character stay out of the map. First, anything that means
# something in another notation, however it is drawn:
#   U+2212 is a minus sign, and a passage carrying a formula carries it as
#     an operator rather than as a dash.
#   U+2032 and U+2033 are primes: derivatives, minutes, locants.
#   U+200C and U+200D are orthographic joiners, and deleting one changes
#     the word.
# Second, anything with no case behind it. U+200B and U+FEFF are as
# harmless as the soft hyphen, and are out anyway: neither rescued a
# passage when measured, and a deletion nobody has watched matter is a
# claim this code cannot make good on. Either can return with evidence.
#
# Deliberately NOT unicodedata NFKC. It folds none of the marks below (it
# leaves every quote, dash and soft hyphen exactly as it found them), and
# the classes it does fold include superscript digits, which is one more
# way for two different footnote references to read alike.
_QUOTE_MARKS = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
    0x00AB: '"', 0x00BB: '"',
}
_DASHES = {c: "-" for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015)}
# The only character deleted rather than replaced: PDF extraction leaves it
# inside a hyphenated word, nothing draws it, and no copy reproduces it.
_SOFT_HYPHEN = {0x00AD: None}
_RENDERING_VARIANTS = {**_QUOTE_MARKS, **_DASHES, **_SOFT_HYPHEN}


def _canonical(text: str) -> str:
    """Text reduced to what a verbatim quote has to preserve.

    Case, the run-length of whitespace, and the rendering of quotes, dashes
    and invisible characters all vary between a source and a faithful copy
    of it. Nothing here removes, adds or reorders a word, a digit or a
    punctuation mark that separates words, so text that is not in the source
    still does not match text that is.
    """
    folded = (text or "").translate(_RENDERING_VARIANTS)
    return re.sub(r"\s+", " ", folded).strip().lower()


def _numbered_pairs(numbered: str) -> list[tuple[int, str]]:
    """The numbered block as (line number, text) pairs, unnumbered lines
    dropped. Shared by the verifier and the locator so the two can never
    disagree about what a line is."""
    out = []
    for line in numbered.splitlines():
        num, _, rest = line.partition(": ")
        try:
            out.append((int(num), rest))
        except ValueError:
            continue
    return out


def _locate_passage(numbered: str, line_from: int, line_to: int, quoted: str,
                    back: int = 2, fwd: int = 2) -> tuple[int, int] | None:
    """Where the quote actually sits, or None if it cannot be narrowed.

    The verifier widens the reported range before checking containment, so a
    quote the extractor placed a line or two off still verifies — and the
    reported coordinates were then persisted as the evidence span. A citation
    could point at lines that do not contain the text it cites, and anything
    reading the span back would slice the wrong part of the document.

    That was already true of the forward slack before the range became
    symmetric; widening both ends doubles the exposure and puts it at the end
    a reader looks at first. So the span is corrected rather than the slack
    withdrawn: the tolerance is what lets a faithful quote through, and the
    coordinates are what a reader needs to be true.

    Narrowest window wins. Scanning starts at each candidate line and extends
    only as far as it must, so a quote that fits in one line is recorded as
    one line rather than as the range the model guessed.

    Pure: text in, a pair of line numbers out.
    """
    full = _canonical(quoted)
    if len(full) < 20:
        return None
    rows = [(n, t) for n, t in _numbered_pairs(numbered)
            if line_from - back <= n <= line_to + fwd]

    def window(want: str) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        for i in range(len(rows)):
            acc: list[str] = []
            for j in range(i, len(rows)):
                acc.append(rows[j][1])
                if want in _canonical(" ".join(acc)):
                    # Narrowest wins, earliest breaking the tie. Taking the
                    # first window that matches would return the earliest
                    # start instead, and a quote sitting on one line would be
                    # recorded as the several lines that happen to precede it.
                    if best is None or (j - i) < (best[1] - best[0]):
                        best = (i, j)
                    break
        return best

    # The whole quote first. Verification matches on the first 200 characters
    # and up to 2,000 are stored, so locating on the prefix alone would end the
    # span before the text it is meant to cover — a citation shorter than the
    # passage it cites, which is this function's own defect reappearing at the
    # other end.
    #
    # The prefix is the fallback, not the rule: it is what verification
    # actually guaranteed, so anything the verifier accepted stays locatable
    # and the span never falls back to coordinates already called approximate.
    best = window(full) or window(full[:200])
    return (rows[best[0]][0], rows[best[1]][0]) if best else None


def _verify_passage(numbered: str, line_from: int, line_to: int, quoted: str,
                    back: int = 2, fwd: int = 2) -> bool:
    """Deterministic anti-hallucination: the quoted text must actually occur
    in the claimed line range (containment, normalized by _canonical).

    The range is widened by a couple of lines at both ends. The extractor
    reports where it believes the quote sits, and that estimate is off by a
    line or two in either direction; nothing about the error is directional.
    It used to be widened forward only — `line_from <= n <= line_to + 2` —
    which arrived as a passenger in the commit that built extract drafting and
    was never argued for. `_anchors_for` assumes the opposite three lines
    away, taking `lines[0] - 3` when it builds the reviser's windows, so the
    codebase already treats text before the matched line as part of it.

    `back` is a parameter so the caller can re-run a passage under the old
    asymmetric rule and record which ones only the symmetry recovered. That is
    the measurement the change was made without: rejected passages were never
    persisted, so the size of the loss could only be estimated from a sample
    of five.

    Widening cannot admit a fabrication. The quote still has to occur
    verbatim, in the chunk the model was shown; two more lines of real
    document is more text to match against, not weaker matching.
    """
    want = _canonical(quoted)
    if len(want) < 20:
        return False
    span_lines = [t for n, t in _numbered_pairs(numbered)
                  if line_from - back <= n <= line_to + fwd]
    return want[:200] in _canonical(" ".join(span_lines))


def _reject_reason(shown: dict, fn: str, lf: int | None, lt: int | None,
                   quoted: str) -> str | None:
    """Why this proposed passage was not kept, or None if it was.

    The check exists to stop a fabricated quote and it rejects 1,043 of 3,933
    proposed passages across the stored runs — 27% — with nothing recorded but
    the two counts. A gap that size is worth a reason each: a quote the model
    invented and a quote it copied faithfully while misreporting its line
    number are different failures, and only one of them is the check working.

    Pure: dicts and strings in, a string or None out.
    """
    if fn not in shown:
        return "document not among those shown"
    if lf is None or lt is None:
        return "line range not readable"
    if len(_canonical(quoted)) < 20:
        return "quote under the length floor"
    if not _verify_passage(shown[fn]["text"], lf, lt, quoted):
        return "not found in the claimed lines"
    return None


async def _content_anchors(
    point: str, filenames: list[str], take: int = 4
) -> list[tuple[str, list[int]]]:
    """Documents whose FULL TEXT contains the point's distinctive terms,
    with the matching line numbers.

    The summary matcher (_relevant_docs) sees only manifest text; a gate
    point like "the ten-day time limit for both routes" matched nothing
    there while two retrieved documents quoted the statute verbatim — the
    reviser was told the part was missing five times and never shown the
    documents that carry it. Literal token scan, no tsvector config, no
    stemming: numbers, case references and names survive across languages,
    which is what makes a cross-language corpus searchable at all.
    """
    toks = re.findall(r"[\w()./§-]{2,}", point or "")
    seen: set[str] = set()
    special, plain = [], []
    for t in toks:
        low = t.lower().strip(".,;:()")
        if not low or low in seen:
            continue
        seen.add(low)
        if any(ch.isdigit() for ch in t):
            special.append(low)
        elif t[0].isupper() and len(low) > 3:
            special.append(low)
        elif len(low) > 5:
            plain.append(low)
    terms = (special + plain)[:10]
    if not terms or not filenames:
        return []
    scored: list[tuple[float, str, list[int]]] = []
    async with AsyncSessionLocal() as session:
        for fn in filenames[:60]:
            content = (await session.execute(
                select(DocumentVersion.content_md)
                .join(Document, Document.current_version_id == DocumentVersion.id)
                .where(Document.filename == fn))).scalar()
            if not content:
                continue
            low = content.lower()
            hits = [t for t in terms if t in low]
            # digit-bearing terms are the strongest cross-language signal
            score = sum(3.0 if any(ch.isdigit() for ch in t) else 1.0
                        for t in hits)
            if score <= 0:
                continue
            lines = []
            if hits:
                best = max(hits, key=lambda t: (any(c.isdigit() for c in t), len(t)))
                pos, ln = 0, []
                for m in re.finditer(re.escape(best), low):
                    ln.append(low.count("\n", 0, m.start()) + 1)
                    if len(ln) >= 3:
                        break
                lines = ln
            scored.append((score, fn, lines))
    scored.sort(key=lambda s: -s[0])
    return [(fn, lines) for _, fn, lines in scored[:take]]


def _relevant_docs(point: str, corpus_rows: list[dict], take: int = 4) -> list[str]:
    """Documents whose own text most specifically matches this point.

    Rank orders the whole result for the question; it does not order
    documents for one claim. Michelin sat at rank 18 and Servier at rank 26 —
    both retrieved, neither read, because the planner names anchors from
    summaries and extraction only reads what it names. Terms are weighted by
    rarity within the candidate set, so a word appearing in two documents
    outweighs one appearing in forty.
    """
    terms = {w.lower().strip(".,;:()'\"") for w in (point or "").split() if len(w) > 4}
    if not terms:
        return []
    # Class and annotations are matchable text too: "the Court of Justice
    # judgment on X" should pull the document whose issuing body IS the
    # Court of Justice, not only one whose summary happens to say so.
    hay = {r["filename"]: " ".join(
               (r["filename"], r.get("summary") or "", r.get("class") or "",
                r.get("annotations") or "")).lower()
           for r in corpus_rows if r.get("filename")}
    df = {t: sum(1 for h in hay.values() if t in h) for t in terms}
    scored = []
    for pos, r in enumerate(corpus_rows):
        fn = r.get("filename")
        if not fn:
            continue
        score = sum(1.0 / df[t] for t in terms if df.get(t) and t in hay[fn])
        if score:
            scored.append((round(score, 4), -pos, fn))
    scored.sort(reverse=True)
    return [fn for _, _, fn in scored[:take]]


def _chunk_telemetry(out: list[dict]) -> dict:
    """What chunking cost this run, deduplicated by filename.

    A document anchoring several claims is fetched and chunked once per claim,
    and counting it once per claim would report a corpus property as a run
    one. `extra_extraction_calls` is the honest cost line: calls made beyond
    the one each claim would have made before chunking existed.
    """
    by_file: dict[str, dict] = {}
    extra = 0
    capped = 0
    for r in out:
        rows = r.get("chunked") or []
        for row in rows:
            by_file[row["filename"]] = row
            if row["chunks"] > row["read"]:
                capped += 1
        if rows:
            extra += max(row["read"] for row in rows) - 1
    if not by_file:
        return {}
    return {
        "documents_chunked": len(by_file),
        "chunks_total": sum(r["chunks"] for r in by_file.values()),
        "chunked_documents": sorted(
            by_file.values(), key=lambda r: -r["chunks"]
        )[:20],
        "extra_extraction_calls": extra,
        "rounds_capped": capped,
    }


def _shown_for_round(
    docs: dict[str, list[dict]], k: int, owed: str | None = None
) -> dict[str, dict]:
    """The chunk of each anchored document to show in round k.

    Round k shows chunk k, so a document that fits in a single chunk is
    visible in round 0 and absent from every round after it, while a long one
    stays for all of them. Measured on a real cycle: an owed ECtHR ruling of
    18,706 characters was shown once, beside chunk 0 of a 2.8 MB decision that
    was shown four times.

    The owed document is what the cycle exists to settle, so it is pinned into
    every round, cycling its own chunks when it has several. Order follows
    `docs`, which puts it at the head of the blob, and it is placed in the same
    pass rather than appended so it keeps that position.

    Pure: dicts in, dict out.
    """
    shown: dict[str, dict] = {}
    for fn, chunks in docs.items():
        if k < len(chunks):
            shown[fn] = chunks[k]
        elif fn == owed and chunks:
            shown[fn] = chunks[k % len(chunks)]
    return shown


async def _extract_passages(
    sinas: _Sinas, plan_claims: list[dict], run_id: uuid.UUID | None = None
) -> list[dict]:
    """Inverted split, reading half: the cheap model pulls verbatim passages
    (with line refs) per planned claim from its anchor documents. Every quote
    is then verified against the actual lines — fabricated quotes are dropped,
    so the drafter can only ever see text that exists."""
    sem = asyncio.Semaphore(4)
    started = time.monotonic()
    started_iso = _iso()
    # Decided once, on the way in, and reused for both writes. Computing it
    # again at the end would count the key this one just wrote and number the
    # cycle twice.
    cycle = (await _next_cycle_key(run_id, "extract", "cycle")
             if run_id is not None else "cycle_1")
    if run_id is not None:
        # Written before the work so an extraction that raises still leaves a
        # cycle behind. The final write replaces the cycle with a superset.
        #
        # `started` stays a stage-level keyword beside it. Every stage records
        # when it started, when it ended and how long it took — a contract
        # written after a 52-minute run whose wall clock could not be
        # attributed to any stage — and it is checked by reading this call's
        # keywords, so it has to be one. The stage-level copy answers where
        # the time went; the copy inside the cycle answers what each
        # extraction did.
        await _tele(run_id, "extract", started=started_iso,
                    **{cycle: {"started": started_iso}})

    async def one(c: dict) -> dict:
        anchors = [str(a) for a in (c.get("anchors") or [])[:8]]
        owed = str(c.get("owed") or "") or None
        docs, truncated = await _fetch_numbered(anchors)
        if not docs:
            return {"n": c.get("n"), "passages": [], "proposed": 0, "read": 0,
                    "truncated": truncated, "chunked": []}

        # One round per chunk depth: round k shows chunk k of every anchor that
        # has one. A document that fits is one chunk, so a claim anchored only
        # on documents under the cap makes exactly the one call it made before.
        depth = max(len(ch) for ch in docs.values())
        rounds = min(depth, EXTRACT_MAX_ROUNDS)
        chunked = [
            {"filename": fn, "chunks": len(ch), "strategy": ch[0]["strategy"],
             "read": min(len(ch), rounds)}
            for fn, ch in docs.items() if len(ch) > 1
        ]

        good: list[dict] = []
        rejected: list[dict] = []
        recovered = 0
        corrected = 0
        seen_spans: set[tuple[str, int, int]] = set()
        owed_empty = False
        proposed_total = 0
        last_error: str | None = None
        for k in range(rounds):
            shown = _shown_for_round(docs, k, owed)
            if not shown:
                continue
            # The header states the range and the whole, so the model can tell
            # a section from a document. Without it a chunk reads as the whole
            # text and "the document does not address X" becomes a conclusion
            # about the corpus rather than about the part it was handed.
            doc_blob = "\n\n".join(
                (f"=== {fn} ===\n{ch['text']}" if ch["strategy"] == "whole"
                 else f"=== {fn} (lines {ch['line_from']}-{ch['line_to']}) ===\n"
                      f"{ch['text']}")
                for fn, ch in shown.items())
            partial = any(ch["strategy"] != "whole" for ch in shown.values())
            async with sem:
                try:
                    reply = await sinas.invoke(
                        "sgr/passage-extractor-agent",
                        "From the numbered documents below, extract the passages "
                        "that bear on: " + str(c.get("establishes") or "") + "\n"
                        + (("Focus: " + str(c.get("hint")) + "\n")
                           if c.get("hint") else "")
                        # Naming it is what makes pinning it useful: the model
                        # can only weigh a document it knows is the point of
                        # the call. The refusal is offered in the same breath
                        # and given somewhere to go in the reply, because an
                        # instruction to produce a passage with no expressible
                        # alternative is an instruction to strain for one, and
                        # a strained quote from the real document passes
                        # verification exactly as a faithful one does.
                        + (f"One document below is owed: {owed}. Settling that "
                           "debt is what this call is for, so a passage from it "
                           "is what is wanted. If it holds nothing bearing on "
                           "the objective, take nothing from it and set "
                           '"owed_has_nothing": true — that is a complete '
                           "answer here, and a stretched quote is not.\n"
                           if owed and owed in shown else "")
                        + ("Some documents are shown as a numbered section of a "
                           "longer text, marked with the line range in its header. "
                           "Judge only what is in front of you: silence in a "
                           "section is not silence in the document.\n"
                           if partial else "")
                        + 'Reply ONLY JSON: {"passages": [{"filename": "...", '
                        '"line_from": <int>, "line_to": <int>, "text": "<verbatim '
                        'quote>"}]'
                        + (', "owed_has_nothing": true|false'
                           if owed and owed in shown else "")
                        + '} — max 4 passages, each 2-25 lines, text EXACTLY '
                        "as printed (without the line-number prefixes).\n\n"
                        + doc_blob,
                    )
                    cleaned = reply.strip().strip("`").removeprefix("json").strip()
                    data = json.loads(
                        cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])
                except CancelledOutcome:
                    # A cancel is not a transport failure. It reaches here
                    # because the invoke checks for one after each retry wait,
                    # and the catch below would read it as "this round failed"
                    # and start the next chunk — another paid call on a run the
                    # operator already stopped. It has to travel out.
                    raise
                except Exception as exc:  # noqa: BLE001
                    # A transport failure is not "this document says nothing".
                    # Swallowing it produced runs that ended `partial` — which
                    # here means the corpus cannot answer the question — when the
                    # real cause was an HTTP 429. Record it so the caller can
                    # tell an empty document from an unreachable one.
                    last_error = str(exc)[:200]
                    continue
            if data.get("owed_has_nothing") is True:
                owed_empty = True
            proposed = (data.get("passages") or [])[:4]
            proposed_total += len(proposed)
            for p in proposed:
                fn = str(p.get("filename") or "")
                try:
                    lf, lt = int(p.get("line_from")), int(p.get("line_to"))
                except (TypeError, ValueError):
                    lf = lt = None
                text = str(p.get("text") or "")
                # Verified against the chunk the model was actually shown, not
                # the whole document: a quote it could not have seen is not
                # grounded, however true it happens to be.
                why = _reject_reason(shown, fn, lf, lt, text)
                if why is not None:
                    rejected.append({"filename": fn, "line_from": lf,
                                     "line_to": lt, "reason": why,
                                     "quote": text[:200]})
                    continue
                # Kept, but would the old forward-only rule have kept it? The
                # answer is the whole point of making the range symmetric, and
                # it can only be asked at the moment the passage is judged.
                if not _verify_passage(shown[fn]["text"], lf, lt, text, back=0):
                    recovered += 1
                # Recorded where the quote is, not where it was said to be.
                # The tolerance is what lets a faithful quote through; the
                # coordinates are what a reader needs to be true, and they
                # become the persisted evidence span. Falls back to the
                # reported pair only if the window cannot be narrowed, which
                # verification already says should not happen.
                found = _locate_passage(shown[fn]["text"], lf, lt, text)
                if found and found != (lf, lt):
                    corrected += 1
                alf, alt = found or (lf, lt)
                # The owed document is shown every round, so the same span can
                # come back more than once. Deduplicated on the corrected span
                # — the one persisted — so two reports that locate to the same
                # text are one passage, not two.
                if (fn, alf, alt) in seen_spans:
                    continue
                seen_spans.add((fn, alf, alt))
                good.append({"filename": fn, "line_from": alf, "line_to": alt,
                             "text": text[:2000]})

        # Only a failure with nothing to show for it is an error: one bad round
        # out of several still leaves the claim grounded.
        if last_error and not good:
            return {"n": c.get("n"), "passages": [], "proposed": proposed_total,
                    "read": len(docs), "error": last_error,
                    "rejected": rejected, "recovered_by_symmetry": recovered,
                    "spans_corrected": corrected,
                    "truncated": truncated, "chunked": chunked}
        return {"n": c.get("n"), "establishes": c.get("establishes"),
                "passages": good, "proposed": proposed_total,
                "rejected": rejected, "recovered_by_symmetry": recovered,
                "spans_corrected": corrected,
                "owed": owed, "owed_empty": owed_empty,
                "read": len(docs), "truncated": truncated, "chunked": chunked}

    out = list(await asyncio.gather(*(one(c) for c in plan_claims)))
    errors = [r.get("error") for r in out if r.get("error")]
    if errors and not any(r.get("passages") for r in out):
        # every extraction failed and none succeeded: infrastructure, not
        # a judgment about the sources
        raise RuntimeError(
            f"passage extraction failed for all {len(out)} claims — "
            f"first error: {errors[0]}")
    # Deduplicated by filename: a document anchoring several claims is fetched
    # and cut once per claim, and counting it once per claim would report a
    # corpus problem as a run problem. `documents_read` above is deliberately
    # left as it was, summed per claim, so this is not a ratio of that.
    cut_by_file: dict[str, dict] = {}
    for r in out:
        for t in r.get("truncated") or []:
            cut_by_file.setdefault(t["filename"], t)

    if run_id is not None:
        # The verified/proposed gap is the point of this stage: quotes that
        # did not match the source text never reach the drafter. A gap that
        # stops being small means the extractor is drifting.
        #
        # documents_truncated and characters_dropped are the second thing this
        # stage can be wrong about and could not previously report. A document
        # over the cap is cut before the model sees any of it, so a thin answer
        # can be a corpus that never arrived rather than a corpus with nothing
        # to say. These say which.
        #
        # All of it now sits under `cycle_N`. A run extracts once for the
        # draft and again for every revision cycle that carries a point, and
        # this dict was written whole each time, so `_tele` replaced it and
        # only the last extraction survived. Every number here was affected,
        # not one of them: reading a run's `documents_read` gave whichever
        # extraction happened to go last, and comparing two runs could compare
        # a draft extraction against a revision one without anything saying
        # so. Same shape as round_N and revision_N, and answer_regress already
        # reads that family by prefix.
        await _tele(
            run_id, "extract",
            completed=_iso(),
            elapsed_s=round(time.monotonic() - started, 1),
            **{cycle: {
                "started": started_iso,
                "claims": len(out),
                "documents_read": sum(r.get("read", 0) for r in out),
                "passages_proposed": sum(r.get("proposed", 0) for r in out),
                "passages_verified": sum(
                    len(r.get("passages") or []) for r in out),
                # What the owed documents did. `owed_declared_empty` is
                # the extractor using the refusal rather than straining, and
                # is the only signal that separates "the document does not
                # bear on this" from "nothing was found", which the ledger
                # cannot otherwise tell apart.
                "owed_points": sum(1 for r in out if r.get("owed")),
                "owed_with_passage": sum(
                    1 for r in out if r.get("owed")
                    and any(p["filename"] == r["owed"]
                            for p in (r.get("passages") or []))),
                # Only when the refusal stood. A multi-chunk owed document is
                # shown once per round, so one round can declare its chunk
                # empty while another returns a verified passage from a
                # different one. Counting the declaration on its own would put
                # the same point in both this and `owed_with_passage`, which
                # is the distinction the field exists to draw.
                "owed_declared_empty": sum(
                    1 for r in out if r.get("owed_empty") and not any(
                        p["filename"] == r.get("owed")
                        for p in (r.get("passages") or []))),
                # The 27% that never reach the drafter, with a reason
                # each. Capped because this is a JSONB column a person reads,
                # not a log: the count is exact, the sample is for looking at.
                "passages_rejected": sum(
                    len(r.get("rejected") or []) for r in out),
                "rejected_sample": [
                    x for r in out for x in (r.get("rejected") or [])][:20],
                # Passages the symmetric range kept that the old forward-only
                # one would have thrown away — counted, not estimated.
                "recovered_by_symmetry": sum(
                    r.get("recovered_by_symmetry", 0) for r in out),
                # Passages whose recorded span differs from the one the
                # extractor reported: how often the coordinates needed
                # correcting to point at the text they cite.
                "spans_corrected": sum(r.get("spans_corrected", 0) for r in out),
                "extraction_errors": len(errors),
                "documents_truncated": len(cut_by_file),
                "characters_dropped": sum(
                    t["dropped_chars"] for t in cut_by_file.values()),
                "lines_dropped": sum(
                    t["dropped_lines"] for t in cut_by_file.values()),
                "truncated_documents": sorted(
                    cut_by_file.values(), key=lambda t: -t["dropped_chars"]),
                **_chunk_telemetry(out),
                "completed": _iso(),
                "elapsed_s": round(time.monotonic() - started, 1),
            }})
    return out


async def _synthesis_playbook() -> str:
    """The deployment's drafting rules, as house style for the drafter.

    These were reaching the model through the agent-chat synthesis path,
    which fetched them as a skill. Chatless drafting replaced that path and
    consulted nothing, so every rule in the playbook silently stopped
    applying — sentence length, precedent attribution, the claim target. The
    file was installed and correct the whole time; nothing read it.

    Rules only, never facts: the playbook says how to write, and the verified
    passages remain the only thing a claim may assert.
    """
    from app.models import Playbook

    async with AsyncSessionLocal() as session:
        content = (await session.execute(
            select(Playbook.content).where(Playbook.kind == "synthesis")
            .limit(1))).scalar_one_or_none()
    if not content:
        return ""
    return ("\n\nHOUSE RULES for drafting (how to write, not what is true — "
            "only the passages decide that):\n" + content.strip() + "\n")


def _claims_json(reply: str) -> dict:
    """The drafter's reply as JSON, or JSONDecodeError naming what broke."""
    cleaned = (reply or "").strip().strip("`").removeprefix("json").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise json.JSONDecodeError("no JSON object in reply", cleaned or "", 0)
    return json.loads(cleaned[start:end + 1])


async def _draft_from_extracts(
    run_id: uuid.UUID, answer_id: uuid.UUID, sinas: _Sinas,
    question: str, extracts: list[dict], append: bool = False,
    cap: int | None = None,
) -> int:
    """Inverted split, writing half: one tool-less Sonnet call drafts all
    claims from the verified extracts; the runner persists claims and
    evidence rows itself. No chat loop, no nudges, no wedge surface."""
    # Grounding is on raw source text only. The plan's "establishes" sentence
    # is written from the manifest — summaries, classes, annotations — which
    # are themselves interpretation produced at ingestion, unverified and
    # never checked against anything. Handing it to the drafter let it assert
    # what a summary said: one answer attributed an Opinion to the Advocate
    # General who wrote it, correctly, from a summary rather than from any
    # passage, so the attribution was true and uncheckable. The plan decides
    # what to READ; only what was read may be asserted.
    blocks = []
    for i, e in enumerate(extracts, start=1):
        if not e.get("passages"):
            continue
        ps = "\n".join(
            f"  [{p['filename']} lines {p['line_from']}-{p['line_to']}]\n"
            f"  {p['text']}" for p in e["passages"])
        blocks.append(f"PASSAGE GROUP {i}\n{ps}")
    if not blocks:
        return 0
    reply = await sinas.invoke(
        "sgr/retrieval-planner-agent",
        f"Draft the claims of {_domain_article()}answer from the VERIFIED "
        "PASSAGES below "
        "— these passages are the ONLY thing you know. Every claim must be "
        "supported entirely by the passages you cite for it. Do not name a "
        "court, an Advocate General, a case number or a date that no passage "
        "shows. Skip a passage group that establishes nothing usable. The "
        "final claim states the overall conclusion.\n\n"
        "For each claim also give a RATIONALE: ONE short sentence, at most "
        "20 words — which part of the question this answers and why this "
        "source settles it. Never restate the claim; the reader has just "
        "read it. Where two passage groups spoke to the same point, name "
        "the one you relied on. It is reasoning, not evidence — nothing in "
        "it may assert anything the passages do not show. Name a source the "
        "way the claim names it — deciding body and case reference — never "
        "by its filename.\n\n"
        'Reply ONLY JSON: {"claims": [{"text": "<claim>", "type": '
        '"legal_principle|factual|procedural|conclusion", '
        '"rationale": "<why this claim rests on this source>", "evidence": '
        '[{"filename": "...", "line_from": <int>, "line_to": <int>}]}]}\n\n'
        + await _synthesis_playbook()
        + "\nQUESTION:\n" + question + "\n\n" + "\n\n".join(blocks),
    )
    # One retry on a malformed reply. Drafting is the last call in a run that
    # has already paid for retrieval, planning and extraction, and a single
    # unescaped quote inside a claim threw all of it away. The retry is the
    # cheap half of the work, and a second failure still raises: a run that
    # cannot draft must say so, not publish nothing.
    try:
        data = _claims_json(reply)
    except json.JSONDecodeError as exc:
        await _tele(run_id, "draft", draft_reparse=str(exc)[:200])
        reply = await sinas.invoke(
            "sgr/retrieval-planner-agent",
            "Your previous reply was not valid JSON: " + str(exc)[:200]
            + ". Send the same claims again as strictly valid JSON. Escape "
            'every quotation mark inside a string as \\", and use no line '
            "breaks inside a string.\n\nPREVIOUS REPLY:\n" + reply[:60000])
        data = _claims_json(reply)
    claims = data.get("claims") or []
    written = 0
    async with AsyncSessionLocal() as session:
        start_seq = 1
        if append:
            start_seq = ((await session.execute(
                select(func.max(AnswerClaim.sequence))
                .where(AnswerClaim.answer_id == answer_id)
            )).scalar() or 0) + 1
        for i, c in enumerate(claims[:(cap or 14)], start=start_seq):
            text_ = str(c.get("text") or "").strip()
            if not text_:
                continue
            row = AnswerClaim(answer_id=answer_id, sequence=i,
                              claim_text=text_,
                              rationale=(str(c.get("rationale") or "").strip()
                                         or None),
                              claim_type=str(c.get("type") or "legal_principle")[:50])
            session.add(row)
            await session.flush()
            for ev_ in (c.get("evidence") or [])[:4]:
                doc = (await session.execute(
                    select(Document).where(Document.filename == str(ev_.get("filename") or ""))
                )).scalars().first()
                if doc is None:
                    continue
                session.add(ClaimEvidence(
                    claim_id=row.id, document_id=doc.id,
                    document_version_id=doc.current_version_id,
                    span={"line_from": ev_.get("line_from"),
                          "line_to": ev_.get("line_to"),
                          "char_from": None, "char_to": None, "note": None},
                    validated=False))
            written += 1
        await session.commit()
    await _tele(run_id, "draft", extract_mode=True, claims=written)
    return written


async def _argument_plan(
    sinas: _Sinas, run_id: uuid.UUID, question: str, manifest: str
) -> tuple[str, list[dict]]:
    """Split drafting: a strong tool-less model designs the ARGUMENT (which
    claims, anchored where) from the briefing manifest alone; the drafter
    then executes claim by claim. Judgment is expensive and small; reading
    and writing are cheap and large — price each accordingly (17 Aug:
    memory-padding lived entirely in the deciding, never the writing).
    Fail-open: any planning failure returns "" and drafting proceeds
    exactly as before."""
    try:
        reply = await sinas.invoke(
            "sgr/retrieval-planner-agent",
            "Design the argument for answering the question below, using ONLY "
            "the documents listed. Reply ONLY JSON:\n"
            '{"claims": [{"n": 1, "establishes": "<one sentence: what this '
            'claim must establish>", "anchors": ["<filename>", ...], '
            '"hint": "<which part of the anchor documents to read, from their '
            'TOCs>"}]}\n'
            "Rules: 6-12 claims; every claim anchored to at least one listed "
            "document; never anchor to anything not listed; the final claim "
            "must state the overall conclusion. If the documents cannot "
            "support a part of the question, plan NO claim for it — the gap "
            "will be reported honestly downstream.\n\n"
            "QUESTION:\n" + question + "\n\nDOCUMENTS:\n" + manifest[:60000],
        )
        cleaned = reply.strip().strip("`").removeprefix("json").strip()
        data = json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])
        claims = data.get("claims") or []
        if not claims:
            # Both halves, like every other exit. Returning the bare string
            # here made the caller's `_plan_text, plan_claims = await ...`
            # raise ValueError on a planner that replied `{"claims": []}`, so
            # the run was recorded as failed. The caller already handles an
            # empty plan on the next line, and treats it as a judgment about
            # the corpus rather than a crash; that branch was unreachable
            # through this path.
            return "", []
        lines = []
        for c in claims[:12]:
            anchors = ", ".join(str(a) for a in (c.get("anchors") or [])[:4])
            hint = str(c.get("hint") or "").strip()
            lines.append(
                f"{c.get('n')}. {str(c.get('establishes') or '').strip()}"
                f"\n   anchors: {anchors}" + (f"\n   read: {hint}" if hint else "")
            )
        await _tele(run_id, "draft", argument_plan=[
            {"n": c.get("n"), "establishes": c.get("establishes"),
             "anchors": c.get("anchors")} for c in claims[:12]])
        return (
            "ARGUMENT PLAN (realize these claims in order; read each claim's "
            "anchor documents, write the claim, bind its evidence; do not add "
            "claims beyond the plan):\n" + "\n".join(lines) + "\n\n"
        ), claims[:12]
    except CancelledOutcome:
        # Not a planning failure. The catch below turns anything that is not a
        # JSON problem into RuntimeError("argument planning failed"), which
        # `run_pipeline` records as `failed` with that as the run's error text.
        # A run the operator stopped would then read, to anyone looking at it
        # later, as a run whose planner broke. It has to travel out to the
        # `except CancelledOutcome` in `run_pipeline`, which is reachable from
        # here: this is called from `_stage_synthesize`, inside that try.
        raise
    except json.JSONDecodeError:
        # the planner answered, just not in the shape asked for
        _log.warning("argument plan unparseable for run %s", run_id)
        await _tele(run_id, "draft", plan_unparseable=True)
        return "", []
    except Exception as exc:  # noqa: BLE001
        # Reaching the planner failed. That is infrastructure, and returning
        # an empty plan turns it into "the corpus supports no claims" — a
        # semantic verdict the run then reports as its outcome.
        raise RuntimeError(f"argument planning failed: {str(exc)[:200]}") from exc


async def _stage_synthesize(run_id: uuid.UUID, sinas: _Sinas) -> uuid.UUID:
    from app.models import Answer

    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        question, parent_id = run.question, run.parent_result_id
        answer_id = run.answer_id
        if answer_id and (run.telemetry or {}).get("draft", {}).get("completed"):
            return answer_id
        caller = _runner_caller(run)

    await _mark(run_id, status="synthesizing")
    if not answer_id:
        async with AsyncSessionLocal() as session:
            parent = await session.get(Result, parent_id)
            row = Answer(
                source_result_id=parent_id,
                question=question,
                owner_id=parent.owner_id,
                roles=list(parent.roles or []),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            answer_id = row.id
        await _mark(run_id, answer_id=answer_id)
    _ = caller  # ownership derives from the parent result above

    manifest = await _doc_manifest(parent_id)
    # The manifest is navigation: it decides which documents are worth
    # reading. Nothing it says may become a claim — summaries, classes and
    # annotations are interpretation produced at ingestion and verified
    # against nothing. Only extracted, verbatim-checked passages ground an
    # answer.
    _plan_text, plan_claims = await _argument_plan(sinas, run_id, question, manifest)
    if not plan_claims:
        # the planner ran and produced nothing usable: that IS a judgment
        # about the sources, unlike a transport failure, which raises above
        raise PartialOutcome(
            "no_progress", "no argument plan could be formed from the result")

    # Widen each planned claim's anchors with the documents whose own text
    # matches it. The planner picks from summaries and tops out at the few it
    # names, so an authority that is retrieved but not summarised in those
    # terms is never opened.
    corpus_rows = (await _manifest_rows(parent_id))[:60]
    for c in plan_claims:
        named = [str(a) for a in (c.get("anchors") or [])]
        extra = [d for d in _relevant_docs(str(c.get("establishes") or ""),
                                           corpus_rows, take=4)
                 if d not in named]
        c["anchors"] = named + extra

    await _tele(run_id, "draft", started=_iso())
    extracts = await _extract_passages(sinas, plan_claims, run_id)
    n = await _draft_from_extracts(run_id, answer_id, sinas, question, extracts)
    if not n:
        raise PartialOutcome(
            "no_progress", "no passage supported a claim well enough to draft")
    await _tele(run_id, "draft", completed=_iso(), claims=n,
                **({"thin": True, "minimum": MIN_CLAIMS} if n < MIN_CLAIMS else {}))
    if n < MIN_CLAIMS:
        # Thin is not fatal: validation and the gate decide whether the answer
        # stands, and revision can still grow it. Failing here killed runs
        # that had drafted four sound claims.
        _log.info("run %s drafted %s claims (below %s) — continuing to validation",
                  run_id, n, MIN_CLAIMS)
    return answer_id


async def _record_gate_cycle(
    run_id: uuid.UUID, *, parts: list[dict],
    reparse: str | None = None, unparseable: str | None = None,
    unaccounted: list[str] | None = None,
) -> None:
    """One write per gate cycle, covering every key a cycle can set.

    The gate runs once per validation cycle and telemetry merges by key, so
    a key written by one cycle and not by the next is read as belonging to
    the next. That produced two defects in mirror image: a decomposition
    surviving an unreadable verdict, and an unreadable verdict surviving a
    decomposition. Both came from the same shape, two exit paths each
    writing the subset of keys it knew about, and a third would have
    followed the moment a fourth key appeared. So every path records the
    whole outcome through here, and a cycle inherits nothing.

    The third key arrived and this is it: `gate_reparse` is carried in a
    local from where the repair happens to the one write at the end, rather
    than written where it occurs. A cycle that needed a repair and then
    parsed records both facts together, and a cycle that needed none clears
    it. Writing it where it happens would leave it behind exactly as the two
    defects above did. The cost is that a transport failure inside the repair
    loses the marker, which buys one write to keep correct instead of two to
    keep in step.

    A cycle can be followed by another because `_pre_publish_sweep` returns
    False after feeding a repair cycle and the caller re-enters the validate
    loop, which is what makes the staleness reachable rather than theoretical.

    `accounted` on a part is answer-scoped, not part-scoped, and the same
    value is written onto every part of the cycle. The gate names unused
    sources as `"<filename>: <why>"` with no reference to a part, so nothing
    says which limb of the question a named source bears on; a per-part claim
    would be invented here rather than read. It is written onto the parts
    anyway because that is where it has to be read against `covered`: a row
    saying `covered: true, accounted: false` is the contradiction this exists
    to make legible, and `gate_unaccounted` beside it names the documents.

    A key that did not happen is written null rather than omitted, and that
    has a consequence worth stating: once this ships,
    `telemetry->'validate' ? 'gate_unparseable'` is true for every run,
    because the key is always present. These are truthiness fields. The one
    consumer in the tree reads them that way already.
    """
    await _tele(run_id, "validate", gate_parts=parts,
                gate_reparse=reparse, gate_unparseable=unparseable,
                gate_unaccounted=unaccounted)


def _gate_json(reply: str) -> dict:
    """The gate's verdict as an object, or ValueError naming what broke.

    Shaped after `_claims_json`, with one difference the caller depends on:
    a reply that parses to something other than an object is rejected here
    rather than downstream, so both ways a verdict can be unusable arrive
    as one exception type and get one repair.
    """
    cleaned = (reply or "").strip().strip("`").removeprefix("json").strip()
    # A whole reply that is valid JSON is read as it stands. The slice below
    # is for a verdict wrapped in prose, and on a reply that is a JSON array
    # it would quietly lift the first element out and return that: an object,
    # so the isinstance check never fires, and a reply that was never a
    # verdict becomes one.
    try:
        whole = json.loads(cleaned)
    except ValueError:
        pass
    else:
        if not isinstance(whole, dict):
            raise ValueError("gate verdict was not an object")
        return whole
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the gate's reply")
    data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("gate verdict was not an object")
    return data
async def _split_question(sinas: _Sinas, question: str) -> list[str]:
    """The distinct things the question asks, or [] if that cannot be read.

    Its own call, on the question alone. The gate used to do this inside its
    verdict, where the question is a fraction of a percent of a prompt whose
    bulk is the claims and the working set, and the instruction to split
    arrives after all of it. So the split moved with the answer: over the
    cycles of one run the same question was read as four parts and then
    three; in another as two, three, two, three; in a third a fifth part
    appeared that the question does not ask but the draft happened to
    contain, and was then marked covered. A part that stops being listed
    stops being checked, and nothing says so.

    Splitting needs none of that. What a question asks is a property of the
    question; judging coverage is what needs the answer, so only the second
    half keeps the large prompt.

    One repair, as the drafter does with its own reply. An empty return is
    the caller's signal to fall back to splitting inside the verdict, which
    is the behaviour this replaces: a run must not fail because an
    improvement could not be applied.
    """
    prompt = (
        "QUESTION:\n" + question
        + "\n\nSplit the question into the distinct things it asks. A question "
        "asking what the conditions are, whether a regulation applies and "
        "whether a step is mandatory asks three things, not one. Split on "
        "what is asked, not on how the sentence is punctuated: one sentence "
        "can ask two things, and two sentences can ask one. Do not answer "
        "any of them, and do not add anything the question does not ask."
        '\n\nReply ONLY JSON: {"parts": ["<one thing the question asks>", ...]}'
    )
    reply = await sinas.invoke("sgr/answer-gate-agent", prompt)
    for attempt in (0, 1):
        try:
            cleaned = (reply or "").strip().strip("`").removeprefix("json").strip()
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("no JSON object in reply")
            raw = json.loads(cleaned[start:end + 1]).get("parts")
            if not isinstance(raw, list) or not raw:
                raise ValueError("no parts in reply")
            # Only strings. str() on a dict is a non-empty string, so without
            # this an object in the list becomes a question part that binds
            # every later cycle. Dropping it silently would be worse: a lost
            # part is the defect this whole change exists to stop, so an
            # element that is not a part makes the reply unusable and the
            # repair below runs.
            got = [x.strip() for x in raw if isinstance(x, str) and x.strip()]
            if len(got) != len(raw):
                raise ValueError("a part was not a non-empty string")
            return got[:8]
        except Exception as exc:  # noqa: BLE001
            if attempt:
                _log.warning("question split unreadable twice: %s", exc)
                return []
            reply = await sinas.invoke(
                "sgr/answer-gate-agent",
                "Your previous reply was not valid JSON: " + str(exc)[:200]
                + ". Send the same split again as strictly valid JSON, the "
                "object alone.\n\nPREVIOUS REPLY:\n" + (reply or "")[:8000])
    return []


async def _question_parts(
    sinas: _Sinas, run_id: uuid.UUID, question: str
) -> list[str]:
    """The run's decomposition, split once and reused by every later cycle.

    Computed on the first gate cycle rather than as a pipeline stage: all
    three call sites already carry the run id, so no signature moves, and a
    resumed run reads what the first one wrote. State lives in telemetry,
    the obligation ledger's precedent: no migration, survives a restart,
    one writer per run.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        stored = ((run.telemetry or {}).get("validate") or {}).get("question_parts")
    if isinstance(stored, list) and stored:
        return [str(x) for x in stored]
    parts = await _split_question(sinas, question)
    if parts:
        await _tele(run_id, "validate", question_parts=parts)
    return parts


async def _gate_answer(
    sinas: _Sinas, run_question: str, answer_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[bool, str, list[str], list[str], list[str]]:
    """Judge whether the surviving claims still answer the question, and
    surface quality findings. Generic by construction: no claim-type
    vocabulary, no counting floors — a stateless judge, per part of the
    question.

    Returns (publishable, missing, issues, correctness, points).
    `publishable` and `correctness` are the hard gate; `issues` are
    best-effort remediation targets that must never block publication on
    their own; `points` are the things revision must be given passages for —
    one entry per part of the question the claims do not answer, then each
    stronger source the gate named.

    Everything the gate finds is returned. It used to leave some of it in a
    module-level dict, which a later edit deleted the declaration of — so
    every verdict raised NameError inside the try below and came back out of
    the except as "treated as pass". The gate stopped gating and nothing
    said so. A value that callers need is a return value.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(AnswerClaim.sequence, AnswerClaim.claim_text)
                .where(AnswerClaim.answer_id == answer_id)
                .order_by(AnswerClaim.sequence)
            )
        ).all()
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
        parent_result_id = (
            await session.execute(
                select(QueryRun.parent_result_id).where(QueryRun.answer_id == answer_id)
            )
        ).scalar_one_or_none()
    # The gate judges which sources the answer should have used, which is
    # planning-shaped work: it gets the planner's manifest line — class and
    # declared annotations included — not a bare filename and summary. It
    # cannot call a judgment "plainly more authoritative" than a bulletin
    # article without being shown which document is which.
    mrows = await _manifest_rows(parent_result_id) if parent_result_id else []
    claims = "\n".join(f"{seq}. {text}" for seq, text in rows)
    source_lines = "\n".join(
        f"- [{'CITED' if r['filename'] in cited else 'uncited'}] "
        f"{r['filename']} | {r['class'] or '-'} | {r['annotations'] or '-'} | "
        f"{r.get('properties') or '-'} | "
        f"{r['summary'].replace(chr(10), ' ')[:200]}"
        for r in mrows
    ) or "(working set unavailable)"
    # The decomposition is fixed for the run and judged here, not derived
    # here. An empty list means the split could not be read twice, and the
    # prompt falls back to deriving it, which is the behaviour this replaces.
    fixed = await _question_parts(sinas, run_id, run_question)
    reply = await sinas.invoke(
        "sgr/answer-gate-agent",
        "QUESTION:\n" + run_question
        + "\n\nCLAIMS OF THE DRAFT ANSWER (the number before each claim is "
          "its identifier, not its position: revision drops claims, so gaps "
          "in the numbering are expected and are not a defect — there is no "
          "claim missing from this list):\n" + claims
        + "\n\nWORKING DOCUMENT SET (each marked CITED if the answer uses it):\n" + source_lines
        + (('\n\nPARTS OF THE QUESTION (fixed for this run; judge'
            ' each against the claims, and do not add, merge or drop'
            ' one):\n'
            + "\n".join(f'  {i}. {a}' for i, a in enumerate(fixed, 1)))
           if fixed else
           '\n\nFirst split the QUESTION into the distinct things it asks: '
           'a question asking what the conditions are, whether a regulation '
           'applies, and whether a step is mandatory asks three things, not one.')
        + '\n\nJudge each part separately against the claims. A part is COVERED when '
        'the claims answer it either way: claims that rebut the premise of '
        'the question with grounds — the question asks about liability '
        'without fault, the claims establish fault is always required — '
        'ANSWER that part; do not demand a claim affirming a premise the '
        'sources reject. A claim that states plainly that the available '
        'sources do not address a part (an abstention) also COVERS that '
        'part: telling the reader what the sources cannot establish is the '
        'honest answer when the corpus lacks the authority, not a gap.'
        '\n\nThen read the UNCITED documents in the working set against the '
        'parts. A part can be covered and still be poorly served: a claim '
        'rests on commentary or an interim order while the deciding judgment '
        'sits uncited, or a part is answered thinly while an uncited document '
        'whose manifest line bears squarely on it holds the mechanism or '
        'holding the answer lacks. Name such documents in unused_sources — '
        'coverage alone does not make an unused, plainly better document '
        'acceptable to leave unread.'
        '\n\nReply ONLY JSON: {"publishable": true|false,'
        + (' "parts": [{"n": <the number of the part above>, "covered": '
           'true|false, "gap": "<what is missing, if not covered>"}],'
           if fixed else
           ' "parts": [{"asks": "<one thing the question asks>", "covered": '
           'true|false, "gap": "<what is missing, if not covered>"}],')
        + ' "missing": "<if not publishable: what the claims fail to deliver on>",'
        ' "unresponsive": [<sequence numbers of claims that only describe a source without advancing the answer>],'
        ' "tension": "<ONLY a pair of claims that CANNOT BOTH BE TRUE — quote the '
'two incompatible propositions verbatim. Claims that restate the same rule, '
'overlap, emphasise different aspects, or address different procedural '
'stages are NOT in tension; when in doubt, null. Or null.>",'
        ' "dangling": [<sequence numbers of claims that lean on another claim that is not there: they open with or depend on phrases like "that logic", "applying this reasoning", "the same principle" whose antecedent claim is absent or says something else>],'
        ' "no_conclusion": <true if no claim draws the overall conclusion the question asks for>,'
        ' "unused_sources": ["<filename>: <the point it settles and why the answer is poorer without it — either plainly more direct or authoritative than the source cited for that point, or bearing squarely on a part of the question the claims treat thinly or not at all>", ...]}',
    )
    # Only the parse is guarded. A wide try around the whole body turns a
    # fault in this function into "the gate had no objection" — which is what
    # happened here for three hours — so everything after the parse runs
    # unguarded and fails the run loudly if it is broken.
    reparse: str | None = None
    try:
        data = _gate_json(reply)
    except ValueError as exc:
        # Recording the failure was right and was not enough. This call is
        # the only stage that asks whether the answer addresses the
        # question at all: every other check reads a claim against the
        # passage under it, and a claim can be perfectly grounded while the
        # part of the question it was meant to answer goes untouched. So
        # reading a parse failure as approval did not merely make a broken
        # gate look like a clean one, it made the run publish having
        # verified everything about its claims except the one thing this
        # call exists to establish, and left no signal that anyone had to
        # act on.
        #
        # Repair once, the way the drafter repairs its own reply above: the
        # verdict is malformed rather than absent, so "send it again,
        # valid" has a referent, and the gate's prompt is the largest in
        # the run and not worth resending.
        reparse = str(exc)[:200]
        reply = await sinas.invoke(
            "sgr/answer-gate-agent",
            "Your previous reply was not valid JSON: " + str(exc)[:200]
            + ". Send the same verdict again as strictly valid JSON. Escape "
            'every quotation mark inside a string as \\", and use no line '
            "breaks inside a string.\n\nPREVIOUS REPLY:\n" + (reply or "")[:60000])
        try:
            data = _gate_json(reply)
        except ValueError as exc2:
            # Twice is not a transient fault to paper over, and neither
            # remaining option is free. Failing the run would discard claims
            # that have already passed evidence validation, over a malformed
            # reply rather than anything wrong with them. Publishing would
            # assert a coverage check that never ran. Partial is the only
            # one of the three that is true: there is an answer, and whether
            # it covers the question was never established. The telemetry
            # key keeps its name so runs that took this path before and
            # after the change are one query, but it now marks a run that
            # stopped rather than one that shipped.
            await _record_gate_cycle(run_id, parts=[], reparse=reparse,
                                     unparseable=str(exc2)[:200])
            raise PartialOutcome(
                "coverage",
                "the completeness review could not be read, twice, so "
                "whether the claims address every part of the question was "
                "never established",
            ) from exc2

    # Coverage is judged per part. One holistic verdict let an answer
    # addressing two of a question's three parts publish, and named one
    # gap at a time when it failed — so revision fixed them one cycle
    # each, or the run ran out of cycles first.
    if fixed:
        # The list is what the runner asked about, not what came back. A
        # verdict naming a part outside it is dropped, and a part it does not
        # name is uncovered rather than assumed: those two rules are what make
        # a fixed decomposition binding instead of advisory. Without them the
        # drift returns through the verdict, which is how a part the question
        # does not ask was once added and immediately marked covered.
        seen: dict[int, dict] = {}
        for x in (data.get("parts") or []):
            if not isinstance(x, dict):
                continue
            try:
                n = int(x.get("n"))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(fixed):
                seen.setdefault(n, x)
        parts = [
            {"asks": a, "covered": bool((seen.get(n) or {}).get("covered")),
             "gap": str((seen.get(n) or {}).get("gap") or "").strip()
                    or ("" if n in seen else
                        "the review returned no verdict for this part")}
            for n, a in enumerate(fixed, start=1)
        ]
    else:
        parts = [x for x in (data.get("parts") or []) if isinstance(x, dict)]
    uncovered = [
        str(x.get("gap") or x.get("asks") or "").strip()
        for x in parts if not x.get("covered")
    ]
    uncovered = [u for u in uncovered if u]
    # The decomposition is recorded further down, after the obligation
    # ledger has been consulted, because a part now carries whether the
    # answer accounted for the sources the gate named as well as whether the
    # gate called it covered. Recording it here would mean two writes per
    # cycle to keep in step, which is the shape #106 removed.

    issues: list[str] = []
    # Correctness defects make the answer wrong or incoherent, and must be
    # fixed before publication. Everything else — a claim that only
    # describes its source, a better source left uncited — is recorded and
    # does not hold the answer back. Both used to sit in one list, so
    # "the answer contradicts itself" carried the same weight as "you
    # could have cited a stronger source", and shipped.
    correctness: list[str] = []
    seqs = [s for s in (data.get("unresponsive") or []) if isinstance(s, (int, str))]
    if seqs:
        issues.append(
            "Claims " + ", ".join(str(s) for s in seqs) + " only describe their source "
            "document; each must state what that source contributes to answering the "
            "question, or be dropped."
        )
    if data.get("tension"):
        correctness.append(
            "Unreconciled tension: " + str(data["tension"]) + " Add a claim that "
            "reconciles these positions (grounded in evidence), or revise them."
        )
    dang = [s for s in (data.get("dangling") or []) if isinstance(s, (int, str))]
    if dang:
        correctness.append(
            "Claims " + ", ".join(str(s) for s in dang) + " depend on reasoning "
            "from a claim that is no longer in the answer. Rewrite each to stand "
            "alone (restate the reasoning it relies on, with evidence), or drop it."
        )
    if data.get("no_conclusion"):
        correctness.append(
            "The answer never draws its overall conclusion. Add a final claim "
            "that directly answers the question, supported by the evidence "
            "already cited."
        )
    # Naming the document in prose is not enough. Revision may cite only
    # passages it is shown, and it is shown passages for the points passed to
    # it — so a run was told to use 32025M11936.md, given no line of it, and
    # correctly changed nothing. Each named source becomes a point to ground,
    # which is what causes it to be opened and quoted.
    #
    # And a message alone is not enough either: it lives one round, so a
    # source could be demanded, cited, and then lost to a later deletion
    # with nothing owed any more. Every source the gate names goes into the
    # run's obligation ledger; what is fed below comes from the ledger's
    # unmet entries — persisting across rounds, reopening if the citing
    # claim dies — not from this round's gate reply alone.
    fresh: dict[str, str] = {}
    for src in (data.get("unused_sources") or []):
        fn, _, why = str(src).partition(":")
        if fn.strip():
            fresh[fn.strip()] = (why.strip() or str(src))[:400]
            await obligations.record(run_id, fn.strip(), why.strip() or str(src))
    # What is owed this round is the ledger's to decide, this round's findings
    # included. Asking what was owed and then appending whatever had not come
    # back read an absence as "no opinion", and an entry is withheld for three
    # different reasons: waived, already cited, or unreadable. Only the last is
    # no opinion; the other two are decisions, and adding over them put retired
    # and satisfied obligations back in front of the reviser at a count the cap
    # could not act on.
    owed = await obligations.to_feed(run_id, answer_id, fresh)
    capped = [u for u in owed if u["fed"] >= obligations.MAX_FEEDS]
    for u in capped:
        await obligations.waive(
            run_id, u["doc"],
            f"not grounded after {u['fed']} revision attempts", by="system")
    if capped:
        await _tele(run_id, "validate",
                    obligations_system_waived=[u["doc"] for u in capped])
    feed = [u for u in owed if u["fed"] < obligations.MAX_FEEDS][:5]
    stronger = [f"{u['note']} [obligated document: {u['doc']}]" for u in feed]
    for u in feed:
        issues.append(
            f"Owed source unused: {u['doc']} — {u['note']} Cite it for the "
            "point it carries, or waive it with a rationale you can only "
            "give after reading its passages. An obligation neither cited "
            "nor waived returns every round."
        )
    await obligations.note_fed(run_id, [u["doc"] for u in feed])

    # What the answer has not accounted for, decided from the ledger rather
    # than asked of the model. The gate is given two jobs in one call and
    # nothing ties its answers together: it judges each part covered, and it
    # separately names sources that bear on those parts and sit uncited. Its
    # own prompt permits both at once — "a part can be covered and still be
    # poorly served" — and `publishable` reads only the first, so a source the
    # gate itself called decisive never affected whether the question counted
    # as answered.
    #
    # Measured over eleven runs with a decomposition recorded: in the cycle
    # that decided publication, 29 of 29 parts were covered, and ten of the
    # eleven published with at least one named source neither cited nor
    # waived. That is not the run's whole history — one run did return an
    # uncovered part in an earlier cycle before converging — but it is the
    # verdict each run published on.
    #
    # The tie is answer-scoped because it cannot honestly be finer: the gate
    # names sources without saying which part each bears on.
    unaccounted = await obligations.unaccounted(run_id, answer_id)
    if unaccounted:
        # Ahead of the per-source lines, because those read as "a better
        # source exists" and the reviser is told to add a claim only where the
        # answer fails to address the question. It has just been told it does
        # not fail. This says what the per-source lines do not: while a named
        # source is unaccounted for, the question is not yet fully answered,
        # so writing a claim that cites one is in scope.
        issues.insert(0, (
            f"{len(unaccounted)} source(s) this review named as bearing on "
            "what the question asks are neither cited nor waived, so the "
            "answer does not yet account for them and the question is not "
            "fully answered. Adding a claim that cites one is in scope. "
            "Waiving it with a rationale you can only give after reading its "
            "passages is in scope. Leaving it untouched is not."))
    await _record_gate_cycle(
        run_id, reparse=reparse, unaccounted=unaccounted,
        parts=[{"asks": str(x.get("asks") or "")[:300],
                "covered": bool(x.get("covered")),
                "accounted": not unaccounted,
                "gap": str(x.get("gap") or "")[:300]}
               for x in parts])
    # A claim can attribute something to a source and never say which source.
    # The evidence checker cannot see that: it asks whether stated provenance
    # is correct, and unstated provenance is not wrong. So it is checked here,
    # deterministically, and only ever as an issue. The claim is true; it is
    # written so the reader cannot follow it, which is not grounds to hold an
    # answer back.
    issues += await claim_naming.issues_for(answer_id)
    # every uncovered part is a gap the answer must close, not just one
    missing = "; ".join(uncovered) if uncovered else str(data.get("missing") or "")
    publishable = bool(data.get("publishable")) and not uncovered
    # Coverage gaps first: they are what blocks publication, and the reviser
    # is given passages for a bounded number of points.
    return publishable, missing, issues + correctness, correctness, uncovered + stronger


def _gate_remediation_msg(missing: str, issues: list[str]) -> str:
    parts = ([f"The verified claims no longer fully answer the question. Missing: {missing}"]
             if missing else []) + issues
    return (
        "Answer review found problems to fix before publication:\n- "
        + "\n- ".join(parts)
        + "\nGround every new or revised claim ONLY in evidence you can bind "
        "(read documents with numbered:true and copy the visible line numbers "
        "into spans). Revise an existing claim by re-posting its sequence "
        "number. When the problem is voice — the cited passage reports an "
        "advocate's or interested party's words, and the claim presents them "
        "as the decider's own finding — the fix is re-attribution, not "
        "dropping: restate the claim in the true voice if it still advances "
        "the answer. Then reply REMEDIATION COMPLETE."
    )


def _overreach_detail(verdicts: list[dict]) -> list[dict]:
    """What the coverage check objected to, small enough to store.

    The check judges whether the union of a claim's spans carries the whole
    claim, and it works: 384 claims marked across 3,777 judged. But its
    verdicts were the one kind `validate_answer_evidence` returns without
    persisting — the per-span ones land in `claim_evidence.validated` and
    `validation_reasoning`, while a coverage verdict is separated off before
    that and lives only long enough to build the reviser's feedback. Only its
    count reached telemetry, so 15 answers published with overreach standing
    in the round that decided them and nothing records which claim it was.

    The claim text is kept beside the sequence deliberately. Revision narrows
    an overreaching claim to its supported core, so reading the claim back by
    sequence after the run tells you what it became, not what was objected to.

    Pure: dicts in, dicts out.
    """
    return [
        {"seq": v.get("claim_sequence"),
         "uncovered": str(v.get("uncovered") or "")[:300],
         "claim": str(v.get("claim_text") or "")[:300]}
        for v in verdicts
    ]


async def _pre_publish_sweep(
    run_id: uuid.UUID, sinas: _Sinas, caller, answer_id: uuid.UUID,
    question: str,
) -> bool:
    """Strong-tier re-judge of the FULL surviving claim set — the last
    checkpoint before any publish, on every path that publishes. Returns
    True when publication may proceed; False after feeding findings to one
    repair cycle (the caller re-enters the validate loop); raises
    PartialOutcome once the repair chance is spent, with the flagged
    claims dropped so the partial cannot ship them.

    Cycle-internal validation stays on the cheap tier; this is where
    capability is decisive (voice, modality) and where a miss ships to a
    reader. It originally guarded only the clean-cycle publish branch —
    a run that exhausted its validation rounds published unswept.
    """
    from app.services.faithfulness import validate_answer_evidence

    async with AsyncSessionLocal() as s2:
        run_row = await s2.get(QueryRun, run_id)
        sweeps = int(((run_row.telemetry or {}).get("validate")
                      or {}).get("final_sweeps") or 0)
    async with AsyncSessionLocal() as s2:
        fv = await validate_answer_evidence(
            s2, caller, answer_id, pending_only=False,
            run_id=run_id, final=True)
    f_over = fv.get("overreaching") or []
    # Both writes, not one. The sweep is a second overreach finding of the
    # same kind — 37 across 23 stored runs — and recording the subject in one
    # place and not the other is how the mirrored defects in #103 and #106
    # appeared: two writes of the same fact that drift apart.
    await _tele(run_id, "validate", final_sweeps=sweeps + 1,
                final_sweep_result={
                    "failed": len(fv["failed"]),
                    "overreaching": len(f_over),
                    "overreaching_claims": _overreach_detail(f_over)})
    if not fv["failed"] and not f_over:
        return True
    fb = [f"Claim {f['claim_sequence']}: {f['reason']}"
          for f in fv["failed"]]
    fb += [f"Claim {o.get('claim_sequence')} asserts more "
           f"than its passages establish: {o.get('uncovered')}. "
           "Narrow it to what the passages say, or bind "
           "evidence that carries the rest."
           for o in f_over]
    if sweeps >= 1:
        # One repair attempt was already spent on sweep findings;
        # publishing anyway would ship the exact defect class this sweep
        # exists to stop. Drop the flagged claims first: overreach
        # findings do not fail evidence rows, so without this the
        # partial's validated-claims set kept exactly the claims the
        # sweep objected to.
        flagged_ids = {f["claim_id"] for f in fv["failed"]
                       if f.get("claim_id")}
        flagged_ids |= {o["claim_id"] for o in f_over if o.get("claim_id")}
        async with AsyncSessionLocal() as s3:
            for cid in flagged_ids:
                cid = uuid.UUID(str(cid))
                await s3.execute(
                    ClaimEvidence.__table__.delete()
                    .where(ClaimEvidence.claim_id == cid))
                await s3.execute(
                    AnswerClaim.__table__.delete()
                    .where(AnswerClaim.id == cid))
            await s3.commit()
        await _tele(run_id, "validate", final_sweep_dropped=len(flagged_ids))
        # A fixable defect in one claim must not demote the whole answer.
        # The flagged claims are gone — visibly: the drop is recorded and
        # the numbering keeps the gap — so re-ask the gate whether what
        # survives still answers the question. If it does, publish; the
        # partial state is reserved for the corpus genuinely not answering,
        # not for the repair budget running out one claim short.
        ok, missing, _issues, correctness, _pts = await _gate_answer(
            sinas, question, answer_id, run_id)
        if ok and not correctness:
            await _tele(run_id, "validate", final_sweep_published_after_drop=True)
            return True
        raise PartialOutcome(
            "coverage",
            "after removing claims the final review could not support, the "
            "surviving claims no longer fully answer the question — "
            + (missing or " ".join(fb))[:600])
    await _revise_answer(sinas, run_id, answer_id, question, fb,
                         last_attempt=True)
    return False


async def _compact_claim_sequences(session, answer_id: uuid.UUID) -> None:
    """Compact claim numbering to 1..N. Sequence gaps left by drops are
    identity during revision, but a terminal answer whose claims jump 3 -> 5
    sends reviewers hunting for a missing claim. Only call once no patch
    cycle can run again. Does not commit."""
    claims = (await session.execute(
        select(AnswerClaim)
        .where(AnswerClaim.answer_id == answer_id)
        .order_by(AnswerClaim.sequence)
    )).scalars().all()
    if any(c.sequence != i for i, c in enumerate(claims, start=1)):
        # Two phases under uq_answer_claim_sequence: park everything out
        # of range first, then assign the compact numbering.
        park = len(claims) + 1000
        for i, c in enumerate(claims):
            c.sequence = park + i
        await session.flush()
        for i, c in enumerate(claims, start=1):
            c.sequence = i


async def _publish_answer(run_id: uuid.UUID, answer_id: uuid.UUID, **tele: Any) -> None:
    from app.models import Answer

    async with AsyncSessionLocal() as session:
        row = await session.get(Answer, answer_id)
        row.status = "published"
        row.published_at = _now()
        await _compact_claim_sequences(session, answer_id)
        await session.commit()
    await _tele(run_id, "validate", published=_iso(), **tele)


def _spans_of(obj: dict) -> list[dict]:
    """Citable spans only: a filename and a line number, or it is not one."""
    return [
        {"filename": str(e["filename"]), "line_from": int(e["line_from"]),
         "line_to": int(e.get("line_to") or e["line_from"])}
        for e in (obj.get("evidence") or [])
        if isinstance(e, dict) and e.get("filename")
        and str(e.get("line_from", "")).lstrip("-").isdigit()
    ]


# An abstention says, in the answer, that the sources do not settle a point.
# It carries no evidence, so it is the one claim the faithfulness machinery
# cannot check — which is why it is allowed only on the last revision, capped,
# and still judged by the gate.
MAX_ABSTENTIONS = 2


def _parse_patch(reply: str, allow_abstention: bool = False) -> dict | None:
    """A revision is a patch: which claims to rewrite, drop and add.

    The guard is structural. A rewritten claim needs a sequence number, text
    and at least one citable span; an added claim needs text and a span. A
    reply that is prose, a refusal, or a verdict on the evidence yields no
    operations and changes nothing — nothing is decided by matching words.
    """
    try:
        cleaned = (reply or "").strip().strip("`").removeprefix("json").strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(cleaned[start : end + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None

    revise = []
    for c in (data.get("revise") or []):
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        spans = _spans_of(c)
        if len(text) >= 30 and spans and str(c.get("seq", "")).lstrip("-").isdigit():
            revise.append({"seq": int(c["seq"]), "text": text, "evidence": spans,
                           "rationale": str(c.get("rationale") or "").strip()})

    add = []
    abstentions = 0
    for c in (data.get("add") or []):
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        spans = _spans_of(c)
        if len(text) < 30:
            continue
        if spans:
            add.append({"text": text, "type": c.get("type"), "evidence": spans,
                        "rationale": str(c.get("rationale") or "").strip()})
        elif (allow_abstention
              and str(c.get("type") or "").lower() == "abstention"
              and abstentions < MAX_ABSTENTIONS):
            abstentions += 1
            add.append({"text": text, "type": "abstention", "evidence": [],
                        "rationale": str(c.get("rationale") or "").strip()})

    drop = [int(x) for x in (data.get("drop") or [])
            if str(x).lstrip("-").isdigit()]

    # Keeping a claim the gate objected to is an answer, not a non-answer.
    # When the gate names a stronger source, the draft's citation is often
    # still the right one — but with nowhere to say so, that decision was
    # indistinguishable from having ignored the finding. A keep changes only
    # the rationale, never the text or the spans, so it cannot smuggle in a
    # claim: it needs a sequence number and a reason, and nothing else.
    keep = []
    for c in (data.get("keep") or []):
        if not isinstance(c, dict):
            continue
        why = str(c.get("rationale") or "").strip()
        if len(why) >= 20 and str(c.get("seq", "")).lstrip("-").isdigit():
            keep.append({"seq": int(c["seq"]), "rationale": why})

    waives = []
    for w in (data.get("waive") or []):
        if isinstance(w, dict) and str(w.get("doc") or "").strip() \
                and len(str(w.get("rationale") or "").strip()) >= 20:
            waives.append({"doc": str(w["doc"]).strip(),
                           "rationale": str(w["rationale"]).strip()})
    if not (revise or add or drop or keep or waives):
        return None
    return {"revise": revise, "add": add, "drop": drop, "keep": keep,
            "waive": waives}


def _cycle_key(existing: dict, prefix: str) -> str:
    """The next per-cycle telemetry key, `prefix_1` upward.

    `_tele` merges by key and cannot delete, so a stage that writes the same
    key every cycle keeps only its last one. `round_N` already avoids that by
    numbering; this is the same trick for stages whose loop has no counter of
    its own to number by.
    """
    return f"{prefix}_{sum(1 for k in existing if k.startswith(prefix + '_')) + 1}"


async def _next_cycle_key(run_id: uuid.UUID, stage: str, prefix: str) -> str:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        entry = ((run.telemetry if run is not None else None) or {}).get(stage) or {}
    return _cycle_key(entry, prefix)


def _admit_adds(adds: list[dict], live: int) -> tuple[list[dict], int]:
    """Split the reviser's additions into those the answer has room for and
    the count refused, given `live` claims already in it.

    Pure, and separate, because the refusal is silent everywhere else: an
    answer at MAX_CLAIMS takes nothing, the reviser is not told, and the run
    continues as though the claim had been written. Returning the refused
    count is what lets the caller record a cap hit instead of recording an
    addition that never happened.
    """
    room = max(0, MAX_CLAIMS - live)
    return adds[:room], len(adds) - min(len(adds), room)


async def _bind_spans(session, claim_id: uuid.UUID, spans: list[dict]) -> None:
    for sp in spans[:4]:
        doc = (await session.execute(
            select(Document).where(Document.filename == sp["filename"]))
        ).scalars().first()
        if doc is None:
            continue
        session.add(ClaimEvidence(
            claim_id=claim_id, document_id=doc.id,
            document_version_id=doc.current_version_id,
            span={"line_from": sp["line_from"], "line_to": sp["line_to"],
                  "char_from": None, "char_to": None, "note": None},
            validated=False))


async def _revise_answer(
    sinas: _Sinas, run_id: uuid.UUID, answer_id: uuid.UUID, question: str,
    feedback: list[str], extra_points: list[str] | None = None,
    last_attempt: bool = False,
) -> int:
    """Rewrite the whole answer from its evidence and the round's feedback.

    One operation replaces what used to be three — narrowing an overreaching
    claim, rebinding a failed one, and appending claims the gate asked for.
    They were all additive and all returned free text, so an answer could grow
    to 29 claims, could never be made coherent by removing something, and a
    model's reply about the task could land in the answer as a claim.

    Here the reviser is given every current claim with its passages, plus any
    newly extracted passages for points the gate raised, and returns the
    corrected claim set as JSON. The set REPLACES the old one, so revising,
    dropping and adding are the same operation. A reply that is not a claim
    object is not a claim: no text matching decides it.
    """
    if not feedback:
        return 0

    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        parent_id = run.parent_result_id
        rows = (await session.execute(
            select(AnswerClaim, ClaimEvidence, Document.filename,
                   DocumentVersion.content_md)
            .outerjoin(ClaimEvidence, ClaimEvidence.claim_id == AnswerClaim.id)
            .outerjoin(Document, Document.id == ClaimEvidence.document_id)
            .outerjoin(DocumentVersion,
                       DocumentVersion.id == Document.current_version_id)
            .where(AnswerClaim.answer_id == answer_id)
            .order_by(AnswerClaim.sequence)
        )).all()
    corpus_rows = (await _manifest_rows(parent_id))[:60]
    corpus = [r["filename"] for r in corpus_rows if r.get("filename")]

    # Current claims — but full passages only for the ones the feedback
    # names. The reviser is instructed to change only what the feedback
    # identifies, so untouched claims are context, not work: sending their
    # passages tripled the prompt and invited re-emitting them, and the
    # reviser call is where 70% of a run's wall-clock goes. A claim the
    # patch does not name keeps its row, its spans and its verdicts — it is
    # never rebuilt, so it is never re-judged.
    named = {int(m) for f in feedback for m in re.findall(r"[Cc]laims? (\d+)", f)}
    named |= {int(m) for f in feedback
              for m in re.findall(r"(?:^|[ ,])(\d+)(?=[ ,.:]|$)", f)}
    by_claim: dict[int, dict] = {}
    for claim, ev, fn, content in rows:
        entry = by_claim.setdefault(claim.sequence, {"text": claim.claim_text,
                                                     "passages": []})
        if ev is None or not content or (ev.span or {}).get("line_from") is None:
            continue
        lf, lt = int(ev.span["line_from"]), int(ev.span.get("line_to") or ev.span["line_from"])
        body = "\n".join(content.splitlines()[max(0, lf - 1):lt])[:1200]
        entry["passages"].append(f"[{fn} lines {lf}-{lt}]\n{body}")

    current = "\n\n".join(
        f"CLAIM {seq}: {c['text']}\n"
        + (("\n".join(c["passages"]) or "(no passage)")
           if (seq in named or not named) else "(passages withheld — this "
           "claim is context; do not revise it)")
        for seq, c in sorted(by_claim.items()))

    # passages for anything the gate said was missing
    async def _anchors_for(point: str) -> tuple[list[str], str]:
        """Documents that could contain this point, best first: full-text
        term hits (with line neighborhoods for the extractor) lead, then
        the summary matcher planning uses, then a couple of top-ranked
        for context."""
        found = await _content_anchors(point, corpus, take=4)
        picked = [fn for fn, _ in found]
        picked += [fn for fn in _relevant_docs(point, corpus_rows, take=6)
                   if fn not in picked]
        picked += [fn for fn in corpus[:4] if fn not in picked]
        near = "; ".join(
            f"{fn} near line {lines[0]}" for fn, lines in found if lines)
        return picked[:10], near

    fresh = ""
    if extra_points and corpus:
        plan = []
        windows: list[tuple[str, int, int, str]] = []
        for i, pt in enumerate(extra_points[:5], start=1):
            anchors, near = await _anchors_for(pt)
            # An obligated point names its document; the term-scan may rank
            # it low or miss it. The debt is to THAT document, so it leads.
            ob = re.search(r"\[obligated document: ([^\]]+)\]", pt)
            owed = ob.group(1) if ob else None
            if owed:
                anchors = [owed] + [a for a in anchors if a != owed]
            # The term scan already KNOWS where the matching lines sit.
            # Hand the reviser those windows directly — text copied from
            # the stored document, so the quotes are correct by
            # construction — instead of hoping a whole-document extraction
            # pass lands on them. This is how a ten-day statutory limit,
            # located by search, still went unextracted for a whole run.
            found = await _content_anchors(pt, corpus, take=3)
            async with AsyncSessionLocal() as session:
                for fn, lines in found:
                    if not lines:
                        continue
                    content = (await session.execute(
                        select(DocumentVersion.content_md)
                        .join(Document,
                              Document.current_version_id == DocumentVersion.id)
                        .where(Document.filename == fn))).scalar()
                    if not content:
                        continue
                    doc_lines = content.split("\n")
                    lf = max(1, lines[0] - 3)
                    lt = min(len(doc_lines), lines[0] + 6)
                    windows.append(
                        (fn, lf, lt, "\n".join(doc_lines[lf - 1:lt])[:1500]))
            plan.append({
                # The debt travels as a field. It used to reach the extractor
                # only as a marker inside this sentence, which is truncated at
                # 600 characters: a 400-character note plus a 130-character
                # filename left 48 characters of margin, and lengthening
                # either would have dropped the marker with nothing failing
                # loudly. Raising the number moves that cliff rather than
                # removing it. The sentence keeps the note alone.
                "n": i, "establishes": (pt[:ob.start()] if ob else pt).strip()[:600],
                "owed": owed, "anchors": anchors,
                "hint": ("extract the passage that states this; a case "
                         "caption or party list is not a holding"
                         + (f". Matching terms sit at: {near}" if near else ""))})
        for fn, lf, lt, txt in windows[:8]:
            fresh += f"\n[{fn} lines {lf}-{lt}]\n{txt}\n"
        for ex in await _extract_passages(sinas, plan, run_id):
            for pas in (ex.get("passages") or [])[:3]:
                fresh += (f"\n[{pas['filename']} lines {pas['line_from']}-"
                          f"{pas['line_to']}]\n{pas['text'][:1200]}\n")

    reply = await sinas.invoke(
        "sgr/retrieval-planner-agent",
        f"Correct {_domain_article()}answer. Every current claim is listed for "
        "context, with the passages bound to it. Only some are wrong.\n\n"
        "Change ONLY what the feedback identifies. Leave every other claim "
        "alone — do not restate it, do not rephrase it, do not return it. "
        "A claim you do not mention is kept exactly as it is.\n\n"
        "For each claim you do change: narrow it if it asserts more than its "
        "passages establish, rewrite it if it contradicts another claim, and "
        "drop it if no passage can carry it. Add a claim only where the answer "
        "fails to address the question. Every claim you write must be carried "
        "entirely by the passages you cite for it, and you may cite only "
        "passages shown below. Keep the answer at about 12 claims.\n\n"
        "Where the feedback names a stronger source and you judge the current "
        "citation to be the better one, say so instead of changing nothing: "
        'put the claim in "keep" with a rationale giving the reason. A keep '
        "changes neither the claim nor its evidence. Use it only when you "
        "have read the passages from the named source and they do not carry "
        "the point better — not to avoid the work.\n\n"
        "A feedback line marked as an OWED source is an obligation, not a "
        "suggestion: either cite that document in a revised or added claim, "
        'or list it in "waive" with a rationale you could only give after '
        "reading its passages. An obligation neither cited nor waived comes "
        "back every round.\n\n"
        "Give a RATIONALE with every claim you revise, add or keep: ONE "
        "short sentence, at most 20 words — which part of the question it "
        "answers and why this source settles it. Never restate the claim. "
        "It is reasoning, not evidence — nothing in it may assert anything "
        "the passages do not show. Name a source the way the claim names it "
        "— deciding body and case reference — never by its filename.\n\n"
        + ("This is the final revision. If the passages available genuinely "
           "cannot settle a point the question asks about, do not stretch a "
           "source to cover it and do not leave the point unmentioned: add a "
           'claim with "type": "abstention" and no evidence, stating plainly '
           "which part of the question the available sources do not answer. "
           "Say what is missing, not that you are unable — 'The documents "
           "before us do not address X' rather than 'I cannot determine X'. "
           f"At most {MAX_ABSTENTIONS} such claims, and never for the central "
           "question if the sources do answer it.\n\n" if last_attempt else "")
        + 'Reply ONLY JSON: {"revise": [{"seq": <int>, "text": "<claim>", '
        '"rationale": "<why this claim rests on this source>", '
        '"evidence": [{"filename": "...", "line_from": <int>, "line_to": <int>}]}], '
        '"drop": [<seq>], '
        '"keep": [{"seq": <int>, "rationale": "<why the current citation '
        'stands despite the feedback>"}], '
        '"waive": [{"doc": "<filename>", "rationale": "<why, having read '
        'its passages, this OWED document does not carry any point this '
        'answer needs>"}], '
        '"add": [{"text": "<claim>", "type": "legal_principle|factual|'
        'procedural|conclusion", "rationale": "<why this claim rests on this '
        'source>", "evidence": [{"filename": "...", '
        '"line_from": <int>, "line_to": <int>}]}]}\n\n'
        + await _synthesis_playbook()
        + f"\nQUESTION:\n{question}\n\nCURRENT ANSWER:\n{current}\n\n"
        f"FEEDBACK:\n- " + "\n- ".join(feedback[:10])
        + (f"\n\nADDITIONAL VERIFIED PASSAGES:{fresh}" if fresh else ""),
    )

    patch = _parse_patch(reply, allow_abstention=last_attempt)
    if patch:
        # A waive is a discharge with a recorded reason, not a dropped
        # message: it is honored even when the rest of the patch is empty.
        for w in patch.get("waive") or []:
            await obligations.waive(run_id, w["doc"], w["rationale"])
    if not patch or not (patch["revise"] or patch["add"] or patch["drop"]):
        await _tele(run_id, "validate", revision_yielded_no_change=True)
        return 0

    by_seq = {c.sequence: c for c, *_ in rows}
    touched = 0
    # Bound before the add block, which does not run when the patch adds
    # nothing; the telemetry below reads both either way.
    admitted: list[dict] = []
    add_dropped_at_cap = 0
    async with AsyncSessionLocal() as session:
        for seq in patch["drop"]:
            claim = by_seq.get(seq)
            if claim is None:
                continue
            await session.execute(ClaimEvidence.__table__.delete()
                                  .where(ClaimEvidence.claim_id == claim.id))
            await session.execute(AnswerClaim.__table__.delete()
                                  .where(AnswerClaim.id == claim.id))
            touched += 1

        for item in patch["revise"]:
            claim = by_seq.get(item["seq"])
            if claim is None:
                continue
            row = await session.get(AnswerClaim, claim.id)
            if row is None:
                continue
            row.claim_text = item["text"][:4000]
            if item.get("rationale"):
                row.rationale = item["rationale"][:2000]
            # its evidence is re-bound, so its verdicts no longer apply
            await session.execute(ClaimEvidence.__table__.delete()
                                  .where(ClaimEvidence.claim_id == row.id))
            await _bind_spans(session, row.id, item["evidence"])
            touched += 1

        if patch["add"]:
            live = (await session.execute(
                select(func.count(AnswerClaim.id))
                .where(AnswerClaim.answer_id == answer_id))).scalar() or 0
            nxt = ((await session.execute(
                select(func.max(AnswerClaim.sequence))
                .where(AnswerClaim.answer_id == answer_id))).scalar() or 0) + 1
            admitted, add_dropped_at_cap = _admit_adds(patch["add"], live)
            for item in admitted:
                row = AnswerClaim(answer_id=answer_id, sequence=nxt,
                                  claim_text=item["text"][:4000],
                                  rationale=(item.get("rationale") or "")[:2000]
                                  or None,
                                  claim_type=str(item.get("type")
                                                 or "legal_principle")[:50])
                session.add(row)
                await session.flush()
                await _bind_spans(session, row.id, item["evidence"])
                nxt += 1
                touched += 1
        for item in patch.get("keep") or []:
            claim = by_seq.get(item["seq"])
            if claim is None:
                continue
            row = await session.get(AnswerClaim, claim.id)
            if row is not None:
                # text and spans untouched, so its verdicts still stand and
                # it is not re-judged. Only the reasoning is recorded.
                row.rationale = item["rationale"][:2000]
        await session.commit()

    # `added` counts rows written, not rows asked for. It used to be
    # len(patch["add"]), so a patch whose additions were all refused at the
    # cap recorded the same number as one where every addition landed.
    # `add_dropped_at_cap` carries the difference; the two sum to what the
    # reviser proposed.
    #
    # And one key per cycle, like round_N. Recording the right number was not
    # enough on its own: a single `revision` key kept only the last cycle, so
    # a run that revised four times threw away three cycles of adds, drops and
    # cap refusals before anyone could read them. Measured on three runs that
    # each reached the cap, every one reported add_dropped_at_cap = 0, because
    # the cycle that survived was not the cycle that hit it. Numbering is what
    # makes this a history instead of a last-write.
    cycle = await _next_cycle_key(run_id, "validate", "revision")
    await _tele(run_id, "validate", **{
        cycle: {
            "claims": len(by_claim), "revised": len(patch["revise"]),
            "kept_with_reason": len(patch.get("keep") or []),
            "abstentions": sum(1 for a in admitted
                               if a.get("type") == "abstention"),
            "dropped": len(patch["drop"]), "added": len(admitted),
            "add_dropped_at_cap": add_dropped_at_cap,
            "untouched": len(by_claim) - touched,
            "feedback_items": len(feedback)}})
    return touched


async def _stage_validate_publish(
    run_id: uuid.UUID, sinas: _Sinas, gate_cycles: int | None = None
) -> None:
    from app.services.faithfulness import validate_answer_evidence

    await _tele(run_id, "validate", started=_iso())
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        answer_id = run.answer_id
        caller = _runner_caller(run)
        question_text = run.question
        if gate_cycles is None:
            gate_cycles = EFFORT_GATE_CYCLES.get(
                run.effort or "medium", ANSWER_GATE_CYCLES)

    await _mark(run_id, status="validating")
    failed_history: list[int] = []
    round_no = 0
    while True:
        round_no += 1
        spent = await _run_cost_usd(run_id)
        if spent > RUN_COST_CAP_USD:
            raise PartialOutcome(
                "budget_ceiling",
                f"run spend reached ${spent:.2f} (cap ${RUN_COST_CAP_USD:.0f}) "
                f"in validation round {round_no}")
        if round_no > MAX_VALIDATE_ROUNDS:
            converging = (len(failed_history) >= 2
                          and failed_history[-1] < failed_history[-2])
            if round_no > HARD_VALIDATE_ROUNDS or not converging:
                break
            await _tele(run_id, "validate", extended_to_round=round_no)
        async with AsyncSessionLocal() as session:
            verdict = await validate_answer_evidence(session, caller, answer_id,
                                              pending_only=True, run_id=run_id)
        failed_history.append(len(verdict["failed"]))
        await _tele(run_id, "validate", **{f"round_{round_no}": {
            "judged": verdict["judged"], "passed": verdict["passed"],
            "failed": len(verdict["failed"]), "errors": len(verdict["errors"]),
            "overreaching": len(verdict.get("overreaching") or []),
            # The count stays where it is: answer_regress reads it by prefix.
            # This is the same finding with its subject attached, so a run can
            # be asked which claim was objected to and on what ground.
            "overreaching_claims": _overreach_detail(
                verdict.get("overreaching") or []),
        }})
        # A claim whose every span passes can still assert more than those
        # spans establish — "the whole period" on passages about a second
        # infringement. That is what the reviewers marked as partially
        # supported, and it was found here every round and then ignored:
        # overreach only reached the reviser through the branch below, which
        # a clean span-level result skips entirely. It is a defect in the
        # answer, so it holds the answer back like any other.
        over = verdict.get("overreaching") or []
        if not verdict["failed"] and not verdict["errors"] and not over:
            async with AsyncSessionLocal() as session:
                pending = (
                    await session.execute(
                        select(ClaimEvidence.id)
                        .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
                        .where(AnswerClaim.answer_id == answer_id)
                        .where(ClaimEvidence.validated.is_(False))
                    )
                ).scalars().first()
            if pending is None:
                async with AsyncSessionLocal() as session:
                    question = (await session.get(QueryRun, run_id)).question
                ok, missing, issues, correctness, points = await _gate_answer(
                    sinas, question, answer_id, run_id)
                if ok and not correctness and (not issues or gate_cycles <= 0):
                    if not await _pre_publish_sweep(
                            run_id, sinas, caller, answer_id, question):
                        return await _stage_validate_publish(run_id, sinas, 0)
                    # quality issues never block publication on their own —
                    # unremediated ones are recorded, not fatal
                    tele = {"quality_issues": issues} if issues else {}
                    await _publish_answer(run_id, answer_id, **tele)
                    return
                key = _gate_key(missing, correctness)
                if gate_cycles <= 0 and (not ok or correctness):
                    if await _gate_point_is_new(run_id, key):
                        await _tele(run_id, "validate", gate_redraft=missing,
                                    gate_issues=issues, bonus_cycle=True)
                        await _record_fed(run_id, key, bonus=True)
                        await _revise_answer(
                            sinas, run_id, answer_id, question,
                            correctness + issues,
                            points or ([missing] if missing else []),
                            last_attempt=True)
                        return await _stage_validate_publish(run_id, sinas, 0)
                    if not ok:
                        raise PartialOutcome(
                            "coverage",
                            f"the validated claims no longer answer the question — {missing}")
                    # Distinct cause: this is not a source-coverage gap, and
                    # labeling it one made partial notes claim the sources
                    # were silent on points the run's own evidence settled.
                    raise PartialOutcome(
                        "consistency",
                        "the answer could not be made internally consistent — "
                        + " ".join(correctness)[:600])
                await _tele(run_id, "validate", gate_redraft=missing, gate_issues=issues)
                # A gap the reviser was already fed once is a gap it could
                # not close from the corpus. Allowing the declared-gap claim
                # only on the very last attempt meant the run burned every
                # cycle trying the impossible and went partial anyway —
                # partial as "out of retries" instead of "the sources do not
                # answer this". Second time a point comes back, the honest
                # abstention is on the table.
                repeated = not await _gate_point_is_new(run_id, key)
                await _record_fed(run_id, key)
                await _revise_answer(
                    sinas, run_id, answer_id, question,
                    correctness + issues,
                    points or ([missing] if missing else []),
                    last_attempt=gate_cycles <= 1 or repeated)
                return await _stage_validate_publish(
                    run_id, sinas, gate_cycles - 1)
        # One revision per round, over everything this round found.
        fb = [f"Claim {f['claim_sequence']}: {f['reason']}"
              for f in verdict["failed"]]
        fb += [f"Claim {o.get('claim_sequence')} asserts more than its "
               f"passages establish: {o.get('uncovered')}. Narrow it to what "
               f"the passages say, or bind evidence that carries the rest."
               for o in over]
        if fb and await _revise_answer(sinas, run_id, answer_id, question_text, fb):
            continue
        break  # revision produced nothing usable; drop below

    # Rounds exhausted without convergence. Drop the claims that still carry
    # unvalidated evidence, then let the answer gate decide: the surviving
    # claims must still answer the question, or the drafter gets ONE redraft
    # cycle, or the run fails loudly. No counting floors.
    async with AsyncSessionLocal() as session:
        failing_ids = set(
            (
                await session.execute(
                    select(ClaimEvidence.claim_id)
                    .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
                    .where(AnswerClaim.answer_id == answer_id)
                    .where(ClaimEvidence.validated.is_(False))
                )
            ).scalars().all()
        )
        # Record what is being removed before removing it. Only a count was
        # kept, so a reader of the published answer saw claim numbering jump
        # from 1 to 3 to 10 with nothing to explain the gap, and no way to
        # tell a dropped claim from an export fault.
        dropped = []
        for cid in failing_ids:
            claim = await session.get(AnswerClaim, cid)
            if claim is not None:
                reasons = (await session.execute(
                    select(ClaimEvidence.validation_reasoning)
                    .where(ClaimEvidence.claim_id == cid)
                    .where(ClaimEvidence.validated.is_(False))
                )).scalars().all()
                dropped.append({
                    "sequence": claim.sequence,
                    "claim": (claim.claim_text or "")[:400],
                    "why": [r for r in reasons if r][:3],
                })
            await session.execute(
                ClaimEvidence.__table__.delete().where(ClaimEvidence.claim_id == cid)
            )
            await session.execute(
                AnswerClaim.__table__.delete().where(AnswerClaim.id == cid)
            )
        await session.commit()
    if dropped:
        await _tele(run_id, "validate",
                    dropped_detail=sorted(dropped, key=lambda d: d["sequence"]))
    async with AsyncSessionLocal() as session:
        question = (await session.get(QueryRun, run_id)).question
    ok, missing, issues, correctness, points = await _gate_answer(
        sinas, question, answer_id, run_id)
    if ok and not correctness and (not issues or gate_cycles <= 0):
        async with AsyncSessionLocal() as session:
            caller = _runner_caller(await session.get(QueryRun, run_id))
        if not await _pre_publish_sweep(
                run_id, sinas, caller, answer_id, question):
            return await _stage_validate_publish(run_id, sinas, 0)
        tele = {"quality_issues": issues} if issues else {}
        await _publish_answer(
            run_id, answer_id, dropped_claims=len(failing_ids), **tele
        )
        return
    key = _gate_key(missing, correctness)
    if gate_cycles <= 0 and (not ok or correctness):
        if await _gate_point_is_new(run_id, key):
            await _tele(run_id, "validate", gate_redraft=missing,
                        gate_issues=issues, bonus_cycle=True)
            await _record_fed(run_id, key, bonus=True)
            await _revise_answer(sinas, run_id, answer_id, question,
                                 correctness + issues,
                                 points or ([missing] if missing else []),
                                 last_attempt=True)
            return await _stage_validate_publish(run_id, sinas, 0)
        raise PartialOutcome(
            "coverage",
            ("validation exhausted and the surviving claims do not answer the "
             f"question — {missing}") if not ok else
            ("the answer could not be made internally consistent — "
             + " ".join(correctness)[:600]))
    await _tele(
        run_id, "validate",
        gate_redraft=missing, gate_issues=issues, dropped_claims=len(failing_ids),
    )
    await _record_fed(run_id, key)
    await _revise_answer(sinas, run_id, answer_id, question,
                         correctness + issues,
                         points or ([missing] if missing else []),
                         last_attempt=gate_cycles <= 1)
    return await _stage_validate_publish(run_id, sinas, gate_cycles - 1)


async def _gate_point_is_new(run_id: uuid.UUID, key: str) -> bool:
    """May this objection earn a bonus revision cycle?

    Each gate pass can raise findings the previous passes did not, but the
    cycle budget never asked whether a finding was ever shown to the
    reviser. A run died partial on an objection that surfaced only at the
    final check: zero revisions, zero chance to soften or abstain. An
    objection the reviser has never seen gets one attempt even with the
    budget spent; the same objection twice, or a third novel one, does not
    — the budget still bounds the run.
    """
    async with AsyncSessionLocal() as session:
        v = ((await session.get(QueryRun, run_id)).telemetry or {}).get("validate") or {}
    return key not in (v.get("fed_points") or []) and int(v.get("bonus_cycles") or 0) < 2


async def _record_fed(run_id: uuid.UUID, key: str, bonus: bool = False) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        t = dict(run.telemetry or {})
        v = dict(t.get("validate") or {})
        v["fed_points"] = ((v.get("fed_points") or []) + [key])[-12:]
        if bonus:
            v["bonus_cycles"] = int(v.get("bonus_cycles") or 0) + 1
        t["validate"] = v
        run.telemetry = t
        await session.commit()


def _gate_key(missing: str, correctness: list[str]) -> str:
    return ((missing or "")[:200] + "||" + " ".join(correctness)[:300]).strip()



def _note_language(question: str) -> str:
    """The note is written in the question's language, decided
    deterministically server-side: the writer model kept keying on the
    sources' language (a French-law question in English got a French
    note), and prompt rules did not hold."""
    from app.services.toc import _guess_language

    names = {"en": "English", "fr": "French", "de": "German",
             "nl": "Dutch", "es": "Spanish", "it": "Italian"}
    return names.get(_guess_language(question or ""), "English")


async def _mark_cancelled(run_id: uuid.UUID, c: CancelledOutcome) -> None:
    """Terminal `cancelled`: record when it stopped and who asked.

    No phrasing call, unlike `_mark_partial`: a cancelled run owes the client
    no explanation beyond the fact, and spending money to narrate a stop the
    client asked for would be perverse. `error` stays null — nothing failed.
    """
    await _tele(
        run_id,
        "cancel",
        cancelled_at=_now().isoformat(),
        requested_by=c.requested_by,
        message="This run was cancelled before it produced an answer.",
    )
    await _mark(run_id, status="cancelled", error=None, completed_at=_now())


async def _mark_partial(run_id: uuid.UUID, sinas: _Sinas, p: PartialOutcome) -> None:
    """Terminal `partial`: store cause + explanation + a short client-facing
    note (one cheap phrasing call, in the question's language) over the top
    of the stored retrieval. The note is explicitly NOT an answer; validated
    claims are not included — sources with reasons only."""
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        question, parent_id = run.question, run.parent_result_id
        if run.answer_id:
            # A partial is as terminal as a publish; reviewers read its
            # claims by number too.
            await _compact_claim_sequences(session, run.answer_id)
            await session.commit()
        validated_claims: list[str] = []
        if run.answer_id:
            # Claims whose every evidence row passed verification are as
            # trustworthy as in a published answer; a coverage-partial keeps
            # them ("on X we can say nothing; on Y the verified findings are").
            validated_claims = [
                t for (t,) in (
                    await session.execute(
                        select(AnswerClaim.claim_text)
                        .where(AnswerClaim.answer_id == run.answer_id)
                        .where(~AnswerClaim.id.in_(
                            select(ClaimEvidence.claim_id)
                            .where(ClaimEvidence.validated.is_(False))))
                        .order_by(AnswerClaim.sequence)
                    )
                ).all()
            ]
        cited_in_order: list[str] = []
        if run.answer_id:
            for (fn,) in (await session.execute(
                select(Document.filename)
                .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
                .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
                .where(AnswerClaim.answer_id == run.answer_id)
                .where(ClaimEvidence.validated.is_(True))
                .order_by(AnswerClaim.sequence)
            )).all():
                if fn not in cited_in_order:
                    cited_in_order.append(fn)
        cited = set(cited_in_order)
        sources: list[tuple[str, str]] = []
        if parent_id:
            ranked = [
                (fn, (reason or "")[:160])
                for fn, reason in (
                    await session.execute(
                        select(Document.filename, ResultDocument.reason)
                        .join(Document, Document.id == ResultDocument.document_id)
                        .where(ResultDocument.result_id == parent_id)
                        .order_by(ResultDocument.rank)
                        .limit(40)
                    )
                ).all()
            ]
            # Every document the verified claims cite leads the list, in
            # claim order — even when retrieval ranked it below the stored
            # top-40 (intersecting with the ranked list dropped exactly the
            # documents the findings rest on). Best-ranked uncited docs only
            # fill whatever room is left.
            reasons = dict(ranked)
            sources = [
                (fn, reasons.get(fn, "cited by the verified findings"))
                for fn in cited_in_order
            ]
            sources += [s for s in ranked if s[0] not in cited]
            sources = sources[:max(10, len(cited_in_order))]
    src_lines = "\n".join(f"- {fn}: {r}" for fn, r in sources) or "(none stored)"
    claim_lines = "\n".join(f"- {c[:300]}" for c in validated_claims[:12])
    claims_part = (
        "\n\nVERIFIED FINDINGS (each of these passed evidence verification; "
        "present them as what CAN be said, clearly separated from the gap):\n"
        + claim_lines if claim_lines else ""
    )
    message = ""
    try:
        # A dedicated writer persona: the gate agent's system prompt expects
        # a draft to judge, and invoked without one it refused — and the
        # refusal text was persisted as the client-facing note.
        message = await sinas.invoke(
            "sgr/note-writer-agent",
            "Reply with ONLY the note text itself — no preamble, no commentary "
            "about the task, no restatement of these instructions. "
            "Never reference claims or findings by number; internal numbering "
            "may not match what the reader sees. "
            "Describe any gap as what THIS ANALYSIS could not establish — "
            "never state that the sources lack or do not contain something: "
            "the analysis has read only part of them and cannot know that. "
            "Write a note (max 200 words) to "
            + get_settings().sgr_audience
            + ". WRITE THE NOTE IN " + _note_language(question).upper() + " — the "
            "language of the question, regardless of the language of any "
            "sources or findings below. Structure: (1) state plainly which "
            "part of the question could NOT be established and why (reason "
            "below, rephrased plainly — no internal jargon, no dollar amounts; if "
            "the reason mentions spend or budget, phrase it as: the analysis "
            "could not be completed within its allotted scope); "
            "(2) if verified findings are provided below, summarise what CAN be "
            "said, faithfully — do not go beyond them; (3) point to the source "
            "list for further reading. Never invent sources or findings."
            "\n\nQUESTION:\n" + question
            + "\n\nREASON: " + p.explanation
            + claims_part
            + "\n\nSOURCES:\n" + src_lines,
        )
    except CancelledOutcome:
        # Deliberately NOT re-raised, which is the opposite of what the same
        # shape needs everywhere else in this file, so it is spelled out.
        #
        # `_mark_partial` is called from inside `run_pipeline`'s
        # `except PartialOutcome` handler. An exception raised there does not
        # reach the sibling `except CancelledOutcome`; sibling handlers do not
        # catch each other. It would leave `run_pipeline` entirely with the run
        # row never marked, stranding it in its in-flight status forever, which
        # is the exact failure `CancelledOutcome`'s own docstring exists to
        # avoid.
        #
        # Nothing is lost by stopping here. The outcome is already decided by
        # the time this runs, a cancel cannot un-decide it, and the phrasing
        # call is the last billable work: falling through to the canned message
        # is what a cancel wanted anyway.
        _log.info("cancelled while phrasing the partial note for run %s; "
                  "using the canned message", run_id)
    except Exception:  # noqa: BLE001 — phrasing is best-effort
        pass
    if not message.strip():
        message = (
            "No fully validated answer could be produced for this question. "
            "The sources listed below were identified as the most relevant "
            "and may contain the material you need."
        )
    await _tele(run_id, "partial", cause=p.cause, explanation=p.explanation,
                message=message.strip()[:2000],
                validated_claims=len(validated_claims),
                sources=[fn for fn, _ in sources])
    await _mark(run_id, status="partial",
                error=None, completed_at=_now())
    _log.info("query run %s partial (%s)", run_id, p.cause)


async def _stage_retrieve_first(run_id: uuid.UUID) -> None:
    """Retrieval for full/retrieval modes via the retrieval-first engine
    (schema-aware plan + deterministic channels), replacing the retired
    agentic decompose/search path. Runs in-process; the
    stored result id lands on the run exactly as the merge stage used to."""
    from app import retrieval_first as rf

    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        question, effort = run.question, run.effort or "medium"
        if run.parent_result_id:  # resume: retrieval already stored
            return
    await _mark(run_id, status="retrieving")
    await _tele(run_id, "retrieval", started=_iso())
    # Between each step, not just at the stage boundary: these are the four
    # billable calls of the stage, and a checkpoint is only worth having
    # where it can still stop the next one from being made.
    plan = await rf.plan_question(question, effort=effort, run_id=run_id)
    await _check_cancel(run_id)
    ranked = await rf.retrieve_and_rank(plan)
    await _check_cancel(run_id)
    briefing = await rf.build_briefing(ranked, effort)
    await _check_cancel(run_id)
    rid = await rf.store_result(question, ranked, briefing, plan)
    await _mark(run_id, parent_result_id=uuid.UUID(str(rid)))
    await _tele(run_id, "retrieval", completed=_iso(),
                documents=len(ranked), queries=len(plan.get("queries") or []))


# ── entrypoint ──────────────────────────────────────────────────────────────


async def run_pipeline(run_id: uuid.UUID) -> None:
    """Drive one QueryRun to published/failed. Designed to be launched as an
    asyncio background task; safe to re-launch on a failed run (resume)."""
    sinas = _Sinas(run_id=run_id)
    await _mark(run_id, started_at=_now(), error=None)
    async with AsyncSessionLocal() as session:
        mode = (await session.get(QueryRun, run_id)).mode
    try:
        await _check_cancel(run_id)
        if mode in ("full", "retrieval"):
            await _stage_retrieve_first(run_id)
        if mode == "retrieval":
            await _mark(run_id, status="published", completed_at=_now())
            _log.info("query run %s retrieval published", run_id)
            return
        # synthesis mode requires parent_result_id supplied at creation
        await _check_cancel(run_id)
        await _stage_synthesize(run_id, sinas)
        await _check_cancel(run_id)
        await _stage_validate_publish(run_id, sinas)
        await _mark(run_id, status="published", completed_at=_now())
        _log.info("query run %s published", run_id)
    except CancelledOutcome as c:
        _log.info("query run %s cancelled", run_id)
        await _mark_cancelled(run_id, c)
        # Same call as partial and failed, and worth being clear about what it
        # buys: nothing, today. Cancellation's saving is entirely the calls the
        # checkpoints stopped from being made. This archives whatever the
        # retired pipeline recorded — currently nothing on a retrieval-first
        # run — and is where a real Sinas abort gets wired when it exists.
        try:
            async with AsyncSessionLocal() as session:
                run = await session.get(QueryRun, run_id)
                chat_ids = _chat_ids_for_cleanup(run.telemetry, run.searches)
            await _teardown_chats(sinas, chat_ids)
        except Exception:
            _log.warning("post-cancel chat teardown failed for run %s", run_id)
    except PartialOutcome as p:
        _log.warning("query run %s partial: %s", run_id, p)
        await _mark_partial(run_id, sinas, p)
        try:
            async with AsyncSessionLocal() as session:
                run = await session.get(QueryRun, run_id)
                chat_ids = _chat_ids_for_cleanup(run.telemetry, run.searches)
            await _teardown_chats(sinas, chat_ids)
        except Exception:
            _log.warning("post-partial chat teardown failed for run %s", run_id)
    except Exception as exc:
        _log.exception("query run %s failed", run_id)
        # str(exc) can be empty (e.g. httpx.ReadTimeout); keep the class name
        # so the run row never carries a blank error.
        await _mark(
            run_id,
            status="failed",
            error=(str(exc) or type(exc).__name__)[:2000],
            completed_at=_now(),
        )
        try:
            async with AsyncSessionLocal() as session:
                run = await session.get(QueryRun, run_id)
                chat_ids = _chat_ids_for_cleanup(run.telemetry, run.searches)
            await _teardown_chats(sinas, chat_ids)
        except Exception:
            _log.warning("post-failure chat teardown failed for run %s", run_id)
