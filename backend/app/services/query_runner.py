"""Server-supervised question pipeline (the query-side ingestion_runner).

The choreography of a question — retrieve, synthesize, validate, publish —
lives HERE, in code, with per-stage state checkpointed on the QueryRun row.
Agents are consulted for judgment only. Every consultation but one is a
stateless one-shot invoke; the exception is drafting, which is a single
conversation per answer (see `services/drafting_chat`), because a drafter
that cannot remember what it wrote cannot defend it and cannot reuse a
cached prefix.

  retrieve    the retrieval-first engine (app/retrieval_first): schema-aware
              plan + deterministic channels, in-process
  synthesize  sgr/retrieval-planner-agent — the argument plan as a one-shot,
              then ONE chat carrying the draft and every revision of it —
              and sgr/passage-extractor-agent (verbatim grounding extracts)
  verdicts    the stateless evidence-check fan-out (services/faithfulness),
              then sgr/answer-gate-agent judging the surviving answer

Every transition lands in QueryRun.telemetry. A failed run can be resumed:
completed stages short-circuit off the persisted state.
"""

from __future__ import annotations

import asyncio
import bisect
import html
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from app.auth import CallerIdentity
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import (
    AnswerClaim,
    ClaimEvidence,
    Document,
    DocumentClass,
    DocumentVersion,
    Result,
    ResultDocument,
)
from app.models.query import QueryRun
from app.services import (
    answer_render,
    answer_structure,
    claim_naming,
    declared_roles,
    drafting_chat,
    naming,
    objections,
    obligations,
    standing,
    strikes,
    supersession,
)

#: How many findings one round puts to the drafter. A round that named
#: thirty would be a redraft with extra steps, and the ones past the cap are
#: not asked about — which is also why they do not count as strikes.
MAX_FEEDBACK_ITEMS = 10

MAX_VALIDATE_ROUNDS = 2
# A round that reduced the failed count earns extra rounds, up to this cap —
# converging runs finish instead of dying at an arbitrary budget.
#
# Was 4 and 8, then 3 and 4. Measured on 18 September 2026: at 4 and 8, one
# question ran six review cycles and eight revision rounds, 42 minutes,
# roughly $13, and the cycles past the fourth dropped as many claims as
# they added, one of them the source the expert review had asked for. At 3
# and 4, four questions all ran to the fourth cycle at 18–31 minutes and
# about $10 each, and on the two that did not turn the fourth cycle gained
# no source. A loop still arguing after three cycles is not converging on a
# better answer; it is rewriting the one it has. The bound is the whole
# lever on both cost and time, and the cycle count is on every run's
# telemetry, so the effect of a change here is measured, not guessed.
HARD_VALIDATE_ROUNDS = 3
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

#: The agent that drafts and revises. Named once because the drafting
#: conversation is one chat belonging to it, and a second name here would be a
#: second chat.
DRAFTER_AGENT = "sgr/retrieval-planner-agent"

#: Where the drafting conversation's bookkeeping lives inside `telemetry`.
#: The chat ID ALSO lives in its own column (`query_run.synthesis_chat_id`),
#: which is the column the activity endpoint already reads; this carries what
#: a column cannot, which is how many rounds the conversation has run and what
#: each of them settled.
DRAFT_CHAT_KEY = "draft_chat"

# Hard per-run spend ceiling in USD, summed over the run's synthesis chat
# (which also carries remediation traffic — empirically where runaway spend
# lives; one observed run burned $23.81 there hunting unanchorable evidence).
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


class DrafterSilent(Exception):
    """The drafting call delivered no claims, and WHICH way it delivered none
    is the whole point of the class.

    A run whose drafter returned an empty reply used to end with "no passage
    supported a claim well enough to draft" — a verdict about the corpus,
    reported on behalf of a model that had never spoken. Two runs were lost
    to that reading, with fifty documents read and twelve passage groups
    verified behind it. `cause` says which of the three happened, and the
    caller turns it into the run's own cause so the record names it.
    """

    #: The model returned nothing, twice. Not a judgment about anything.
    SILENT = "drafter_returned_nothing"
    #: The model replied, in shape, with an empty list of claims. That IS a
    #: judgment, and a different one from "no passage was good enough".
    NO_CLAIMS = "drafter_offered_no_claims"
    #: Claims arrived and none survived normalisation — no text, or not
    #: objects at all.
    UNUSABLE = "drafter_claims_unusable"

    def __init__(self, explanation: str, cause: str = SILENT):
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
    return datetime.now(UTC)


def _iso() -> str:
    return _now().isoformat()


# Waits between invoke retries, in seconds. Deliberately long: the Anthropic
# SDK underneath already retries twice with sub-second backoff on anything
# >= 500, so a fast retry here would only repeat what it just exhausted. What
# gets through that and reaches us is an overload lasting longer than a couple
# of seconds, and these are sized for it. Two attempts, not more: an overload
# that survives twenty seconds is not going to yield to a third.
INVOKE_RETRY_WAITS = (5.0, 15.0)

#: Wall-clock ceiling for the uncovered-theme observation, which is
#: best-effort and sits on the path to required work. Under the general invoke
#: policy its two calls have a worst case near an hour; the median published
#: run is 559s. A first cut: `uncovered_themes_seconds` records what it
#: actually costs so this can be set from measurement.
_OBSERVATION_BUDGET_S = 120.0


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

    async def chat_send(self, chat_id: str, content: str,
                        agent: str = "") -> str:
        """One turn of an existing conversation, and the reply to it.

        The counterpart of `invoke` for a chat that outlives the call. Sinas
        replays the chat's stored history on every message, so the provider
        underneath sees a growing message list whose prefix does not change —
        which is the whole point: its rolling cache breakpoint sits on the
        last message, so each turn reads back the prefix the previous turn
        wrote instead of paying for it again.

        `agent` is passed for the retry telemetry only; the chat already
        knows which agent it belongs to.
        """
        return await self._retrying(
            agent or "chat",
            lambda c: c.post(f"{self.base}/chats/{chat_id}/messages",
                             headers=self.headers, json={"content": content}),
            reply_key="content")

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
        return await self._retrying(
            agent,
            lambda c: c.post(f"{self.base}/agents/{agent}/invoke",
                             headers=self.headers, json={"message": message}),
            reply_key="reply")

    async def _retrying(self, agent: str, request, reply_key: str) -> str:
        """One model-bearing POST, retried past a transient upstream failure.

        Shared by `invoke` and `chat_send` so the two cannot drift: both start
        model work, both cost money, and both have to behave the same way when
        the provider is overloaded.
        """
        last: Exception | None = None
        for attempt, wait in enumerate((*INVOKE_RETRY_WAITS, None)):
            try:
                async with httpx.AsyncClient(timeout=600.0) as c:
                    r = await request(c)
                    r.raise_for_status()
                    data = r.json()
                if attempt:
                    await _tele_invoke_retry(self.run_id, agent, attempt,
                                             last, recovered=True)
                await record_llm_call(self.run_id, data.get("chat_id"), agent)
                return data.get(reply_key, "") or ""
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
    for key, entry in (telemetry or {}).items():
        # The drafting conversation is excluded by name, and deliberately.
        # It is the one chat a run is meant to REJOIN: a failed run is
        # resumable, and the conversation holding the brief, the passages and
        # every argument the drafter has already made is the most expensive
        # thing the run owns. Archiving stops no work (see `_teardown_chats`),
        # so tearing it down could only cost a resume its memory.
        if key == DRAFT_CHAT_KEY:
            continue
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
    from app.services.document_identity import document_title_subquery

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Document.id,
                    Document.filename,
                    DocumentClass.name,
                    ResultDocument.reason,
                    Document.summary,
                    ResultDocument.rank,
                    DocumentClass.identifier_property,
                    document_title_subquery(),
                    DocumentClass.authority_label,
                    DocumentClass.standing,
                    DocumentClass.naming_required,
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
        # What every field of this deployment means to the engine: which
        # property of each class is its date, its jurisdiction, its status,
        # what replaced it and its second identifier, and which annotations
        # carry standing and the issuing body. Resolved once per manifest,
        # never per document, and never again downstream — the readers of
        # these rows are pure and could not resolve it if they wanted to.
        roles = await declared_roles.resolve(session)
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
    for (did, fn, cls, reason, summary, rank, ident_prop, title,
         class_label, class_standing, class_naming) in rows:
        values = (per_doc.get(did) or {}).get("values") or {}
        ann = "; ".join(
            f"{name}: {_fmt(v)}" for name, v in values.items() if v is not None
        )
        props = "; ".join(
            f"{n}: {_fmt(v)}"[:60] for n, v in (props_by_doc.get(did) or [])[:6]
        )
        # The same values unformatted, for the code that reads them rather
        # than the model: the currency rules, the source date, the tier.
        raw_props = {n: answer_structure.unwrap(v)
                     for n, v in (props_by_doc.get(did) or [])}
        out.append({
            "document_id": did, "filename": fn, "class": cls or "",
            "annotations": ann, "properties": props, "reason": (reason or ""),
            "summary": (summary or ""),
            "rank": rank,
            "briefing": briefing_by_doc.get(str(did)),
            "title": title,
            "props": raw_props,
            "annotation_values": values,
            # What this deployment declared, carried per row so the pure
            # readers stay pure — nothing downstream touches a session to
            # learn a name. The same object on every row: it is the
            # deployment's configuration, not the document's.
            "roles": roles,
            "identifier": (raw_props.get(ident_prop) if ident_prop else None),
            # What a claim citing this document says about the source, as
            # the class declares it. None for a class that declares none,
            # which is also what says the class carries rules on its own.
            "class_authority_label": class_label,
            # How high a source of this class stands, as the class declared
            # it. None for a class that declared nothing, and None is inert:
            # it neither satisfies the highest-standing rule nor breaches it.
            "class_standing": class_standing,
            # Whether a claim asserting a rule on this class must name it in
            # the sentence, as the class declared. False for a class that
            # declared nothing, which holds it to nothing.
            "naming_required": class_naming,
        })
    return out


# Where the planner's attention actually goes. Measured over 3,854 citations in
# published answers: 56% name a document in the top ten, 72% in the top twenty,
# and the median citation is rank 9. The list is still worth carrying to the end
# — 13% of citations come from below rank 40 — but not at a flat price per
# document, which spends the same on rank 3 as on rank 97.
HEAD_DOCUMENTS = 10
# The ten highest-ranked documents are the ones the planner builds from, and
# truncating them is what put the law in the part it never read: every summary
# in the corpus exceeds 200 characters, median length 973, and the opening is
# court, date and parties. 4,000 is above every summary there is, longest
# 1,525, so nothing real is cut. It is a bound rather than no bound so that one
# pathological summary cannot spend the whole budget and drop the working set
# behind it.
HEAD_SUMMARY_CHARS = 4000
TAIL_SUMMARY_CHARS = 100

# The other two columns of every line, and together they cost as much as the
# summaries. Measured over all 377 stored result sets that hold a full working
# set: the properties come to 20,152 characters on the set decomposed, as much
# as all 100 summaries together, with the retrieval reason behind them. Tuning
# the head and the tail alone cannot fit the list under the cap, because two
# thirds of it is not summary.
REASON_CHARS = 60
PROPERTY_CHARS = 120

# A table of contents, rendered as line ranges and titles rather than as the
# repr of its JSON. Half of what the old rendering carried was keys, quotes and
# braces, so this holds roughly the same titles in half the characters.
TOC_CHARS = 300


def _manifest_line(r: dict, summary_chars: int = HEAD_SUMMARY_CHARS) -> str:
    """One document as the planner sees it, bounded by its band's budget."""
    summary = r["summary"][:summary_chars]
    return (f"- {r['filename']} | {r['class'] or '-'} | "
            f"{r['annotations'] or '-'} | "
            f"{str(r.get('properties') or '-')[:PROPERTY_CHARS]} | "
            f"{r['reason'][:REASON_CHARS]} | {summary}")


def _toc_digest(toc, cap: int = TOC_CHARS) -> str:
    """A table of contents as `start-end title`, bounded.

    Stored as JSON and rendered with `str()`, 57% of the characters that
    reached the planner were structural: `{"line": 1, "level": 1, "title":
    "...", "line_to": 8487}`. The planner needs the titles and where they
    begin, so those are what it gets, and more of them fit.

    Never raises for the sake of a table of contents: anything unreadable is
    passed through truncated, which is what the old rendering did to
    everything.
    """
    if isinstance(toc, str):
        try:
            toc = json.loads(toc)
        except ValueError:
            return toc[:cap]
    entries = toc.get("entries") if isinstance(toc, dict) else (
        toc if isinstance(toc, list) else None)
    # A mapping of entries is still entries. Iterating a dict yields its keys,
    # every one a string, and the loop below drops non-dictionaries -- so a
    # mapping would render as nothing at all where the old `str(toc)` at least
    # showed the reader something. Not seen in this corpus, where all 5,722
    # stored tables of contents hold a list; cheap to not depend on that.
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not entries:
        return str(toc)[:cap]
    out: list[str] = []
    used = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        row = f"{e.get('line')}-{e.get('line_to')} {e.get('title') or ''}".strip()
        if used + len(row) + 2 > cap:
            break
        out.append(row)
        used += len(row) + 2
    return "; ".join(out)


# What the planner may be shown. The prompt slices to this, so a manifest
# that outgrows it loses its TAIL — the lowest-ranked documents vanish from
# the planner's view with nothing said. That is worth a number: on one
# measured run the manifest is already 50,218 characters, 84% of this, and the
# margin is one corpus away from being spent.
MANIFEST_CHAR_CAP = 60000


async def _doc_manifest(
    parent_id: uuid.UUID, cap: int = MANIFEST_CHAR_CAP
) -> tuple[str, dict]:
    """The planner's view of the working set, and what the cap cost to build it.

    Returns the text and a record: how many documents the result held, how
    many survived, and how many the cap dropped.

    Capped per document rather than by slicing the joined string. A slice cuts
    mid-line, so the last document the planner sees arrives with half a summary
    and no way to tell that is what happened; and counting what survived would
    then mean counting "- " prefixes in text that contains a summary with a
    newline in it (one of the 4,019 summaries in the corpus does). Assembling
    document by document makes both exact.

    Dropping the tail is the right behaviour when something must go: the rows
    arrive in rank order, so what is lost is what retrieval ranked last. The
    defect was never the dropping, only that it happened silently.
    """
    rows = await _manifest_rows(parent_id)
    # Position is rank only when there is a rank. A curated result -- documents
    # merged in, reached through the graph, or attached by hand -- can carry
    # rows with no rank at all, and one in this corpus has 178 of 178. Ordering
    # by a null column leaves the sequence arbitrary, so banding on position
    # would hand five times the budget to whichever ten happened to come first
    # and drop the rest of the tail on the same non-reason. Where the ranking is
    # incomplete, nothing is privileged: every document gets the tail budget and
    # the record says the banding did not apply.
    unranked = sum(1 for r in rows if r.get("rank") is None)
    lines: list[str] = []
    total = shown = 0
    ranked_seen = 0
    used = 0
    full = False
    for r in rows:
        total += 1
        # Per row, not per result. A ranked row keeps its place in the band; an
        # unranked one takes the tail budget rather than a place it has not
        # earned. Deciding this for the whole result would mean one attached
        # document stripping the head budget from every ranked one beside it,
        # which is worse than the problem: no result in this corpus mixes the
        # two, but merge and graph expansion are how one would.
        has_rank = r.get("rank") is not None
        if has_rank:
            ranked_seen += 1
        block = [_manifest_line(
            r, HEAD_SUMMARY_CHARS if (has_rank and ranked_seen <= HEAD_DOCUMENTS)
            else TAIL_SUMMARY_CHARS)]
        brief = r.get("briefing")
        if brief:
            props = brief.get("properties")
            if props:
                block.append(f"    properties: {json.dumps(props, ensure_ascii=False)[:400]}")
            toc = brief.get("toc")
            if toc:
                block.append(f"    toc: {_toc_digest(toc)}")
        # Exactly what this block adds to the joined string: its own lines,
        # the newlines between them, and one more to join it to what is
        # already there -- which the first block does not need. Charging a
        # newline per line instead counts one that `"\n".join` never writes,
        # which put `chars` one above the real length and made the effective
        # cap 59,999: a last document that fit exactly was refused.
        cost = (sum(len(x) for x in block) + len(block) - 1
                + (1 if lines else 0))
        # Once one document does not fit, nothing after it is taken either.
        # Letting a later, smaller one through would hand the planner a set
        # that is not the top of the ranking, which is a quieter defect than
        # the one this replaces. The loop still runs to the end so `dropped`
        # counts the whole tail rather than the point it began.
        if full or used + cost > cap:
            full = True
            continue
        used += cost
        shown += 1
        lines.extend(block)
    return "\n".join(lines), {
        "chars": used, "cap": cap, "unranked": unranked,
        "documents": total, "shown": shown, "dropped": total - shown,
    }


_sinas_usage_engine = None


async def _chats_cost_usd(chat_ids: list[str]) -> float:
    """Spend across these Sinas chats, USD, from llm_usage.

    Sinas shares the Postgres server with SGR (different database), so this
    is one cross-database read. Rates are per million tokens. The Anthropic
    figures are the published rate card; the Gemini ones are calibrated
    against this deployment's own ingestion bill, which the published tiers
    reproduce to within a few percent. Gemini used to fall through to the
    Sonnet branch — a 60x over-count on the agents that do most of the work.

    THE RATES MUST TRACK THE CARD, and for a while they did not. Opus stood
    at 5.0/25.0 and Sonnet at 2.0/10.0 — the previous generation's prices,
    left behind when the models moved. Opus was therefore counted at exactly
    a third of what it cost, and it is the model the publish gate runs on. A
    night of benchmark runs spent about $25 a question against a $10 ceiling
    without tripping it; the two runs that did trip it had spent near $30.
    A ceiling that measures a third of the spend is not a ceiling.

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
                          (prompt_tokens - cache_read_tokens - cache_write_tokens) * 15.0
                          + cache_write_tokens * 18.75 + cache_read_tokens * 1.50
                          + completion_tokens * 75.0
                        ELSE
                          -- sonnet-5 tier
                          (prompt_tokens - cache_read_tokens - cache_write_tokens) * 3.0
                          + cache_write_tokens * 3.75 + cache_read_tokens * 0.30
                          + completion_tokens * 15.0
                      END) / 1e6, 0)
                    FROM llm_usage
                    WHERE chat_id = ANY(CAST(:cids AS uuid[]))
                      AND error IS NULL"""),
                {"cids": chat_ids})
            return float(row.scalar() or 0.0)
    except Exception:  # noqa: BLE001 — fail open by design
        return 0.0


DRAFT_MODE = get_settings().sgr_draft_mode


async def _drafting_chat(run_id: uuid.UUID, sinas: _Sinas) -> drafting_chat.DraftingChat:
    """The run's drafting conversation, as the run row left it.

    Rebuilt rather than passed down the call stack, because the two stages
    that talk to the drafter — synthesis and validation — are separate entry
    points and a resumed run enters at the second one. The chat id lives on
    the RUN, not the answer: a run is the resumable unit, `run_pipeline` is
    handed a run id and nothing else, and the column that holds it is the one
    the activity endpoint already serves a conversation from.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        stored = dict(((run.telemetry or {}).get(DRAFT_CHAT_KEY)) or {}) if run else {}
        chat_id = (run.synthesis_chat_id if run else None) or stored.get("chat_id")
    chat = drafting_chat.DraftingChat(
        client=sinas, agent=DRAFTER_AGENT,
        max_exchanges=get_settings().sgr_draft_chat_exchanges)
    chat.restore(stored)
    chat.chat_id = chat_id
    return chat


async def _save_drafting_chat(run_id: uuid.UUID,
                              chat: drafting_chat.DraftingChat) -> None:
    """Write the conversation back, id and rounds together.

    Best-effort in the same sense as the rest of the bookkeeping: a write that
    fails costs the conversation its memory of how many rounds it has run,
    which degrades to the behaviour this replaces rather than failing a run
    that has an answer in it.
    """
    try:
        async with AsyncSessionLocal() as session:
            run = await session.get(QueryRun, run_id)
            if run is None:
                return
            tel = dict(run.telemetry or {})
            tel[DRAFT_CHAT_KEY] = chat.state()
            run.telemetry = tel
            flag_modified(run, "telemetry")
            run.synthesis_chat_id = (chat.chat_id or "")[:64] or None
            await session.commit()
    except Exception:  # noqa: BLE001
        _log.warning("could not record the drafting conversation for run %s",
                     run_id, exc_info=True)


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

# Emphasis, as markdown writes it down. The collection is markdown, and a
# paragraph stored as `**14.**&nbsp;The Court held` is read — and quoted, by a
# person or by a model — as `14. The Court held`. The asterisks are how the
# emphasis is spelled, not part of the sentence, so they fold away like the
# soft hyphen: one character for none.
#
# Measured before this existed: the verifier rejected 1,043 of 3,933 proposed
# passages, and on a sample of four rejections drawn from the stored runs
# three were present in the source, verbatim, on exactly the line the
# extractor named. They failed only because the stored text carries emphasis
# markers and HTML entities that no faithful copy of the rendered text has.
# The documents that carry the most of that markup are the judgments, which
# is why answers came to rest on commentary: the primary source's passages
# were extracted, then thrown away.
_EMPHASIS = {ord("*"): None, ord("_"): None}

_RENDERING_VARIANTS = {**_QUOTE_MARKS, **_DASHES, **_SOFT_HYPHEN, **_EMPHASIS}

#: A character reference, numeric or named. Deliberately strict: it must end
#: in a semicolon, so the ampersand in `AM & S Europe` is left alone.
_ENTITY = re.compile(
    r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


def _rendered_chars(text: str) -> list[tuple[str, int]]:
    """The text's characters as they are drawn, each with the offset in `text`
    it came from. Pure.

    Only entities expand here. `&nbsp;` is a space to every reader and to
    every copy; `&#8220;` is a quotation mark the variant table already folds,
    but only once it is a character rather than six. Everything else is passed
    through for the caller's own folding.

    The first character of an expansion carries the entity's opening offset
    and the last carries its closing one, so a span located through this map
    still covers the whole of what was written.
    """
    out: list[tuple[str, int]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "&":
            m = _ENTITY.match(text, i)
            if m and (rendered := html.unescape(m.group(0))) != m.group(0):
                last = m.end() - 1
                out.extend((c, i if k == 0 else last)
                           for k, c in enumerate(rendered))
                i = m.end()
                continue
        out.append((text[i], i))
        i += 1
    return out

# What a passage should be, in characters rather than lines.
#
# It used to be asked for in lines, "2-25 lines", which is not a unit: two
# lines is 90 characters in a short-lined document and over 2,000 in a
# long-lined one, so the same instruction asked for a sentence in one
# document and a page in another. Measured on the first run to store its
# quotes, a passage ran 2 to 2.7 times the length of the claim it supported.
#
# 200 because that is where a quote becomes wholly checked. The verifier and
# the locator compare only the first 200 canonical characters, so on a
# 460-character quote more than half the text is never compared against the
# source at all: see _quote_whole, which exists to notice that a quote ran
# past what was verified. Shorter is better verified, not worse.
#
# The floor is the real constraint and it is not arithmetic. A sentence can
# mean something else without the one before it, and "that requirement does
# not apply here" is verbatim, locatable and useless. Hence a floor with an
# anchor rule beside it rather than a floor alone.
_QUOTE_TARGET_CHARS = 200
_QUOTE_FLOOR_CHARS = 80


def _canonical_offsets(text: str) -> tuple[str, list[int]]:
    """Canonical text, and for each of its characters the offset in `text`
    that character came from.

    A match is found in canonical space and a coordinate has to be reported
    in the source, so something has to carry one back to the other. Folding a
    rendering variant is one character for one, dropping a soft hyphen is one
    for none, collapsing a run of whitespace is many for one, and lower-casing
    is occasionally one for two. Each of those breaks the assumption that the
    two strings run in step, which is why the map is built rather than the
    offsets recomputed.

    `_canonical` is written in terms of this, so the two can never disagree
    about what the canonical form is, in the way `_numbered_pairs` is shared
    by the verifier and the locator.
    """
    out: list[str] = []
    src: list[int] = []
    pending = False
    text = text or ""
    # The expansion pass costs a list the length of the document, and a
    # document without a single entity has nothing to expand, so it is skipped
    # where it would only allocate. This runs over full documents.
    drawn = (_rendered_chars(text) if "&" in text
             else [(ch, i) for i, ch in enumerate(text)])
    for ch, i in drawn:
        rep = _RENDERING_VARIANTS.get(ord(ch), ch)
        if rep is None:            # soft hyphen: nothing draws it, no copy has it
            continue
        if rep.isspace():
            pending = True
            continue
        if pending:
            # A run of whitespace stands as one space, and a leading run as
            # nothing, which is what `.strip()` did.
            if out:
                out.append(" ")
                src.append(i)
            pending = False
        for c in rep.lower():
            out.append(c)
            src.append(i)
    return "".join(out), src


def _canonical(text: str) -> str:
    """Text reduced to what a verbatim quote has to preserve.

    Case, the run-length of whitespace, and the rendering of quotes, dashes
    and invisible characters all vary between a source and a faithful copy
    of it. Nothing here removes, adds or reorders a word, a digit or a
    punctuation mark that separates words, so text that is not in the source
    still does not match text that is.
    """
    return _canonical_offsets(text)[0]


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


def _quote_lengths(out: list[dict]) -> dict:
    """What the extractor actually returned, in characters.

    Counted and not enforced. The instruction moved from lines to characters
    and nothing rejects a passage for its length, because the floor is a
    judgement about whether a quote can be read on its own and no character
    count decides that. What this is for is seeing the distribution move, or
    fail to, before anything is made binding on it.

    Reported against the target rather than as a bare average: an average
    hides the shape, and the shape is the question. A run whose quotes cluster
    at the target is the instruction working; one that keeps a long tail is
    the model quoting the paragraph whatever it was asked.
    """
    # Canonical length, not raw. The 200-character boundary this reports
    # against is applied after canonicalization, which folds rendering
    # variants, drops soft hyphens and collapses whitespace runs. Measuring
    # the raw string counts characters verification never sees, so a passage
    # with ragged spacing would be called over target while being wholly
    # checked.
    lens = sorted(len(_canonical(p.get("text") or ""))
                  for r in out for p in (r.get("passages") or []))
    if not lens:
        return {"passages": 0}
    # The middle of an even sample is between two values, not at the higher
    # of them. lens[n // 2] is the upper of the pair, which reports a longer
    # typical quote than the run produced and biases the number this exists
    # to watch.
    n = len(lens)
    mid = lens[n // 2] if n % 2 else (lens[n // 2 - 1] + lens[n // 2]) / 2
    return {
        "passages": len(lens),
        "median_chars": round(mid, 1),
        "mean_chars": round(sum(lens) / len(lens)),
        "longest_chars": lens[-1],
        # The two bands that matter, for opposite reasons: past the target a
        # quote stops being wholly verified, and under the floor it may not be
        # readable on its own.
        "over_target": sum(1 for n in lens if n > _QUOTE_TARGET_CHARS),
        "over_target_pct": round(
            100.0 * sum(1 for n in lens if n > _QUOTE_TARGET_CHARS) / len(lens)),
        "under_floor": sum(1 for n in lens if n < _QUOTE_FLOOR_CHARS),
        # How much text is carried past the point the verifier stops looking.
        # This is the finding the target answers, stated as a number per run.
        "chars_beyond_verified": sum(
            max(0, n - _QUOTE_TARGET_CHARS) for n in lens),
    }


def _quote_whole(numbered: str, line_from: int, line_to: int, quoted: str,
                 back: int = 2, fwd: int = 2) -> bool:
    """Whether the whole quote sits in the window, not just its opening.

    The verifier and the locator both compare only the first 200 canonical
    characters, so a long quote whose tail runs past `line_to + fwd` is
    accepted and its span recorded from that prefix alone. The span then
    starts in the right place and stops short of the text it cites.

    A tail running past the window is one way to get here. The other is a
    quote that stops being verbatim after its opening, spliced or paraphrased
    from that point on, which the 200-character comparison cannot see either.
    This does not separate the two, and says only that the whole quote was not
    found where the span points.

    This is a different axis from the narrowed/moved split, not a third
    bucket in it. That split asks whether the reported range held the quote;
    this asks whether the recorded span covers all of it. A span can be moved
    and also fall short, so the counters overlap by design and must not be
    added together. Kept separate because a reader wants both answers: where
    the citation points, and how much of the quote it reaches.

    Pure: text in, a verdict out.
    """
    full = _canonical(quoted)
    if len(full) < 20:
        return True
    rows = [t for n, t in _numbered_pairs(numbered)
            if line_from - back <= n <= line_to + fwd]
    return full in _canonical(" ".join(rows))


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

    if not rows:
        return None

    # The window is canonicalised ONCE and the quote found in it once, and the
    # lines it lands on are read back off the offset map. Narrowest still
    # wins: the span of a single occurrence IS the narrowest window that
    # contains it, so the answer is the same and the work is not.
    #
    # It used to scan every start line and, inside that, extend an accumulator
    # one line at a time, re-joining and re-canonicalising it at each step —
    # quadratic in lines and cubic in characters. That is harmless on the
    # ten-line window a passage normally claims and ruinous on a wrong one:
    # the extractor's claimed ranges run to 1,967 lines, the collection holds
    # 347 documents over half a megabyte and one of 5.5MB, and this function
    # is called on the event loop. A single passage with a wild line range
    # therefore stopped the server — measured at 55 minutes of one core with
    # every concurrent run frozen behind it, which is how it was found.
    joined = "\n".join(t for _, t in rows)
    canon, src = _canonical_offsets(joined)
    starts, off = [], 0
    for _, t in rows:
        starts.append(off)
        off += len(t) + 1

    def window(want: str) -> tuple[int, int] | None:
        at = canon.find(want)
        if at < 0:
            return None
        first = bisect.bisect_right(starts, src[at]) - 1
        last = bisect.bisect_right(starts, src[at + len(want) - 1]) - 1
        return first, last

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


def _locate_chars(content: str, line_from: int, line_to: int,
                  quoted: str) -> tuple[int, int] | None:
    """Where the quote sits in `content`, as character offsets, or None.

    The line locator answers which lines hold the quote; this answers which
    characters, which is a different question wherever a line is a paragraph.
    Measured on the evidence rows held on 10 September 2026, a line span
    covers 1,547 characters on average and 84% of them cover more than 400,
    so a citation resolves to about a page where the text it rests on is a
    sentence. Everything that reads a span back reads it at that precision:
    the passage a reviewer is shown in the workbook, the passage the reviser
    is given when the gate asks for a revision, and the export.

    Offsets are absolute in `content`, matching how an entity mention records
    its span, so one convention covers both.

    Pure: text in, a pair of offsets out.
    """
    lines = (content or "").split("\n")
    if line_from < 1 or line_to < line_from or line_to > len(lines):
        return None
    want = _canonical(quoted)
    if len(want) < 20:            # the floor the line locator already applies
        return None
    base = sum(len(ln) + 1 for ln in lines[:line_from - 1])
    canon, src = _canonical_offsets("\n".join(lines[line_from - 1:line_to]))
    at = canon.find(want)
    if at < 0:
        # Verification matches on the first 200 characters, so a quote stored
        # longer than it was checked still locates on what was guaranteed.
        want = want[:200]
        at = canon.find(want)
        if at < 0 or len(want) < 20:
            return None
    return base + src[at], base + src[at + len(want) - 1] + 1


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
        moved = 0
        prefix_only = 0
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
                        + "} — max 4 passages, text EXACTLY as printed "
                        "(without the line-number prefixes).\n"
                        + f"Quote the sentence that states the point, not the "
                        f"paragraph around it: about {_QUOTE_TARGET_CHARS} "
                        f"characters, and not less than {_QUOTE_FLOOR_CHARS}. "
                        "Where a sentence needs the one before it to mean what "
                        "it says, take both: a fragment that cannot be read on "
                        "its own is worse than a long quote, and the length is "
                        "the target rather than the rule.\n"
                        # The source's own label for the paragraph is the only
                        # thing that lets a reader find the passage again, and
                        # "quote the sentence, not the paragraph" was trimming
                        # it off the front of every quote. Nothing downstream
                        # can put it back: this system does not know how a
                        # given source numbers itself, and a number it derived
                        # would be a number the source never wrote.
                        + "Where the passage shows the source's own label for "
                        "the paragraph the sentence sits in — a bare number, "
                        '"r.o. 4.2", "recital 14" — begin the quote with that '
                        "label, exactly as printed. It is how a reader finds "
                        "the passage again, and trimming it loses it for "
                        "good. Where the passage shows none, do not invent "
                        "one: begin at the sentence.\n\n"
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
                # Recorded where the quote is, not where it was said to be.
                # The tolerance is what lets a faithful quote through; the
                # coordinates are what a reader needs to be true, and they
                # become the persisted evidence span. Falls back to the
                # reported pair only if the window cannot be narrowed, which
                # verification already says should not happen.
                found = _locate_passage(shown[fn]["text"], lf, lt, text)
                alf, alt = found or (lf, lt)
                # The owed document is shown every round, so the same span can
                # come back more than once. Deduplicated on the corrected span,
                # the one persisted, so two reports that locate to the same
                # text are one passage and not two.
                if (fn, alf, alt) in seen_spans:
                    continue
                seen_spans.add((fn, alf, alt))
                # Everything below is counted after the duplicate is dropped,
                # so it shares a denominator with `passages_verified`. Counted
                # above it, these described proposals while that field
                # described retained passages, and a ratio of the two was a
                # ratio of nothing: 15 of the 229 proposals in the two runs
                # that first reported these numbers were duplicates.
                # Would the old forward-only rule have kept this one? The
                # answer is the whole point of making the range symmetric, and
                # it can only be asked where the passage is judged.
                if not _verify_passage(shown[fn]["text"], lf, lt, text, back=0):
                    recovered += 1
                if found and (alf, alt) != (lf, lt):
                    corrected += 1
                    # Two different things wear the same number. Narrowing a
                    # range that already held the quote improves how a citation
                    # reads; moving one that did not is a citation that pointed
                    # at lines without the text it cited. Only the second is a
                    # provenance defect, and only the second became possible
                    # when the range stopped being forward-only. Under the old
                    # rule an accepted quote was always inside
                    # [line_from, line_to + 2], so the reported start could
                    # never fall after it.
                    if alf < lf or alt > lt:
                        moved += 1
                # A separate axis, deliberately overlapping the two above
                # rather than excluding them: those ask whether the reported
                # range held the quote, this asks whether the recorded span
                # covers all of it. A moved span can also fall short. Do not
                # add these together.
                if not _quote_whole(shown[fn]["text"], lf, lt, text):
                    prefix_only += 1
                good.append({"filename": fn, "line_from": alf, "line_to": alt,
                             "text": text[:2000]})

        # Only a failure with nothing to show for it is an error: one bad round
        # out of several still leaves the claim grounded.
        if last_error and not good:
            return {"n": c.get("n"), "passages": [], "proposed": proposed_total,
                    "read": len(docs), "error": last_error,
                    "rejected": rejected, "recovered_by_symmetry": recovered,
                    "spans_corrected": corrected, "spans_moved": moved,
                    "spans_prefix_only": prefix_only,
                    "truncated": truncated, "chunked": chunked}
        return {"n": c.get("n"), "establishes": c.get("establishes"),
                # Which part of the question the plan filed this under, so
                # the drafter sees each passage group beside the part it
                # was read for.
                "part": c.get("part"), "conclusion": bool(c.get("conclusion")),
                "passages": good, "proposed": proposed_total,
                "rejected": rejected, "recovered_by_symmetry": recovered,
                "spans_corrected": corrected, "spans_moved": moved,
                "spans_prefix_only": prefix_only,
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
                "quote_lengths": _quote_lengths(out),
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
                # The half of `spans_corrected` that is a provenance defect:
                # the reported range did not contain the whole quote. The rest
                # is narrowing, which only makes a citation read better.
                "spans_moved": sum(r.get("spans_moved", 0) for r in out),
                # Only the quote's first 200 characters were found, so the
                # span does not cover all of what it cites. Either the quote
                # runs past the window, or it stops being verbatim after its
                # opening. Overlaps `spans_moved` on purpose: this is coverage,
                # that is location, and a span can fail both.
                "spans_prefix_only": sum(
                    r.get("spans_prefix_only", 0) for r in out),
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


async def _synthesis_playbook(role: str = "drafting") -> str:
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

    # House style is not a reason to lose a run. Three stages read this now,
    # and one of them -- argument planning -- promises to fail open, so a
    # database that cannot be reached must cost the rules and nothing else.
    # Announced rather than swallowed: an unreadable playbook and an absent
    # one produce the same empty string, and only the log tells them apart.
    try:
        async with AsyncSessionLocal() as session:
            content = (await session.execute(
                select(Playbook.content).where(Playbook.kind == "synthesis")
                .limit(1))).scalar_one_or_none()
    except Exception:
        _log.exception("playbook unreadable; %s proceeds without house rules",
                       role)
        return ""
    if not content:
        return ""
    # Applied AFTER the structure rules and said to be beneath them. The
    # deployment's playbook governs wording and emphasis; the section order,
    # the per-part conclusions, the tests as conditions and the labelling of
    # secondary sources are the engine's and a house rule cannot lower them.
    return (f"\n\nHOUSE RULES for {role} (how to write — wording and emphasis. "
            "They never change the STRUCTURE rules above: section order, "
            "conclusion first per part, tests as ordered conditions, labelled "
            "authorities. And they say nothing about what is true — only the "
            "passages decide that):\n" + content.strip() + "\n")


#: What the reduced ask allows itself, as a fraction of the normal cap. The
#: retry exists because the first reply never arrived; asking for most of the
#: answer again is asking for the same failure again.
RETRY_CLAIM_FRACTION = 0.5
#: And never fewer than this, or the retry cannot carry a conclusion per part.
RETRY_MIN_CLAIMS = 4


#: The kinds a drafter may choose, as the contract prints them, built from
#: the taxonomy rather than spelled out beside it: the three schemas below
#: listed the values by hand and drifted apart from `CLAIM_KINDS` twice.
#: `abstention` is deliberately not offered — a drafter that cannot answer
#: says so through the refusal path, not by writing a claim.
_KIND_ALTERNATIVES = "|".join(
    k for k in answer_structure.CLAIM_KINDS if k != "abstention")
#: What each of those words means, once, in the brief. The names are the
#: engine's and say nothing about any one corpus, so a drafter that has not
#: been told what they mean will guess from its own domain — which is how
#: `factual` collected everything the model was unsure of.
_KIND_GLOSS_BLOCK = "\n".join(
    f'- "{k}": {answer_structure.CLAIM_KIND_GLOSS[k]}'
    for k in answer_structure.CLAIM_KINDS if k != "abstention")


#: The shape the drafter replies in. Six fields it used to author are gone:
#: `section`, which follows from `kind`; `authority_label`,
#: `jurisdiction_note` and `currency_note`, which follow from the cited
#: document; `position`, which follows from the reply's order; and the `test`
#: wrapper, whose `conditions` are now flat on the claim. Each was a decision
#: the engine could make from what it already had, and every one of them was
#: output the model had to spend before it could emit a single claim.
_DRAFT_SCHEMA = (
    '{"claims": [{"n": <1, 2, ...>, "text": "<claim, one sentence>", '
    '"part": <part number or null>, "kind": '
    f'"{_KIND_ALTERNATIVES}", '
    '"follows_from": [<n of each claim this one reasons from; required '
    'for inference and conclusion claims>], '
    '"rationale": "<why this claim rests on this source>", '
    '"evidence": [{"filename": "...", "line_from": <int>, '
    '"line_to": <int>, "locator": "<or null>"}], '
    '"test_name": "<for kind test only: what the test is called>", '
    '"conditions": [<for kind test only>{"text": "<condition, in the '
    'source\'s order>", "cumulative": true|false, "evidence": '
    '{"filename": "...", "line_from": <int>, "line_to": <int>, '
    '"locator": "<or null>"}}]}]}'
)


#: The shape a correction comes back in.
_PATCH_SCHEMA = (
    '{"revise": [{"seq": <int>, "text": "<claim>", '
    f'"part": <part number or null>, "kind": "{_KIND_ALTERNATIVES}", '
    '"follows_from": [<seq>, ...], '
    '"rationale": "<why this claim rests on this source>", '
    '"evidence": [{"filename": "...", "line_from": <int>, "line_to": <int>, '
    '"locator": "<or null>"}]}], '
    '"drop": [{"seq": <int>, "rationale": "<what the claim asserted '
    'and why no passage available can carry it>", '
    '"objection": "<the id of the request this answers, or omit>"}], '
    '"keep": [{"seq": <int>, "rationale": "<why the current citation '
    'stands despite the feedback>", '
    '"objection": "<the id of the request this answers, or omit>"}], '
    '"refuse": [{"objection": "<the id from a feedback line>", '
    '"rationale": "<why this source cannot carry the point asked of it>"}], '
    '"waive": [{"doc": "<filename>", "rationale": "<why, having read '
    'its passages, this OWED document does not carry any point this '
    'answer needs>"}], '
    f'"add": [{{"text": "<claim>", "type": "{_KIND_ALTERNATIVES}", '
    '"part": <part number or null>, "follows_from": [<seq>, ...], '
    '"test_name": "<for a test claim only>", '
    '"conditions": [<for a test claim only>{"text", "cumulative", '
    '"evidence": {"filename", "line_from", "line_to", "locator"}}], '
    '"rationale": "<why this claim rests on this source>", '
    '"evidence": [{"filename": "...", "line_from": <int>, '
    '"line_to": <int>, "locator": "<or null>"}]}]}'
)


def _revision_contract() -> str:
    """How every later round is answered — stated once, in the brief. Pure.

    None of this changes between rounds, so none of it belongs on a round.
    It used to be re-sent in full every time, in a call that could not
    remember having been told it before; here it sits in the cached prefix and
    a round carries only what that round found.
    """
    return (
        "HOW THE ROUNDS AFTER THIS ONE WORK (read now; it will not be "
        "repeated).\n"
        "Once you have drafted, a review reads the answer and I send you what "
        "it found — and nothing else. Your own claims are not read back to "
        "you: they are above, in your own reply, and they are your position. "
        "What each round carries is the review's findings, the requests "
        "outstanding against the answer, and the engine's numbering of the "
        "claims so you can address them.\n\n"
        "You answer a round with a PATCH, never with the whole answer. Change "
        "ONLY what the feedback identifies; a claim you do not mention is kept "
        "exactly as it is. Do not restate, rephrase or return an untouched "
        "claim.\n\n"
        "For a claim the feedback does identify: NARROW it if it asserts more "
        "than its passages establish, REWRITE it if it contradicts another "
        "claim, and ABANDON it — drop it, with a reason — if no passage "
        "available can carry it. Abandoning is a normal move and often the "
        "right one. A claim you wrote in your first reply has no standing "
        "just because you wrote it: rewording a claim the passages cannot "
        "support leaves the same defect in different words, and the round "
        "after this one will find it again. Add a claim only where the answer "
        "fails to address the question. Every claim you write must be carried "
        "entirely by passages in this brief, and you may cite only those.\n\n"
        "Dropping a claim costs a reason, like every other change: say what "
        "the claim asserted and why no passage available can carry it. A drop "
        "with no reason is not applied and the claim stays.\n\n"
        "The structure is part of what is corrected. Where the feedback says "
        "a part lacks its conclusion, or a conclusion sits in the analysis, "
        'RESTORE THE ORDER: add or revise a claim of kind "conclusion" for '
        "that part, never append a conclusion to the end of the analysis. "
        'Every claim you add or revise states its "part" and "kind", and an '
        'inference or conclusion its "follows_from". Where the feedback names '
        "two claims restating one proposition from one source, merge them: "
        "revise one to carry the point and both citations, drop the other "
        "with a reason.\n\n"
        "THERE ARE THREE ANSWERS TO A REQUEST, NOT TWO. Obeying and ignoring "
        "were the only two moves you used to have, so reasoning that should "
        "have retired a request went into a dropped claim where nothing read "
        "it and the same source came back twice more. A feedback line that "
        "carries an objection id can be REFUSED with a reason, and a refusal "
        "is a reply: the review must either accept it — the point is then "
        "settled and never comes back — or press it with something it has not "
        "said before. Refuse when the source cannot carry what is asked of "
        "it, in terms of what the source IS and what the point NEEDS, never "
        "in terms of effort. Where the feedback names a stronger source and "
        "you judge the current citation to be the better one, say so rather "
        'than changing nothing: put the claim in "keep" with a rationale '
        "giving the reason, having actually read the named source's "
        "passages.\n\n"
        "A feedback line marked as an OWED source is an obligation, not a "
        "suggestion: either cite that document in a revised or added claim, "
        'or list it in "waive" with a rationale you could only give after '
        "reading its passages, or refuse it. An obligation neither cited nor "
        "waived nor refused comes back every round.\n\n"
        "Give a RATIONALE with every claim you revise, add or keep, on the "
        "same terms as a drafted one.\n\n"
        "What a source IS — what kind of thing it is, how current it is, "
        "which jurisdiction it belongs to — is never yours to write: it is "
        "filled in from the document you cite.\n\n"
        "A round is answered with JSON and nothing else — no preamble, no "
        "reasoning outside the JSON. Begin with { and end with }:\n"
        + _PATCH_SCHEMA + "\n\n"
    )


def _draft_ask() -> str:
    """Turn two: the work. Pure.

    Short by construction — everything it needs was in the brief. It is a
    separate turn so that the brief can be replayed into a fresh chat without
    a whole draft being produced and discarded; see `services/drafting_chat`.
    """
    return (
        "Now draft the claims, following the brief above.\n\n"
        "Reply ONLY JSON, and nothing else — no preamble, no explanation, no "
        "reasoning outside the JSON. Begin with { and end with }:\n"
        + _DRAFT_SCHEMA + "\n"
        'Omit "test_name" and "conditions" on every claim that is not a test.')


def _shorter_draft_ask(cap: int) -> str:
    """The same ask, smaller, after a reply that never arrived. Pure.

    What it drops is how much the model is asked to produce, which is what ran
    out. What it does NOT drop is the passages — and it no longer has to carry
    them, because they are the brief and the brief is still in the
    conversation.
    """
    n = max(RETRY_MIN_CLAIMS, int(cap * RETRY_CLAIM_FRACTION))
    return (
        "Your previous reply was EMPTY — nothing came back at all. The most "
        "likely reason is that you ran out of room before writing any of it. "
        "Answer the same task again, smaller. The brief has not changed and "
        "the passages are still above; do not ask for them again.\n"
        f"- Write at most {n} claims, not {cap}. Cover the conclusions first "
        "and stop; a short answer is wanted, an absent one is not.\n"
        "- Keep each claim to one sentence and each rationale to one short "
        "clause.\n"
        "- Write NOTHING outside the JSON object: no plan, no commentary, no "
        "working. Your first character is { and your last is }.\n"
        "- Do not re-read every passage group. Take the groups that most "
        "directly answer each part and leave the rest.\n\n"
        "The reply shape is unchanged:\n" + _DRAFT_SCHEMA)


def _claims_json(reply: str) -> dict:
    """The drafter's reply as JSON, or JSONDecodeError naming what broke."""
    cleaned = (reply or "").strip().strip("`").removeprefix("json").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise json.JSONDecodeError("no JSON object in reply", cleaned or "", 0)
    return json.loads(cleaned[start:end + 1])


def _verified_quote(verified: dict, filename: str, evidence: dict) -> str:
    """The checked passage behind one citation, or "" if it cannot be pinned.

    Exact coordinates first, because that is what the drafter was shown. A
    drafter that widens or narrows a range still cites the same passage, so a
    single overlapping passage in the same document is accepted; two or more
    are not, since choosing between them would be a guess and a quote located
    into the wrong sentence is worse than no offset at all.
    """
    try:
        lf = int(evidence.get("line_from"))
        lt = int(evidence.get("line_to") or lf)
    except (TypeError, ValueError):
        return ""
    exact = verified.get((filename, lf, lt))
    if exact:
        return exact
    overlapping = [t for (fn, a, b), t in verified.items()
                   if fn == filename and t and not (b < lf or lt < a)]
    return overlapping[0] if len(overlapping) == 1 else ""


def _locator_of(evidence: dict) -> str | None:
    """The paragraph label an evidence entry claims, or None.

    A label, never a number this system worked out: it is whatever the
    source prints — "42", "r.o. 4.2", "recital 14" — and it is stored
    unchecked, because the check belongs where the span text is (the
    faithfulness pass), not where the model's reply is read. Capped at the
    column width; a "locator" that is a sentence is not a locator. Pure.
    """
    raw = evidence.get("locator")
    if raw is None or isinstance(raw, bool) or isinstance(raw, (list, dict)):
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none", "n/a"):
        return None
    return s[:50]


def _evidence_entries(claim: dict, limit: int = 4) -> list[dict]:
    """The evidence entries that are objects, which is all a caller can read a
    filename off.

    The drafter's reply is unvalidated here, and two malformed shapes arrive
    from a model often enough to matter: `"evidence": ["a.md"]`, where the
    entry is a bare string with no `.get`, and `"evidence": "a.md"`, where
    slicing the value yields characters. Both raised AttributeError out of the
    drafting transaction, which rolled the draft back and failed the whole run
    over one malformed field in one claim. Losing the run is a worse outcome
    than losing the claim.

    Sliced before filtering, so "at most `limit` entries are considered" keeps
    the meaning it had: a claim that sends six entries does not get more of
    them read by sending two bad ones.
    """
    entries = claim.get("evidence")
    if not isinstance(entries, (list, tuple)):
        return []
    return [e for e in entries[:limit] if isinstance(e, dict)]


def _no_text_record(sequence: int, claim: dict) -> dict:
    """What to keep about a claim the drafter sent with no text.

    Enough to tell the two cases apart later. An empty placeholder carries
    nothing but its sequence, and the gap it leaves in the numbering is the
    only casualty. A claim whose text failed to arrive while its reasoning and
    its sources did carries both, and the answer is short a proposition it
    meant to make. The claim itself is not kept: `carried` names which fields
    arrived with something in them, which is what separates the two, and the
    rationale is capped because this sits in telemetry beside everything else.
    """
    return {
        "sequence": sequence,
        "carried": sorted(k for k in claim
                          if claim.get(k) not in (None, "", [], {})),
        "rationale": str(claim.get("rationale") or "")[:400],
        "evidence": [str(e.get("filename") or "")
                     for e in _evidence_entries(claim)],
        # Written every time, zero included. `carried` says the field arrived
        # with something in it and `evidence` says what could be read off it,
        # so without this a claim whose evidence was all malformed and one
        # whose evidence was absent read the same in the record, and the
        # record exists to tell cases apart.
        "evidence_unreadable": sum(
            1 for e in (claim.get("evidence") or [])[:4]
            if not isinstance(e, dict)
        ) if isinstance(claim.get("evidence"), (list, tuple)) else 0,
        "type": str(claim.get("type") or "")[:50],
    }


async def _draft_from_extracts(
    run_id: uuid.UUID, answer_id: uuid.UUID, chat: drafting_chat.DraftingChat,
    question: str, extracts: list[dict], append: bool = False,
    cap: int | None = None, parts: list[dict] | None = None,
    sources: dict[str, dict] | None = None,
) -> int:
    """Inverted split, writing half: the drafter writes all claims from the
    verified extracts; the runner persists claims and evidence rows itself.

    This is turn one and turn two of the conversation that will carry the
    whole answer. Turn one is the BRIEF — everything that stays true for the
    life of the answer — and turn two asks for the draft. The split is what
    makes the brief replayable: see `services/drafting_chat`.

    The drafter writes to the structure: it files every claim under a part
    of the question, leads each part with its conclusion, states a
    multi-condition test as one claim of ordered conditions, and labels what
    each source is. Above each passage group it is shown what the documents
    ARE — title, class, tier, issuing body, date, jurisdiction, currency —
    which is for labelling and never for asserting: a claim may still say
    only what its passages show."""
    # Grounding is on raw source text only. The plan's "establishes" sentence
    # is written from the manifest — summaries, classes, annotations — which
    # are themselves interpretation produced at ingestion, unverified and
    # never checked against anything. Handing it to the drafter let it assert
    # what a summary said: one answer attributed an Opinion to the Advocate
    # General who wrote it, correctly, from a summary rather than from any
    # passage, so the attribution was true and uncheckable. The plan decides
    # what to READ; only what was read may be asserted.
    # The passage the extractor verified, keyed by the coordinates the drafter
    # is shown. The drafter echoes those coordinates back, so the quote behind
    # a citation is recoverable without asking it to copy the text again, and
    # what is recorded is what was checked rather than what came back.
    parts = parts or []
    sources = sources or {}
    cap = cap or MAX_CLAIMS
    verified: dict[tuple[str, int, int], str] = {}
    for e in extracts:
        for p in e.get("passages") or []:
            verified[(str(p["filename"]), int(p["line_from"]),
                      int(p["line_to"]))] = str(p.get("text") or "")

    blocks = []
    for i, e in enumerate(extracts, start=1):
        if not e.get("passages"):
            continue
        # What the documents in this group ARE, once each, above the
        # passages. The line carries no summary and no holding, and nothing
        # the drafter has to copy back: it says which document this is, what
        # kind of thing it is, and whether its class labels it — the one fact
        # that changes what the drafter may rest on it.
        seen_fn: list[str] = []
        for p in e["passages"]:
            if p["filename"] not in seen_fn:
                seen_fn.append(p["filename"])
        heads = "\n".join(
            f"  SOURCE {fn}: {(sources.get(fn) or {}).get('line') or 'no record'}"
            for fn in seen_fn)
        ps = "\n".join(
            f"  [{p['filename']} lines {p['line_from']}-{p['line_to']}]\n"
            f"  {p['text']}" for p in e["passages"])
        part = e.get("part")
        where = (f" (for part {part + 1})" if isinstance(part, int) else
                 " (for the overall conclusion)" if e.get("conclusion") else "")
        blocks.append(f"PASSAGE GROUP {i}{where}\n{heads}\n{ps}")
    if not blocks:
        return 0
    brief = (
        f"You are drafting the claims of {_domain_article()}answer, and then "
        "correcting them round by round as a review comes back. This message "
        "is the BRIEF: everything in it stays true for the whole of our "
        "conversation and is never repeated. What arrives later is only what "
        "is new.\n\n"
        f"Draft the claims of {_domain_article()}answer from the VERIFIED "
        "PASSAGES below "
        "— these passages are the ONLY thing you know. Every claim must be "
        "supported entirely by the passages you cite for it. Name no deciding "
        "body, no case reference and no date that no passage shows. Skip a "
        "passage group that establishes nothing usable.\n"
        # The instruction used to be that the FINAL claim states the
        # conclusion, and it was followed: across every answer produced, no
        # claim typed as a conclusion has ever been first and they sit on
        # average 84% of the way through. A reader who wants the answer has to
        # read to the end for it, which is the reviewer's first must-have and
        # the one the answer has never met. Claim 1 is the only position a
        # renderer cannot lose and a reader cannot miss. With the question in
        # parts the rule is the same one, per part: the overall conclusion is
        # claim 1, each part's conclusion leads that part.
        "The FIRST claim states the answer to the question, in one sentence, "
        "before any reasoning or authority: a reader who stops there has the "
        "answer. The claims after it give the reasoning, then the detail. Do "
        "not repeat the conclusion at the end.\n\n"
        "Each group opens with SOURCE lines saying what its documents ARE: "
        "title, class, and a `labelled:` note where the source is one that "
        "cannot carry a rule alone. Never assert anything from a SOURCE line "
        "in a claim, and never copy one into a field — everything the answer "
        "prints about what a source IS is filled in for you.\n\n"
        + _structure_rules(parts, cap)
        + "\nFor each claim also give a RATIONALE: ONE short sentence, at most "
        "20 words — which part of the question this answers and why this "
        "source settles it. Never restate the claim; the reader has just "
        "read it. It is reasoning, not evidence — nothing in it may assert "
        "anything the passages do not show. Name a source the way the claim "
        "names it, never by its filename. One claim per proposition per "
        "source: do not restate a point a claim already makes from the same "
        "document.\n\n"
        # The locator is the drafter's, because the drafter is the only stage
        # that has both the passage and the proposition in front of it. It is
        # checked deterministically against the passage before anything is
        # judged, so a label that is not there costs the citation — which is
        # the whole reason it is safe to print one at all.
        + 'The "locator" of an evidence entry is the source\'s OWN label for '
        "the paragraph the passage sits in, copied from the passage exactly "
        'as it prints it: "42", "r.o. 4.2", "recital 14". Where the passage '
        "shows no such label, write null. Never derive one, never count "
        "paragraphs, never carry one over from another passage: a locator "
        "that does not appear in the passage it labels is checked and the "
        "citation is thrown away with it.\n\n"
        # The standing half of every revision round, moved off the round and
        # into the brief. It never changes, so it belongs in the part of the
        # conversation that is written to cache once and read back after; what
        # a round then carries is only what that round found.
        + _revision_contract()
        + await _synthesis_playbook()
        + "\nQUESTION:\n" + question + "\n\n" + "\n\n".join(blocks)
    )
    await chat.start(brief)
    reply = await chat.ask(_draft_ask())
    # Two things can come back that are not claims, and they are different
    # failures. An UNPARSEABLE reply has claims in it and broke on a quote;
    # resending it to be repaired is the right and cheap move. An EMPTY reply
    # has nothing to repair, and that is what a run lost a full answer to:
    # the drafter returned no text at all, the repair prompt was handed an
    # empty PREVIOUS REPLY, and the model — correctly, given what it was
    # shown — answered `{"claims": []}`. The run then reported that no
    # passage supported a claim, which was false: no reply had arrived.
    #
    # So the retry for an empty reply is the WORK again, not a repair of
    # nothing, with the ask cut down to what a shorter reply can hold. That is
    # also the shape of the cause. The reply was empty because the model spent
    # its whole output budget before writing any of it (20,000 completion
    # tokens, zero text blocks, twice), so a retry that asks for less output is
    # a retry that can finish.
    #
    # In a conversation neither retry has to carry anything back: the passages
    # are the brief, which is still there, and the previous reply is the
    # previous message. The repair turn used to resend up to 60,000 characters
    # of broken JSON for the model to read its own words off.
    data: dict | None = None
    try:
        data = _claims_json(reply)
    except json.JSONDecodeError as exc:
        empty = not (reply or "").strip()
        await _tele(run_id, "draft",
                    draft_reparse=str(exc)[:200],
                    draft_reply_chars=len(reply or ""),
                    draft_empty_reply=empty,
                    draft_brief_chars=len(brief))
        if empty:
            _log.warning("run %s: the drafter returned an empty reply to a "
                         "%d-character brief; retrying with a reduced ask",
                         run_id, len(brief))
            reply = await chat.ask(_shorter_draft_ask(cap))
            await _tele(run_id, "draft",
                        draft_retry="reduced_ask",
                        draft_retry_reply_chars=len(reply or ""))
        else:
            reply = await chat.ask(
                "Your previous reply was not valid JSON: " + str(exc)[:200]
                + ". Send the same claims again as strictly valid JSON. Escape "
                'every quotation mark inside a string as \\", and use no line '
                "breaks inside a string. Do not redraft and do not change a "
                "claim: repair the reply you have just written.")
            await _tele(run_id, "draft", draft_retry="repair_json")
        try:
            data = _claims_json(reply)
        except json.JSONDecodeError as exc2:
            # Named for what happened. A drafter that said nothing twice is
            # not a corpus that supports no claim, and the run must not
            # report it as one.
            await _tele(run_id, "draft",
                        draft_retry_failed=str(exc2)[:200],
                        draft_retry_empty=not (reply or "").strip())
            raise DrafterSilent(
                "the drafter returned no usable reply twice"
                + (" (both replies were empty)" if not (reply or "").strip()
                   else f" (second reply: {str(exc2)[:120]})")) from exc2
    # The drafter's reply is unvalidated at every level, and this guard is
    # placed once at the boundary rather than a level at a time. Three
    # findings arrived in three review rounds, each the same defect one level
    # up: an evidence entry that is not an object, a claim that is not an
    # object, and a `claims` value that is not a list, where slicing it raised
    # TypeError before any per-item check could run. Guarding the shape where
    # the reply enters is what stops the fourth level arriving as a fourth
    # round.
    claims = data.get("claims")
    bad_container = "" if isinstance(claims, list) else (
        "absent" if claims is None else type(claims).__name__)
    if not isinstance(claims, list):
        claims = []
    no_text: list[dict] = []
    malformed: list[dict] = []
    # Normalised, then ORDERED before numbering: conclusions lead, analysis
    # follows part by part, authorities close. The drafter was told to write
    # in that order; the row order does not depend on whether it did. The
    # drafter's own number `n` travels with each claim: it is what
    # `follows_from` refers to and what the no-text record is keyed by.
    prepared: list[dict] = []
    for i, c in enumerate(claims[:(cap or 14)], start=1):
        if not isinstance(c, dict):
            # The same rule as the evidence entry below, one level up, and
            # the one this change first missed: the drafter's reply is
            # unvalidated, so `claims` can carry a bare string or a null
            # beside perfectly good claims. `c.get` on it raised
            # AttributeError out of the transaction, which rolled back
            # every valid claim written before it and failed the run over
            # one malformed item. A malformed claim costs the claim.
            malformed.append({"sequence": i, "repr": repr(c)[:200]})
            continue
        text_ = str(c.get("text") or "").strip()
        cols = answer_structure.normalise_claim(c, parts) if text_ else None
        if cols is None:
            # A claim with no text is dropped and its number goes with it,
            # which is why published answers jump from 9 to 11. Keep what
            # the drafter actually sent, because the gap alone cannot say
            # which of two things happened: an empty placeholder, where the
            # numbering is the only casualty, or a claim whose text failed
            # to arrive while its reasoning and its sources did, where the
            # answer is short a proposition it meant to make. Once the
            # reply is discarded the two are indistinguishable, and nothing
            # else records that a claim was dropped at all.
            no_text.append(_no_text_record(i, c))
            continue
        spans = _evidence_entries(c)
        if cols["claim_kind"] == "test":
            spans = spans + answer_structure.condition_spans(
                answer_structure.raw_test(c))
        cols["_n"] = c.get("n") if c.get("n") is not None else i
        cols["_spans"] = spans
        prepared.append(cols)
    ordered = answer_structure.order_claims(prepared)
    written = 0
    drafted: list[tuple[int, set]] = []
    async with AsyncSessionLocal() as session:
        start_seq = 1
        if append:
            start_seq = ((await session.execute(
                select(func.max(AnswerClaim.sequence))
                .where(AnswerClaim.answer_id == answer_id)
            )).scalar() or 0) + 1
        id_by_n: dict[int, uuid.UUID] = {}
        pending_refs: list[tuple[AnswerClaim, list[int]]] = []
        for i, cols in enumerate(ordered, start=start_seq):
            spans = cols.pop("_spans")
            n = cols.pop("_n")
            refs = cols.pop("follows_from_refs")
            row = AnswerClaim(answer_id=answer_id, sequence=i, **cols)
            session.add(row)
            await session.flush()
            try:
                id_by_n[int(n)] = row.id
            except (TypeError, ValueError):
                pass
            if refs:
                pending_refs.append((row, refs))
            # What actually became a citation, not what the drafter offered.
            # Two things drop evidence between the two: the cap at four, and a
            # filename that resolves to no document. `plan_outcome` reads this
            # to decide whether a planned claim was used, and a claim counted
            # as used on a citation that was never written is the one reading
            # the record must not produce.
            cited_here: set[str] = set()
            # Four spans is enough for a claim; a test is one span per
            # condition and needs room for each.
            capped = spans[:4] if cols.get("claim_kind") != "test" else spans[:8]
            first_doc_fn: str | None = None
            for ev_ in capped:
                fn_ = str(ev_.get("filename") or "")
                doc = (await session.execute(
                    select(Document).where(Document.filename == fn_)
                )).scalars().first()
                if doc is None:
                    continue
                span = {"line_from": ev_.get("line_from"),
                        "line_to": ev_.get("line_to"),
                        "char_from": None, "char_to": None,
                        "note": ev_.get("note"),
                        # As the drafter read it off the passage, unchecked.
                        # The faithfulness check finds it in the span text or
                        # fails the span, and only then does it become the
                        # row's `paragraph_ref`.
                        "locator": _locator_of(ev_)}
                quote = _verified_quote(verified, fn_, ev_)
                if quote:
                    ver = await session.get(DocumentVersion,
                                            doc.current_version_id)
                    at = _locate_chars(getattr(ver, "content_md", "") or "",
                                       int(span["line_from"]),
                                       int(span["line_to"] or span["line_from"]),
                                       quote)
                    if at:
                        span["char_from"], span["char_to"] = at
                session.add(ClaimEvidence(
                    claim_id=row.id, document_id=doc.id,
                    document_version_id=doc.current_version_id,
                    span=span, quote=(quote or None) and quote[:2000],
                    validated=False))
                cited_here.add(fn_)
                first_doc_fn = first_doc_fn or fn_
            # What the code knows about the source, rather than what the
            # model was asked to guess. All four of these used to be fields
            # of the reply — the label out of an eight-word vocabulary in
            # engine code, the tier, the jurisdiction note and the currency
            # note — and the engine overrode two of them afterwards anyway,
            # from exactly these rows. Asking for them bought nothing and
            # cost the drafter output it turned out not to have.
            #
            # Keyed on the FIRST cited document, which is the one the claim
            # rests on: a claim citing a labelled source alongside an
            # unlabelled one is a claim resting on the unlabelled one, and
            # that is the order the drafter is told to write them in.
            src = sources.get(first_doc_fn or "") or {}
            if src.get("label"):
                row.authority_label = str(src["label"])[:40]
            if src.get("tier") is not None:
                row.authority_tier = int(src["tier"])
            if src.get("jurisdiction"):
                row.jurisdiction_note = str(src["jurisdiction"])[:300]
            if src.get("currency"):
                row.currency_note = str(src["currency"])[:500]
            written += 1
            drafted.append((i, cited_here))
        for row, refs in pending_refs:
            ids = [str(id_by_n[r]) for r in refs if r in id_by_n and id_by_n[r] != row.id]
            row.follows_from = ids or None
        await session.commit()
    detail: dict[str, Any] = {
        "extract_mode": True, "claims": written,
        "claims_by_part": answer_structure.part_counts(
            ordered, len(parts), key="part_index")}
    if not append:
        detail["plan_outcome"] = _plan_outcome(extracts, drafted)
    if no_text:
        # Named for the cause, not reusing `dropped_claims`, which is a flat
        # count under `validate` meaning something else. One prefix per
        # meaning, as the removal record says.
        detail["no_text_claims"] = no_text
        detail["no_text_count"] = len(no_text)
        _log.warning("run %s: drafter returned %d claim(s) with no text; "
                     "sequence numbers %s are absent from the answer",
                     run_id, len(no_text), [d["sequence"] for d in no_text])
    if bad_container:
        # Its own key. An empty answer because the drafter sent no claims and
        # an empty answer because it sent something that was not a list of
        # them are different failures, and a run that drafted nothing needs to
        # say which.
        detail["claims_not_a_list"] = bad_container
        _log.warning("run %s: drafter returned `claims` as %s, not a list; "
                     "no claims were written", run_id, bad_container)
    if malformed:
        # Its own key, not folded into no_text_claims. A claim that arrived as
        # a string and one that arrived as an object with an empty text field
        # are different failures of the drafter, and the record exists to tell
        # cases apart.
        detail["malformed_claims"] = malformed
        detail["malformed_count"] = len(malformed)
        _log.warning("run %s: drafter returned %d item(s) in claims that are "
                     "not objects; sequence numbers %s are absent from the "
                     "answer", run_id, len(malformed),
                     [d["sequence"] for d in malformed])
    await _tele(run_id, "draft", **detail)
    # An answer with no claims is not a corpus with no support. The drafter
    # was shown passage groups — this function returns early when there are
    # none — so a reply that yielded no row is the drafter's doing, and the
    # run says which of its ways it was rather than blaming the passages.
    if written == 0:
        if not claims:
            raise DrafterSilent(
                "the drafter replied in shape with an empty list of claims "
                f"over {len(blocks)} passage group(s)"
                + (f"; `claims` arrived as {bad_container}" if bad_container
                   else ""),
                cause=DrafterSilent.NO_CLAIMS)
        raise DrafterSilent(
            f"the drafter sent {len(claims)} claim(s) and none could be "
            f"stored ({len(no_text)} with no text, {len(malformed)} not "
            "objects)",
            cause=DrafterSilent.UNUSABLE)
    return written


def _plan_outcome(extracts: list[dict], drafted: list[tuple[int, set]]) -> list[dict]:
    """What became of each planned claim.

    The plan numbers its claims and the answer numbers its claims, and nothing
    joined the two. A planned claim could be extracted, shown to the drafter
    and left out of the answer entirely, and the only way to find out was to
    read the plan, the extraction record and the citations side by side --
    which took nine queries to establish for one claim on one run.

    Three outcomes, and only the middle one is a surprise:

      no_passages       the extractor returned nothing for it, so its group
                        was skipped and the drafter never saw it
      extracted_unused  passages were extracted and shown, and no drafted
                        claim cites any of their documents
      used              at least one drafted claim cites a document it read

    Attribution is by document, not by identity, because nothing carries an
    identity across the drafting call: passage groups are renumbered after the
    empty ones are skipped, and the drafter is not asked which group a claim
    came from. Two planned claims anchored on the same document therefore both
    read as used when one of them was. That asymmetry is deliberate and worth
    stating: `used` can be wrong, `extracted_unused` cannot. A planned claim
    reported unused had none of its documents cited by anything.

    THE SECOND WAY `used` CAN BE WRONG, AND WHY IT IS NOT REPAIRED HERE. This
    is written at drafting, under the `draft` stage, and drafting is not the
    end of the run. Validation, coverage repair and the final sweep can remove
    the claim that carried the citation, or rebind its evidence, after which a
    planned claim recorded `used` has its document in no published claim.
    Measured over the runs stored on 10 September 2026: of 240 published runs,
    5 lost a document's last citation somewhere after drafting, so the record
    overstates `used` in about 2% of them. It is not repaired here because the
    plan is out of scope by the time the answer publishes, and because "did
    the plan reach the draft" and "did the plan reach the published answer"
    are two questions, not one bad answer to a single question.

    `cited_by_draft_sequence` is named for the numbering it holds. Sequences
    are compacted at publish, so a run that dropped any claim renumbers the
    survivors and these numbers no longer address the published answer: 45 of
    those 240 runs dropped at least one claim. The plain name `cited_by` read
    as an index into the answer a reader was holding, which it is not.
    """
    out: list[dict] = []
    for e in extracts:
        files = {str(p.get("filename") or "") for p in (e.get("passages") or [])}
        files.discard("")
        cited = sorted(seq for seq, evf in drafted if files & evf)
        out.append({
            "n": e.get("n"),
            "passages": len(e.get("passages") or []),
            "documents": sorted(files),
            "cited_by_draft_sequence": cited,
            "state": ("no_passages" if not files
                      else "used" if cited else "extracted_unused"),
        })
    return out


def _structure_rules(parts: list[dict], cap: int) -> str:
    """The order an answer is written in, as every drafting prompt states it.

    Conclusion first is a DRAFTING rule, not a rendering nicety: the drafter
    writes each part's answer before its reasoning, and the reasoning as a
    chain — the governing rule, then its application, then the step that
    follows — so a reader meets the answer and then sees why. These rules
    sit ABOVE the deployment's playbook, which governs wording and emphasis
    and cannot lower them. Pure.
    """
    n = len(parts)
    per_part = (
        f"The question has {n} part(s), listed below. Every claim names the "
        'part it answers ("part": the part number, or null for the overall '
        "conclusion). Each part gets at least "
        f"{answer_structure.MIN_CLAIMS_PER_PART} claims; the whole answer at "
        f"most {cap}.\n" if n else
        f"The whole answer holds at most {cap} claims.\n")
    return (
        "STRUCTURE (binding, and above any house rules):\n" + per_part
        + '1. Conclusion first. One claim of kind "conclusion" per part, '
        "stating that part's answer, and one overall conclusion "
        '("part": null) as the FIRST claim. Conclusions lead; nothing '
        "precedes them, and no later claim restates them.\n"
        "2. Then the reasoning per part, as a chain: the governing rule "
        "(evidenced) before its application (evidenced), then the step that "
        "follows from them. Each claim reads on from the previous one in its "
        "part.\n"
        "3. A test a source states as two or more conditions is ONE claim of "
        'kind "test" listing the conditions in the order the source states '
        "them, each condition pinned to its own passage, saying whether they "
        "are cumulative.\n"
        '4. A reasoning step that rests on earlier claims rather than on a '
        'passage is a claim of kind "inference": it carries no evidence, names '
        'the claims it follows from in "follows_from", and introduces no '
        "authority those claims do not carry. A conclusion also names what it "
        'follows from in "follows_from".\n'
        '5. A claim of kind "label" states what a source is and holds; those '
        "close the answer.\n"
        "6. A source whose SOURCE line shows a `labelled:` note never carries "
        "a rule on its own: a rule rests on a source with no such note, or is "
        "stated as what the labelled source says. You never write the label "
        "out — it is printed for you.\n"
        "\nWHAT EACH KIND MEANS (the kind says how the claim stands to its "
        "source, nothing about the subject matter):\n"
        + _KIND_GLOSS_BLOCK + "\n"
        + (("PARTS OF THE QUESTION:\n" + answer_structure.parts_block(parts) + "\n")
           if parts else "")
    )


async def _argument_plan(
    sinas: _Sinas, run_id: uuid.UUID, question: str, manifest: str,
    parts: list[dict] | None = None, cap: int | None = None,
) -> tuple[str, list[dict]]:
    """Split drafting: a strong tool-less model designs the ARGUMENT (which
    claims, anchored where) from the briefing manifest alone; the drafter
    then executes claim by claim. Judgment is expensive and small; reading
    and writing are cheap and large — price each accordingly (17 Aug:
    memory-padding lived entirely in the deciding, never the writing).
    Fail-open: any planning failure returns "" and drafting proceeds
    exactly as before.

    Given the question's parts, the plan allocates claims per part — each
    planned claim names the part it answers — and is capped by the parts'
    budget rather than a flat number. Planned claims that answer no part
    are dropped before anything is read for them."""
    parts = parts or []
    cap = cap or MAX_CLAIMS
    try:
        reply = await sinas.invoke(
            "sgr/retrieval-planner-agent",
            "Design the argument for answering the question below, using ONLY "
            "the documents listed. Reply ONLY JSON:\n"
            '{"claims": [{"n": 1, "part": <part number, or null for the overall '
            'conclusion>, "kind": "<' + _KIND_ALTERNATIVES +
            '>", "establishes": "<one sentence: what this '
            'claim must establish>", "anchors": ["<filename>", ...], '
            '"hint": "<which part of the anchor documents to read, from their '
            'TOCs>"}]}\n'
            f"Rules: at most {cap} claims; every claim anchored to at least one "
            "listed document; never anchor to anything not listed; the FIRST claim "
            "must state the overall conclusion, and no later claim restates "
            "it; every claim names the part of the question it answers, and a "
            "claim that answers no part is not planned. Plan each part's "
            "conclusion, then per part the governing rule before its "
            "application. If the documents cannot "
            "support a part of the question, plan NO claim for it — the gap "
            "will be reported honestly downstream.\n"
            + _structure_rules(parts, cap)
            # The planner designs claims the drafter has to execute, and the
            # drafter is told to skip a group that establishes nothing usable.
            # Planning against rules the drafter is not held to produces claims
            # that are researched and then correctly declined: on 28 measured
            # runs, 23% of the planned claims the answer never reached fell in
            # a class the deployment's own rules tell the drafter to decline,
            # against 2% of those it used. Costing four extraction calls to
            # read documents for a claim that cannot be written is the waste
            # this removes, and the rules that decide it are the deployment's.
            + await _synthesis_playbook("planning the argument")
            + "\nQUESTION:\n" + question + "\n\nDOCUMENTS:\n" + manifest,
        )
        cleaned = reply.strip().strip("`").removeprefix("json").strip()
        data = json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])
        claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
        if not claims:
            # Both halves, like every other exit. Returning the bare string
            # here made the caller's `_plan_text, plan_claims = await ...`
            # raise ValueError on a planner that replied `{"claims": []}`, so
            # the run was recorded as failed. The caller already handles an
            # empty plan on the next line, and treats it as a judgment about
            # the corpus rather than a crash; that branch was unreachable
            # through this path.
            return "", []
        # Filter before reading: a planned claim that answers no part of the
        # question is extraction spent on material the answer cannot use.
        # Recorded, so a planner that keeps answering questions nobody asked
        # is visible. With no decomposition nothing is dropped.
        claims, dropped = answer_structure.filter_plan_to_parts(claims, parts)
        if dropped:
            await _tele(run_id, "draft", plan_dropped_no_part=[
                {"n": c.get("n"), "establishes": str(c.get("establishes") or "")[:200]}
                for c in dropped])
        if not claims:
            return "", []
        thin = answer_structure.thin_parts(claims, parts)
        if thin:
            await _tele(run_id, "draft", plan_parts_thin=[
                {"index": p["index"], "label": p["label"]} for p in thin])
        # And the other end of the same fault. A minimum per part was enforced
        # and a maximum was not, so a plan could satisfy every rule and still
        # be lopsided — 3/4/11 and 5/4/10 across published runs, the last part
        # taking half the answer while the others sat at the floor. Reported
        # here, where the plan is still a plan and the drafter can spend the
        # budget differently, rather than discovered in the rendered answer.
        crowded = answer_structure.crowded_parts(claims, parts)
        if crowded:
            await _tele(run_id, "draft", plan_parts_crowded=[
                {"index": p["index"], "label": p["label"]} for p in crowded])
        lines = []
        for c in claims[:cap]:
            anchors = ", ".join(str(a) for a in (c.get("anchors") or [])[:4])
            hint = str(c.get("hint") or "").strip()
            lines.append(
                f"{c.get('n')}. {str(c.get('establishes') or '').strip()}"
                f"\n   anchors: {anchors}" + (f"\n   read: {hint}" if hint else "")
            )
        await _tele(run_id, "draft", argument_plan=[
            {"n": c.get("n"), "part": c.get("part"), "kind": c.get("kind"),
             "establishes": c.get("establishes"),
             "anchors": c.get("anchors")} for c in claims[:cap]])
        return (
            "ARGUMENT PLAN (realize these claims in order; read each claim's "
            "anchor documents, write the claim, bind its evidence; do not add "
            "claims beyond the plan):\n" + "\n".join(lines) + "\n\n"
        ), claims[:cap]
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


def _source_context(rows: list[dict]) -> dict[str, dict]:
    """Per filename: the line the drafter is shown above the document's
    passages, and the four facts about the source the engine writes onto
    every claim citing it. Pure over `_manifest_rows` rows.

    The label is the document class's own, declared by the deployment; the
    tier, the date behind a currency comparison, the status and the
    jurisdiction all come from what the rows carry as `roles` — resolved in
    `_manifest_rows` from the deployment's declarations, so no annotation or
    property name appears here; the jurisdiction note and the
    currency note are decided across the whole retrieved set, because both
    are comparisons — one against what the other sources are, the other
    against what else in the set is about the same instrument or case.

    Rows that carry no declarations — a caller that built them by hand — get
    the empty set, which is every rule that needs one switched off rather
    than any of them guessed.

    `standing` rides here too and is not one of the four: nothing is written
    onto a claim from it. It is the class's declared rank, and it is in this
    dict because this is already the one place that turns the retrieved set
    into per-filename facts, and the standing gate needs exactly that shape.
    """
    roles = next((r["roles"] for r in rows if r.get("roles")),
                 declared_roles.NONE)
    currency = answer_structure.currency_notes(rows, roles)
    jurisdiction = answer_structure.jurisdiction_notes(rows, roles)
    out: dict[str, dict] = {}
    for r in rows:
        fn = r.get("filename")
        if not fn:
            continue
        label = str(r.get("class_authority_label") or "").strip() or None
        out[fn] = {
            "line": answer_structure.source_context_line(r, label),
            "label": label,
            "tier": answer_structure.tier_of(
                r.get("annotation_values"),
                roles.tier_annotations if r.get("roles") else None),
            "jurisdiction": jurisdiction.get(fn),
            "currency": currency.get(fn),
            # The class's declared rank, carried through unchanged so the
            # standing gate compares two integers and never reads a class
            # name. None where the class declared none, which is inert.
            "standing": r.get("class_standing"),
        }
    return out


async def _fix_question_parts(
    run_id: uuid.UUID, answer_id: uuid.UUID, split: list[dict]
) -> list[dict]:
    """The run's decomposition as the structure uses it: [{index, label,
    text}], written on the answer row and under `draft` in telemetry.

    The split itself is `_question_parts`: one call on the question alone,
    stable across cycles, and the gate judges the same list. The heading is
    the splitter's; this only numbers the parts and records them where the
    drafter, the renderer and the API read them. A resumed run reads what the
    first one wrote.
    """
    from app.models import Answer

    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        stored = ((run.telemetry or {}).get("draft") or {}).get("question_parts")
    if isinstance(stored, list) and stored:
        return [p for p in stored if isinstance(p, dict)]
    parts = answer_structure.parse_parts(
        {"parts": [p for p in (_split_part(x) for x in split) if p]})
    await _tele(run_id, "draft", question_parts=parts,
                claim_cap=answer_structure.claim_cap(len(parts)) if parts else MAX_CLAIMS)
    if parts:
        async with AsyncSessionLocal() as session:
            row = await session.get(Answer, answer_id)
            if row is not None:
                row.question_parts = parts
                await session.commit()
    return parts


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

    manifest, manifest_cap = await _doc_manifest(parent_id)
    # Recorded whether or not anything was dropped: a run that fits is the
    # evidence the margin still exists, and only a stored number can say when
    # it stops existing.
    await _tele(run_id, "draft", manifest_cap=manifest_cap)
    if manifest_cap["dropped"]:
        _log.warning(
            "manifest cap dropped %s of %s documents for run %s (%s of %s chars)",
            manifest_cap["dropped"], manifest_cap["documents"], run_id,
            manifest_cap["chars"], manifest_cap["cap"])
    # The manifest is navigation: it decides which documents are worth
    # reading. Nothing it says may become a claim — summaries, classes and
    # annotations are interpretation produced at ingestion and verified
    # against nothing. Only extracted, verbatim-checked passages ground an
    # answer.
    # Observed here rather than at the gate, and for two reasons. The gate's
    # verdict path is a split then a judgment, and inserting a third call into
    # it makes the observation look like part of the verdict, which it is not.
    # And this is the point the observation is actually about: the question
    # and what retrieval returned, before anything is drafted. Failures are
    # swallowed except a cancel, which is control flow and must reach the
    # runner.
    # Bounded, because it is optional and the general policy is not. Two
    # invokes at three attempts each, a 600s client timeout and the 5s and 15s
    # retry waits, is a worst case near an hour, against a median published
    # run of 559s and a p90 of 1103s over the 200 stored on 10 September 2026.
    # Optional work that can outlast the run it observes is not optional.
    #
    # The split is the first of the two calls and the gate needs it later
    # anyway, so a timeout here costs the cache, not the split: the gate
    # recomputes it at the point where it is required rather than best-effort.
    #
    # The budget is a first cut. `uncovered_themes_seconds` is written on
    # every run so the next reading of it comes from measurement rather than
    # from this comment.
    observed_from = time.monotonic()
    # The split is needed before planning now, not only by the observation:
    # the planner allocates claims to the parts and the drafter files every
    # claim under one. So it is made outside the observation's budget, and
    # only the theme observation is bounded.
    parts_now = await _question_parts(sinas, run_id, question)
    try:
        async with asyncio.timeout(_OBSERVATION_BUDGET_S):
            await _uncovered_themes(sinas, run_id, question, parts_now, manifest)
    except CancelledOutcome:
        raise
    except TimeoutError:
        _log.warning("uncovered-theme observation exceeded %.0fs for run %s",
                     _OBSERVATION_BUDGET_S, run_id)
        await _tele(run_id, "validate", uncovered_themes_not_observed="over budget")
    except Exception:  # noqa: BLE001
        _log.warning("uncovered-theme observation failed for run %s", run_id)
        await _tele(run_id, "validate", uncovered_themes_not_observed="failed")
    await _tele(run_id, "validate",
                uncovered_themes_seconds=round(time.monotonic() - observed_from, 1))

    # The question's parts, decided before anything is planned: the planner
    # allocates claims to them, the drafter files every claim under one, and
    # the gate judges each. The budget follows from the count.
    parts = await _fix_question_parts(run_id, answer_id, parts_now)
    cap = answer_structure.claim_cap(len(parts)) if parts else MAX_CLAIMS
    _plan_text, plan_claims = await _argument_plan(
        sinas, run_id, question, manifest, parts=parts, cap=cap)
    if not plan_claims:
        # the planner ran and produced nothing usable: that IS a judgment
        # about the sources, unlike a transport failure, which raises above
        raise PartialOutcome(
            "no_progress", "no argument plan could be formed from the result")

    # Widen each planned claim's anchors with the documents whose own text
    # matches it. The planner picks from summaries and tops out at the few it
    # names, so an authority that is retrieved but not summarised in those
    # terms is never opened.
    all_rows = await _manifest_rows(parent_id)
    corpus_rows = all_rows[:60]
    for c in plan_claims:
        named = [str(a) for a in (c.get("anchors") or [])]
        extra = [d for d in _relevant_docs(str(c.get("establishes") or ""),
                                           corpus_rows, take=4)
                 if d not in named]
        c["anchors"] = named + extra

    await _tele(run_id, "draft", started=_iso())
    extracts = await _extract_passages(sinas, plan_claims, run_id)
    # What each document IS, for the drafter to label sources by: never a
    # summary, never a holding. The currency notes are decided here from the
    # retrieved set — a superseded status, a later ruling in the same case —
    # and set on the claims whether or not the drafter repeats them.
    sources = _source_context(all_rows)
    # One conversation for the whole answer, opened here and continued by
    # validation. A resumed run finds its chat id on the run row and rejoins
    # rather than starting a second conversation about the same answer.
    chat = await _drafting_chat(run_id, sinas)
    try:
        n = await _draft_from_extracts(run_id, answer_id, chat, question,
                                       extracts, cap=cap, parts=parts,
                                       sources=sources)
    except DrafterSilent as exc:
        # The run's cause is the drafter's, not the corpus's. "no_progress"
        # with "no passage supported a claim" is what this used to say about
        # a model that had returned an empty reply, and reading the record
        # afterwards it was indistinguishable from a genuinely thin corpus.
        _log.warning("run %s: %s", run_id, exc)
        raise PartialOutcome(exc.cause, exc.explanation) from exc
    finally:
        # In a `finally` because the chat exists the moment the brief is sent,
        # and a run that dies after that has still spent money in it. An
        # unrecorded chat id is a conversation nobody can resume, read or
        # bill.
        await _save_drafting_chat(run_id, chat)
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


def _coverage_summary(parts: list[dict]) -> dict:
    """Counts over the parts the gate called covered, for reading without
    walking every part.

    `only_*` rather than `any_*`: a part covered by three claims of which one
    lacks evidence is not the finding. A part every one of whose named claims
    lacks evidence is. Pure.
    """
    cov = [p for p in parts if p.get("covered")]

    def only(key: str) -> int:
        # Length equality already excludes a part that named a claim which does
        # not exist: a missing sequence is never put in the unsupported or
        # unresponsive lists, so the two lengths cannot match while one is
        # there. Guarding on `covered_by_missing` as well would be a condition
        # that can never fire.
        return sum(1 for p in cov
                   if (p.get("covered_by")
                       and len(p.get(key) or []) == len(p.get("covered_by") or [])))

    return {
        "parts": len(parts),
        "covered": len(cov),
        "naming_nothing": sum(1 for p in cov if not p.get("covered_by")),
        "naming_missing": sum(1 for p in cov if p.get("covered_by_missing")),
        "only_unsupported": only("covered_by_unsupported"),
        "only_unresponsive": only("covered_by_unresponsive"),
    }


def _closing_record(data: dict, claims_by_seq: Mapping[int, uuid.UUID],
                    parts: list[dict]) -> dict:
    """Where the answer's concluding claim is, recorded and blocking nothing.

    A reviewer's finding on one question was that the answer ends off-topic
    with no conclusion. Read across eleven runs carrying gate telemetry, two
    end on a claim that answers the question, two arguably do, and seven end
    on a source note or a procedural aside. So the shape is real and common.

    It cannot be derived from `covered_by`. Measured on those eleven, the union
    of every part's `covered_by` names 11 of 11, 13 of 14, 14 of 14 claims —
    nearly all of them, trailing source notes included, because a note about
    a source genuinely does bear on the part that source speaks to.
    Membership says a claim relates to the question; it says nothing about
    which claim discharges it. Hence a separate reading.

    `no_conclusion` already exists in the verdict and already blocks, through
    `correctness`. Two things are wrong with relying on it alone. It is a
    boolean, so "no conclusion anywhere" and "the conclusion is claim 9 of 14"
    are the same answer, and those want different remedies: the first is a
    missing claim, the second is an ordering defect that a rewrite would be the
    wrong instrument for. And it records nothing when false, so a run cannot be
    asked whether the gate considered the question at all. It has fired in 35
    runs from before gate-cycle telemetry existed and in none of the 39 since,
    while at least three of the eleven read by hand end with no conclusion
    anywhere. One of those three has two sibling runs on the same question that
    both close on a claim stating the answer, and it simply has none.

    So both are recorded: the gate's own boolean, and the sequence it puts the
    conclusion at. Where they disagree is the measurement worth having.

    `single_part` rides along because it decides whether any of the coverage
    machinery meant anything on this run. A question that decomposes to one
    part cannot fail coverage — `parts: 1, covered: 1` is the whole check, and
    every `only_*` counter is computed over covered parts, so all of them are
    inert. Four of 34 runs decompose that way, one of them reproducibly across three
    references. On those runs this record is the only whole-answer signal
    there is, which is the argument for keeping it.

    Blocks nothing, like `_audit_coverage` before it: recorded so the next
    batch can say how often each shape happens, and the decision comes after.

    Pure.
    """
    last = max(claims_by_seq) if claims_by_seq else None
    raw = data.get("concludes_at")
    at = None
    if not isinstance(raw, bool) and raw is not None:
        try:
            n = float(raw)
        except (TypeError, ValueError):
            n = None
        # A sequence naming no claim in the answer is not a location. The gate
        # can return one: `covered_by_missing` exists because it does.
        if n is not None and n.is_integer() and int(n) in claims_by_seq:
            at = int(n)
    return {
        "last": last,
        "concludes_at": at,
        # The same claim, by identity. A sequence is a position in a list the
        # run is still editing: a drop renumbers everything after it, and
        # `_compact_claim_sequences` closes the gaps at publication, so a
        # number recorded mid-run can name a different claim in the answer a
        # reviewer reads. Measured on one run: the gate recorded 12, the
        # claim at 11 was dropped, and the conclusion published as claim 11
        # while 12 became a claim about sealed envelopes. The number is kept
        # because it is the gate's own reading and the disagreement measure
        # below depends on it; the id is what still resolves afterwards.
        "concludes_claim_id": (str(claims_by_seq[at]) if at is not None
                               else None),
        # The three findings this has to keep apart. `ends_on_it` true is an
        # answer that closes; false with a sequence is a conclusion buried at
        # that sequence; null is no conclusion anywhere.
        "ends_on_it": (at is not None and at == last),
        "shape": ("closes" if at is not None and at == last
                  else "buried" if at is not None
                  else "absent"),
        # The gate's own boolean, beside the position it gave. Disagreement
        # between the two is the thing to count, which is why this is read
        # strictly rather than for truthiness: `bool("false")` is True, and a
        # non-canonical scalar read that way would manufacture exactly the
        # disagreement this exists to measure. The prompt asks for a boolean,
        # so anything else is a reply that did not answer, recorded as False.
        #
        # Worth knowing, and NOT changed here: the consumer that blocks reads
        # the same field for truthiness (`if data.get("no_conclusion")`), so a
        # reply of "false" would hold the answer back while this records False.
        # That divergence is a defect in the blocking path, not in the record,
        # and fixing it changes what publishes — out of scope for a change that
        # blocks nothing. It is a reason to have the record.
        "gate_said_none": data.get("no_conclusion") is True,
        "single_part": len(parts) == 1,
    }


async def _record_gate_cycle(
    run_id: uuid.UUID, *, parts: list[dict],
    reparse: str | None = None, unparseable: str | None = None,
    unaccounted: list[str] | None = None,
    fed: list[dict] | None = None,
    system_waived: list[str] | None = None,
    closing: dict | None = None,
    coverage: dict | None = None,
    naming_mismatches: list[dict] | None = None,
    checks: dict | None = None,
    reread: dict | None = None,
    standing_counts: dict | None = None,
    objection_ledger: list[dict] | None = None,
    no_claims: bool = False,
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
    # The flat keys stay: `answer_regress` reads `gate_redraft` and the run
    # export reads `gate_issues`, and both want the final state. What they
    # cannot give is a history, because merging by key leaves only the last
    # cycle, so a numbered copy goes beside them.
    #
    # This is the fourth field in this file to need it, after `round_N`,
    # `revision_N` and `cycle_N`. It is the one that was asked for: with only
    # the flat keys, a still-owed source could be shown its note and its feed
    # count and nothing about WHEN the gate first named it, so "first cycle or
    # last" had no answer. `fed` below is that answer.
    cycle = await _next_cycle_key(run_id, "validate", "gate")
    await _tele(run_id, "validate", **{cycle: {
        "at": _iso(), "parts": parts, "reparse": reparse,
        "unparseable": unparseable, "unaccounted": unaccounted or [],
        # Which obligations were put in front of the reviser this cycle, and
        # how many times each has been fed counting this one. The ledger keeps
        # a running total and no dates, so this is the only place the arrival
        # of an obligation is recorded.
        "fed": fed or [], "system_waived": system_waived or [],
        # Which claims named a case they do not cite. Its own key rather than
        # a line in `issues`, because this one is on trial: it reports a
        # different defect from the naming notes it travels with, and whether
        # it deserves a stronger channel than an issue is a question about its
        # false-positive rate. Nothing can answer that unless each cycle's
        # findings are counted where they can be read back per run.
        "naming_mismatches": naming_mismatches or [],
        # eligible / judged / flagged per check. Movement, not correctness:
        # a check judging 200 and flagging 3 every run reads the same whether
        # those 3 are the right 3 or not.
        "checks": checks or {},
        # Always written, even as zeros. A key a cycle does not set is
        # read as belonging to the next one, which is the defect this
        # function exists to stop.
        "reread": reread or {"looked": 0, "found": 0},
        # What the standing rule did this cycle: how many rule claims rested
        # lower than the retrieved set allowed, how many of those gaps became
        # a request, how many higher-standing documents were opened for a
        # proposition and how many of those reads found one, and how many
        # arguments the answer itself closed. Always written, zeros included,
        # for the reason every other key here is: a cycle that sets no key
        # inherits the last one's, and a rule that fired twenty cycles ago
        # would read as firing now.
        "standing": standing_counts or {"gaps": 0, "raised": 0, "looked": 0,
                                        "found": 0, "resolved": 0},
        # Beside the parts it summarises, not only as a flat key. The parts in
        # this dict already carry the per-part audit, so leaving the summary
        # flat would put a last-write count next to a per-cycle history and
        # invite reading one as the other.
        "coverage": coverage or {},
        # Beside the coverage summary for the same reason it is: both are
        # answer-scoped readings of this cycle, and a flat key would be a
        # last-write sitting next to a history.
        "closing": closing or {},
        # What was argued and how far each argument had got when this cycle
        # ended: id, what was asked, how important the gate called it, the
        # drafter's reason, every ruling. Per cycle rather than flat, because
        # the whole value of the ledger is being able to see a point move —
        # raised, refused, accepted — and a last-write would show only where
        # it stopped.
        "objections": objection_ledger or [],
        # The cycle that judged nothing because nothing was left to judge.
        # Written every time, false included: a missing key would say the run
        # predates the field, and an absent cycle would say the gate never
        # ran. Both are wrong about a run whose last claim was removed.
        "no_claims": bool(no_claims),
    }})
    await _tele(run_id, "validate", gate_parts=parts,
                gate_reparse=reparse, gate_unparseable=unparseable,
                gate_unaccounted=unaccounted, gate_coverage=coverage)


async def _amend_gate_cycle(run_id: uuid.UUID, **detail: Any) -> None:
    """The rest of a gate cycle's outcome, merged into the cycle it belongs to.

    A gate pass is recorded in two halves and they cannot be one write. The
    first half is everything the gate itself decided; the second is `issues`,
    which is only complete after the caller has added the deterministic
    provenance check that runs outside the gate, and `redraft`, which the
    caller derives. Writing the second half as flat keys is what made it a
    last-write.

    Amends the highest-numbered cycle rather than taking the key as an
    argument, which would mean threading it back through `_gate_answer`'s
    return type and every one of its call sites. Every path into the caller
    has been through `_record_gate_cycle` first, including the unparseable
    one, so there is always a cycle open to amend.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        entry = ((run.telemetry if run is not None else None) or {}).get("validate") or {}
    cycles = [k for k in entry if _is_numbered(k, "gate")]
    if not cycles:
        return
    latest = max(cycles, key=lambda k: int(k[len("gate_"):]))
    merged = {**(entry.get(latest) or {}), **detail}
    await _tele(run_id, "validate", **{latest: merged})


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


def _split_part(x: Any) -> dict | None:
    """One element of the splitter's reply as `{label, text}`, or None when it
    is not a part at all.

    `text` is the thing the question asks, in full; `label` is the heading it
    is printed under. A bare string is a part that came back without a
    heading — the shape the splitter used to answer in, and the one a model
    falls back to — and its heading is derived from its text. Pure.
    """
    if isinstance(x, str):
        text, label = x.strip(), ""
    elif isinstance(x, dict):
        text = str(x.get("text") or "").strip()
        label = str(x.get("label") or "").strip()
    else:
        return None
    if not text:
        return None
    return {"label": answer_structure.part_heading(label, text), "text": text}


def _part_text(part: Any) -> str:
    """What a part asks, from either shape. A run stored before the splitter
    wrote headings holds the text alone."""
    return str((part or {}).get("text") if isinstance(part, dict) else part or "")


async def _split_question(sinas: _Sinas, question: str) -> list[dict]:
    """The distinct things the question asks, or [] if that cannot be read.

    Each part comes back as `{label, text}`: the full thing asked, and a
    short heading for it. The heading is asked of the splitter rather than
    derived here because only the splitter has read the question — the
    engine's own attempt was the part's first ten words and an ellipsis,
    which printed as a sentence cut off mid-phrase over every section of
    every published answer.

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
        "\n\nGive each part a heading as well as its text. The text is the "
        "whole thing the question asks, in one sentence. The heading is what "
        "a reader scans a section by: a phrase of three to seven words, no "
        "trailing punctuation, and never the text cut short — write the "
        "subject of the part, not its opening words."
        '\n\nReply ONLY JSON: {"parts": [{"label": "<the heading>", '
        '"text": "<one thing the question asks, in full>"}, ...]}'
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
            # An element that is not a part — an object with no text, a
            # number, a null — makes the whole reply unusable rather than
            # being dropped. Dropping it silently would be worse: a lost part
            # is the defect this whole change exists to stop, so the repair
            # below runs instead.
            got = [p for p in (_split_part(x) for x in raw) if p]
            if len(got) != len(raw):
                raise ValueError("a part carried no text")
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
) -> list[dict]:
    """The run's decomposition, split once and reused by every later cycle.

    Computed on the first gate cycle rather than as a pipeline stage: all
    three call sites already carry the run id, so no signature moves, and a
    resumed run reads what the first one wrote. State lives in telemetry,
    the obligation ledger's precedent: no migration, survives a restart,
    one writer per run.

    A run recorded before the splitter wrote headings stored its parts as
    plain strings; they are read back as parts with a derived heading, so a
    resumed run is not held to a shape its record predates.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        stored = ((run.telemetry or {}).get("validate") or {}).get("question_parts")
    if isinstance(stored, list) and stored:
        return [p for p in (_split_part(x) for x in stored) if p]
    parts = await _split_question(sinas, question)
    if parts:
        await _tele(run_id, "validate", question_parts=parts)
    return parts


def _themes_from_reply(reply: str) -> list[str]:
    """Themes out of the observer's reply. Strings only, at most five.

    Separate and pure so the parsing can be tested without a model, and so a
    malformed reply produces nothing rather than a theme that is really an
    error message. Nothing here can return a question part: the caller keeps
    the two lists apart and only one of them is ever checked.
    """
    try:
        cleaned = (reply or "").strip().strip("`").removeprefix("json").strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return []
        raw = json.loads(cleaned[start:end + 1]).get("themes")
        if not isinstance(raw, list):
            return []
        return [x.strip() for x in raw if isinstance(x, str) and x.strip()][:5]
    except Exception:  # noqa: BLE001
        return []


async def _uncovered_themes(
    sinas: _Sinas, run_id: uuid.UUID, question: str,
    parts: list[dict], working_set: str,
) -> list[str]:
    """Themes the retrieved material carries that no part of the question asks.

    An observation, never a part. It is not checked, cannot be marked covered
    and cannot hold an answer back, and the two lists are kept apart at every
    point so that nothing downstream can mistake one for the other.

    Why it is not simply a better split. The splitter reads the question alone,
    which is what makes its output stable across the cycles of a run: before
    #105 the split moved with the draft, a part stopped being listed and so
    stopped being checked, and once a fifth part appeared that the question
    never asked and the draft happened to contain. Reading the working set
    would recover the limbs a lawyer supplies from knowing the area, on the
    questions whose wording does not name them, and would put the other
    questions' correct splits at the mercy of what retrieval returned. On the
    measured set that is one question helped against thirty-one put at risk,
    and inventing a part from the material is the exact defect #105 removed.

    So the material is read and the split is not touched. Where a theme is
    present and unasked, that is worth knowing, for a planner deciding what to
    cover and for a reader asking what protects them on a question whose limbs
    are not in its wording. What it does not do is make the answer accountable
    for it: a limb the system notices is not a limb it is held to.

    Computed once and cached beside the parts, for the same reason: the
    working set is rebuilt each cycle, and an observation that changed between
    cycles would be as unreadable as a split that did.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        stored = ((run.telemetry or {}).get("validate") or {}).get("uncovered_themes")
    if isinstance(stored, list):
        return [str(x) for x in stored]
    if not parts or not working_set:
        # Recorded, not just returned. This runs once, before drafting, and
        # is not retried at the gate: the observation is about the question
        # and what retrieval returned, and after drafting it would be a
        # different observation. So when the split was unavailable here, the
        # key never appears at all, and absent reads the same as "this run
        # predates the field" and as "the observation ran and found nothing".
        # Three different facts. The reason says which.
        await _tele(run_id, "validate", uncovered_themes_not_observed=(
            "no question parts" if not parts else "no working set"))
        return []
    reply = await sinas.invoke(
        "sgr/answer-gate-agent",
        "QUESTION:\n" + question
        + "\n\nTHE THINGS IT ASKS, as already read:\n"
        + "\n".join(f"- {_part_text(p)}" for p in parts)
        + "\n\nRETRIEVED MATERIAL:\n" + working_set[:40000]
        + "\n\nName any subject the retrieved material covers substantially "
        "that none of the things above asks about. A lawyer reading this "
        "question would expect certain routes or doctrines to be in scope "
        "even where the wording does not name them; if the material shows "
        "one and the list above does not reach it, name it. Do not restate "
        "anything already listed, do not name a subject the material only "
        "mentions in passing, and return an empty list if there is none, "
        "which is the ordinary case.\n\n"
        'Reply ONLY JSON: {"themes": ["<subject the material covers and the '
        'question does not ask>", ...]}'
    )
    themes = _themes_from_reply(reply)
    await _tele(run_id, "validate", uncovered_themes=themes)
    return themes


def _seq_list(raw) -> list[int]:
    """Claim sequence numbers out of a verdict field: ints only, order kept,
    duplicates dropped. The gate returns them as numbers or as strings
    depending on the reply, and a repeat means nothing.

    A scalar is read as a list of one. The field is specified as a list, but a
    model that answers `covered_by: 7` plainly means claim 7, and the two ways
    a scalar goes wrong are both silent or fatal rather than merely wrong:
    iterating a bare int raises TypeError and fails the run, and iterating the
    string "10" yields the characters, so the part records claims 1 and 0 —
    corrupting the very telemetry this exists to produce.

    A bool is not a sequence number. `True` is an int in Python and would
    otherwise be read as claim 1.

    Pure.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    out: list[int] = []
    for x in raw:
        if isinstance(x, bool):
            continue
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        # A fractional value names no claim. int() would truncate 3.7 to 3
        # and attribute the verdict to a claim the gate did not name, which
        # is worse than dropping it: a dropped covered_by leaves the part
        # uncovered, the conservative direction.
        if not f.is_integer():
            continue
        n = int(f)
        if n not in out:
            out.append(n)
    return out


def _audit_coverage(named: list[int], claim_seqs: set, with_evidence: set,
                    unresponsive: list[int]) -> dict:
    """What the claims a part names turn out to be.

    The gate declares a part covered and, from here, has to say which claims
    cover it. That turns an assertion into three questions arithmetic can
    answer with no model: does the claim exist, does it carry any evidence, and
    did the gate itself already call it merely descriptive.

    The three are kept apart because they mean different things. Naming nothing
    is the gate declining to point. Naming a claim that is not in the answer is
    the gate pointing at something that is not there, which is worse. Naming
    claims that all lack evidence is a part resting on assertion. Naming claims
    the verdict also listed as `unresponsive` is a part resting on text that
    only describes its source — the one finding class the gate records and then
    lets through, because `unresponsive` goes to `issues` and never blocks.

    Nothing here blocks anything. It is recorded so the next batch can say how
    often each happens, and the decision comes after. Pure.
    """
    missing = [n for n in named if n not in claim_seqs]
    present = [n for n in named if n in claim_seqs]
    return {
        "covered_by": named,
        "covered_by_missing": missing,
        "covered_by_unsupported": [n for n in present if n not in with_evidence],
        "covered_by_unresponsive": [n for n in present if n in unresponsive],
    }



async def _ask_document(sinas: _Sinas, prompt: str, filename: str) -> dict | None:
    """One whole-document look, as a hit or nothing.

    The half of a re-read that is not policy: make the call, read the reply,
    and refuse to call a hit anything that did not come back with a verbatim
    quote. Shared by the two callers of `services.reread` so that the
    condition for "found" is written once — a second copy of this is a second
    place for `found: true` with an empty quote to become evidence.

    It asks the PASSAGE EXTRACTOR, not the gate. This looks for a passage; it
    does not judge one, and what comes back is checked against the document
    below and dropped unless the quote is verbatim — so the tier decides
    recall and cost, never whether a fabrication gets through. Extraction is
    already that agent's whole job, and its instruction is the one this needs:
    quote exactly, never paraphrase.

    It reached for the gate agent originally because the gate's reply shape
    suited it, and the whole document rides in the prompt. Measured over one
    night: 19 of these calls averaged 173,000 tokens and cost $62, against $35
    for the 75 calls that actually asked the gate to judge an answer. The
    collection holds documents up to 5.5MB; asking the deployment's dearest
    model to read one end to end was not a judgement anyone made.
    """
    reply = await sinas.invoke("sgr/passage-extractor-agent", prompt)
    try:
        cleaned = (reply or "").strip().strip("`")
        cleaned = cleaned.removeprefix("json").strip()
        a, b = cleaned.find("{"), cleaned.rfind("}")
        data = json.loads(cleaned[a:b + 1]) if a >= 0 < b else {}
    except (ValueError, json.JSONDecodeError):
        return None
    if not (data.get("found") and str(data.get("quote") or "").strip()):
        return None
    return {"filename": filename, "line_from": data.get("line_from"),
            "line_to": data.get("line_to"), "quote": data.get("quote")}


async def _reread_cited_for_parts(
    sinas: _Sinas, answer_id: uuid.UUID, parts: list[dict]
) -> tuple[list[dict], int, int]:
    """Re-read every cited source, whole, for each part the gate called
    uncovered. Returns the parts, how many were answered by the re-read, and
    how many were looked for.

    Best-effort in the same sense as the naming checks: this can only ever
    turn an uncovered part into a covered one on verbatim evidence, so a
    failure costs a finding rather than inventing one, and the run must not
    fail because a second look could not be taken.
    """
    from app.services.reread import (
        Cited, apply_reread, needs_reread, reread_prompt)

    want = [i for i, p in enumerate(parts) if not p.get("covered")]
    if not want:
        return parts, 0, 0
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(Document.filename, DocumentVersion.content_md)
                .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
                .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
                .join(DocumentVersion,
                      DocumentVersion.id == Document.current_version_id)
                .where(AnswerClaim.answer_id == answer_id)
                .distinct())).all()
        sources = [Cited(filename=str(f), text=str(c or "")) for f, c in rows]
        if not needs_reread(parts, sources):
            return parts, 0, 0

        found: dict[int, dict | None] = {}
        for i in want:
            hit = None
            for src in sources:
                if not src.text or len(src.text) < 40:
                    continue
                hit = await _ask_document(
                    sinas, reread_prompt(parts[i], src), src.filename)
                if hit:
                    break
            found[i] = hit
        parts, hits = apply_reread(parts, found,
                                   cited={s.filename for s in sources})
        return parts, hits, len(want)
    except CancelledOutcome:
        raise
    except Exception:  # noqa: BLE001
        _log.exception("re-read before unanswered failed for answer %s",
                       answer_id)
        return parts, 0, 0


#: Standing gaps one cycle argues about. Each costs a whole-document read per
#: higher-standing document named, and a round that hands the drafter eight
#: arguments gets eight shallow answers. Three is what a revision round can
#: actually act on, and a gap not argued this cycle is argued the next one:
#: the gaps are recomputed from the claims as they then stand.
MAX_STANDING_GAPS = 3
#: Higher-standing documents opened for one gap before the look gives up.
#: The list is best-standing first, so this cuts from the bottom.
MAX_STANDING_LOOKS = 3


async def _standing_objections(
    sinas: _Sinas, run_id: uuid.UUID, answer_id: uuid.UUID,
    mrows: list[dict], claims: list[dict], cycle_no: int,
) -> tuple[list[str], list[str], dict[str, int]]:
    """Argue with the drafter about claims resting lower than the set allows.

    Returns `(issues, points, counts)`. Each issue is a request the drafter
    must act on or refuse with a reason; each point is a proposition the
    reviser is given passages for, anchored on the document that should carry
    it. Deterministic in what it FINDS — two integers compared — and a model
    call only where one is unavoidable: reading a document for a proposition.

    THE ORDER MATTERS AND IS THE WHOLE DESIGN. The gap is found first, the
    higher-standing documents are read SECOND, and only then is the objection
    put. The drafter sees passages extracted for its own planned claims, so a
    claim resting on a lower-standing source may rest there because the
    higher-standing one was never opened for that proposition. Putting the
    objection before the read would produce a refusal from a drafter that had
    nothing to check it against, and a refusal made in ignorance settles a
    point that was never argued.

    Best-effort, like the naming checks around it: this can only ever add a
    finding, so a failure costs the finding and never the run.
    """
    issues: list[str] = []
    points: list[str] = []
    counts = {"gaps": 0, "raised": 0, "looked": 0, "found": 0, "resolved": 0}
    try:
        facts = _source_context(mrows)
        by_file = {fn: f.get("standing") for fn, f in facts.items()}
        gaps = standing.gaps(claims, by_file)
        counts["gaps"] = len(gaps)
        # A point the answer has since fixed is settled by the answer. The
        # gaps are recomputed every cycle from the claims as they now stand,
        # so a claim re-cited, revised into another kind or dropped simply
        # stops appearing — and its argument ends, rather than being asked
        # about a claim that no longer says what was objected to.
        ledger = [e for e in await objections.ledger(run_id)
                  if e.get("kind") == standing.KIND]
        # Only the arguments still running. An accepted refusal and a stall
        # are settled by the review's ruling, and marking either resolved
        # here would rewrite a point the drafter won as a point the answer
        # fixed — which is precisely the count this rule exists to measure.
        done = standing.closed(
            {str(e.get("subject") or "") for e in ledger
             if e.get("state") in (objections.OPEN, objections.ANSWERED)},
            gaps)
        if done:
            await objections.resolve(run_id, done, kind=standing.KIND)
            counts["resolved"] = len(done)
        asked_before = {str(e.get("subject") or ""): e for e in ledger}
        settled = await objections.settled_subjects(run_id)
        fresh = [g for g in gaps if g.subject not in settled]
        for gap in fresh[:MAX_STANDING_GAPS]:
            prior = asked_before.get(gap.subject)
            if prior is not None:
                # Already argued this run and still open. The documents were
                # read when it was first put; reading them again every cycle
                # would spend a call per document per cycle to reach the same
                # answer, and the drafter has not been shown anything new.
                asked = str(prior.get("asked") or "")
                hit = None
            else:
                hit = await _look_higher(sinas, gap, counts)
                asked = standing.objection(gap, hit)
            oid = await objections.raise_objection(
                run_id, kind=standing.KIND, subject=gap.subject, asked=asked,
                # Supporting, always. There is no hard fail here: a claim
                # resting low is a claim a reader must be told about, not a
                # part of the question that could not be answered, and only a
                # justified `essential` may reach a run's verdict.
                importance=objections.SUPPORTING, cycle=cycle_no)
            if oid is None:
                continue
            counts["raised"] += 1
            issues.append(
                f"Standing: {asked} A general proposition rests on the "
                "highest-standing source RETRIEVED FOR THIS ANSWER that "
                "carries it. Revise the claim to cite the higher-standing "
                "source for this proposition — the lower-standing one may "
                "stay beside it where it adds something of its own — or "
                f'REFUSE: reply with {{"objection": "{oid}", "rationale": '
                '"<why no higher-standing retrieved source carries this '
                'proposition>"}, which is an answer and will be ruled on '
                "rather than ignored. A refusal commits you to one more "
                "thing: the claim must then say, in its own sentence and in "
                "your words, what the proposition rests on and what that "
                "means for the weight a reader should give it. Nothing is "
                "added to your sentence for you."
            )
            if hit:
                points.append(standing.point(gap))
    except CancelledOutcome:
        raise
    except Exception:  # noqa: BLE001
        _log.exception("standing check failed for answer %s", answer_id)
    return issues, points, counts


GATE_AGENT = "sgr/answer-gate-agent"


async def _gate_turn(sinas: _Sinas, run_id: uuid.UUID,
                     brief: str, turn: str) -> str:
    """One cycle of judging, as a turn in the review's own conversation.

    Judging used to be a fresh call per cycle, and each call carried the
    question, its parts, the whole retrieved document set and every rule for
    judging it. Measured over one night: 40,000 tokens a call, 39,266 of them
    written to the prompt cache and 732 read back. Sinas' Anthropic provider
    sets a rolling cache breakpoint on the last message precisely so a
    sequence of calls reuses the previous one's prefix; a freshly assembled
    body each time gives it nothing to roll onto. The drafting conversation
    was built for this exact defect — this is the review's half of it.

    Turn one is the brief and is sent once. Every later turn carries the draft
    and nothing else. Two things follow, and the second is not about money:
    the working set is read from cache rather than rewritten, and the gate can
    SEE what it already ruled, where before its own rulings had to be read
    back to it every cycle because it had no memory of making them.

    A conversation that cannot be opened falls back to a single call carrying
    both halves — the old behaviour exactly. Judging is the stage that decides
    whether an answer may publish, and it does not get to fail because a chat
    could not be created.
    """
    chat = drafting_chat.DraftingChat(
        client=sinas, agent=GATE_AGENT, title="[query-run] gate")
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        chat_id = (run.gate_chat_id or None) if run else None
    try:
        await chat.start(brief, chat_id=chat_id)
        if chat.chat_id and chat.chat_id != chat_id:
            async with AsyncSessionLocal() as session:
                run = await session.get(QueryRun, run_id)
                if run is not None:
                    run.gate_chat_id = (chat.chat_id or "")[:64] or None
                    await session.commit()
        return await chat.ask(turn)
    except CancelledOutcome:
        raise
    except Exception:  # noqa: BLE001
        _log.warning("gate conversation unavailable for run %s; "
                     "judging this cycle in one call", run_id, exc_info=True)
        return await sinas.invoke(GATE_AGENT, brief + "\n\n" + turn)


#: How many whole-document reads one cycle spends looking deeper into sources
#: the answer already cites. Bounded like the standing check beside it: the
#: reads are cheap on the extraction tier and they are not free in wall clock,
#: and a cycle that opened everything would be a second retrieval pass.
MAX_DEEPER_LOOKS = 4


async def _look_deeper(
    sinas: _Sinas, cited: list[str], parts: list[dict]
) -> list[dict]:
    """Ask the highest-standing CITED documents what else they carry.

    The expert review's findings are rarely that an answer is wrong. They are
    that it is thin: a rule the cited judgment states and the answer does not.
    Measured on one of them — T-125/03 is retrieved, cited three times by the
    answer, and paragraph 123 of it states the rule the reviewer asked for.
    No claim says it, and nothing in the run ever asked that document about
    that part of the question.

    Nothing was broken. Extraction reads per PLANNED claim from that claim's
    own anchors, and the plan is written from the question before any document
    has been read. A document opened for one point is never opened for
    another, and the gate cannot help: it names sources the answer did NOT
    use, and this document was used.

    Bounded twice over. Only classes the deployment ranks highest are read —
    that is where a holding lives, and a commentary chapter re-read for a
    second point yields more commentary. And only `MAX_DEEPER_LOOKS` reads
    happen per cycle, parts in order, so the cost is a handful of extraction
    calls rather than a second pass over the corpus.
    """
    from app.services.reread import Cited, deeper_prompt

    asks = [str(p.get("asks") or p.get("text") or "").strip() for p in parts]
    asks = [a for a in asks if a]
    if not cited or not asks:
        return []

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Document.filename, DocumentVersion.content_md,
                   DocumentClass.standing)
            .join(DocumentVersion,
                  DocumentVersion.id == Document.current_version_id)
            .outerjoin(DocumentClass,
                       DocumentClass.id == Document.document_class_id)
            .where(Document.filename.in_(list(cited))))).all()

    ranked = sorted(
        ((str(f), str(c or ""), s) for f, c, s in rows if len(str(c or "")) >= 40),
        key=lambda r: (r[2] if r[2] is not None else 10_000, r[0]))
    if not ranked:
        return []
    # Only the top declared rank. A deployment that declares no standing gets
    # nothing here rather than an arbitrary pick — the same silence the other
    # declared checks keep.
    best = ranked[0][2]
    if best is None:
        return []
    top = [r for r in ranked if r[2] == best]

    out: list[dict] = []
    for ask in asks:
        for filename, body, _ in top:
            if len(out) >= MAX_DEEPER_LOOKS:
                return out
            hit = await _ask_document(
                sinas, deeper_prompt(ask, Cited(filename=filename, text=body)),
                filename)
            if hit and str(hit.get("quote") or "").strip():
                out.append({"doc": filename, "part": ask, "hit": hit})
    return out


async def _look_owed(
    sinas: _Sinas, owed: list[dict]
) -> dict[str, dict]:
    """Open each document the review named as unused, for the point it owes.

    The same whole-document read the standing check makes, pointed at the
    third case that needs it. A source is put to the drafter as owed — cite
    it, waive it after reading its passages, or refuse it — and until now
    nothing opened it. Extraction reads per planned claim from that claim's
    anchors, so a document the plan never pointed at has no passages, and a
    drafter with nothing verbatim to quote can only refuse however apt the
    document is.

    That is the mechanical form of a finding the expert review made six times
    over, naming the missing material by its rank in the retrieved set: 11,
    26, 31, 33, 41, 51. Retrieved every time; read none of them.

    Returns {filename: hit} for the documents that yielded a passage. A
    document that yields nothing is not an error and not a hit — the drafter
    is then refusing on an informed basis, which is the whole point.
    """
    from app.services.reread import Cited, owed_prompt

    if not owed:
        return {}
    names = [u["doc"] for u in owed]
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Document.filename, DocumentVersion.content_md)
            .join(DocumentVersion,
                  DocumentVersion.id == Document.current_version_id)
            .where(Document.filename.in_(names)))).all()
    text_of = {str(f): str(c or "") for f, c in rows}
    notes = {u["doc"]: str(u.get("note") or "") for u in owed}
    found: dict[str, dict] = {}
    for name in names:
        body = text_of.get(name, "")
        if len(body) < 40:
            continue
        hit = await _ask_document(
            sinas, owed_prompt(notes.get(name, ""),
                               Cited(filename=name, text=body)), name)
        if hit:
            found[name] = hit
    return found


async def _look_higher(sinas: _Sinas, gap, counts: dict[str, int]) -> dict | None:
    """Read the higher-standing documents for this claim's proposition.

    The same whole-document read the uncovered-part re-read makes, pointed at
    the documents the answer did NOT cite — which is the one boundary that
    differs, and differs for a reason: there, reaching outside the citations
    would be the gate answering the question instead of checking it; here,
    the documents are the retrieved set's own and the entire finding is that
    the drafter was never shown them.
    """
    from app.services.reread import Cited, standing_prompt

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Document.filename, DocumentVersion.content_md)
            .join(DocumentVersion,
                  DocumentVersion.id == Document.current_version_id)
            .where(Document.filename.in_(list(gap.better))))).all()
    order = {fn: i for i, fn in enumerate(gap.better)}
    sources = sorted((Cited(filename=str(f), text=str(c or ""))
                      for f, c in rows), key=lambda s: order.get(s.filename, 99))
    for src in sources[:MAX_STANDING_LOOKS]:
        if len(src.text) < 40:
            continue
        counts["looked"] += 1
        hit = await _ask_document(
            sinas, standing_prompt(gap.text, src), src.filename)
        if hit:
            counts["found"] += 1
            return hit
    return None


async def _gate_answer(
    sinas: _Sinas, run_question: str, answer_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[bool, str, list[str], list[str], list[str]]:
    """Judge whether the surviving claims still answer the question, and
    surface quality findings. Generic by construction: no claim-type
    vocabulary, no counting floors — a stateless judge, per part of the
    question.

    Returns (publishable, missing, issues, correctness, points, cause).
    `cause` is "coverage", "holistic" or "" — what held the answer back, so a
    partial is named by the thing that caused it. There is no `accounting`
    cause: material the answer did not incorporate never names a partial.
    `publishable` and `correctness` are the hard gate; `issues` are
    best-effort remediation targets that must never block publication on
    their own; `points` are the things revision must be given passages for —
    one entry per part of the question the claims do not answer, then each
    stronger source the gate named.

    One thing clears `publishable`: every part of the question covered, with
    the judge's own verdict behind it. A source the review named and the
    answer did not use holds `publishable` no longer — it buys revision
    cycles through `issues`, and what survives the cycles is a note on the
    answer, or, for an `essential` request pressed to a standstill, a
    reservation the reader sees and a contested outcome. `partial` keeps
    meaning a part could not be answered.

    Everything the gate finds is returned. It used to leave some of it in a
    module-level dict, which a later edit deleted the declaration of — so
    every verdict raised NameError inside the try below and came back out of
    the except as "treated as pass". The gate stopped gating and nothing
    said so. A value that callers need is a return value.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(AnswerClaim.sequence, AnswerClaim.claim_text,
                       AnswerClaim.id)
                .where(AnswerClaim.answer_id == answer_id)
                .order_by(AnswerClaim.sequence)
            )
        ).all()
        if not rows:
            # Ahead of the judge, because there is nothing to judge. Deletion
            # runs to completion: validation, the final sweep and the
            # exhausted-round cleanup can each take the last surviving claim,
            # and what is left asserts nothing. `publishable` means the
            # surviving claims still answer the question, and no claims never
            # do, so this is neither a verdict worth a model call nor one a
            # model should be able to overrule after being shown an empty
            # list.
            #
            # Nothing else stops an empty answer from publishing. Neither
            # publish site counts claims and neither does `_publish_answer`.
            # What stands there today is the missing-conclusion finding
            # happening to fire, which is a model's opinion about an empty
            # list of claims and not a guard.
            note = (
                "The answer has no claims at all: every claim was removed "
                "during validation. Write the claims that answer the "
                "question, each citing passages from the working set, "
                "beginning with one that states the answer directly."
            )
            # Recorded before returning, because this is an exit path and
            # `_amend_gate_cycle` amends the highest-numbered cycle rather
            # than one it is handed. Its contract is that every path into the
            # caller has been through here first, so a path that returns
            # without recording leaves the caller's amend with no cycle open
            # and drops the rejection out of the numbered history entirely.
            # That is the third time this file has lost a fact to an exit
            # path that wrote a different subset of keys from its siblings.
            await _record_gate_cycle(run_id, parts=[], no_claims=True)
            return (False, "the answer has no claims left",
                    [note], [note], [], "coverage")
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
        # Which claims carry any evidence at all. `cited` above is a flat set
        # of filenames and cannot answer it per claim, and a part resting on a
        # claim with no bound passage is a part resting on an assertion.
        with_evidence = set((await session.execute(
            select(AnswerClaim.sequence)
            .join(ClaimEvidence, ClaimEvidence.claim_id == AnswerClaim.id)
            .where(AnswerClaim.answer_id == answer_id)
        )).scalars().all())
        # Which documents each claim actually rests on. `cited` above is one
        # flat set for the whole answer and cannot answer the question the
        # standing rule asks — whether THIS claim rests lower than the set
        # allows — for which the citations have to be read per claim.
        cites_by_claim: dict[uuid.UUID, list[str]] = {}
        for cid, fname in (await session.execute(
            select(ClaimEvidence.claim_id, Document.filename)
            .join(Document, Document.id == ClaimEvidence.document_id)
            .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
            .where(AnswerClaim.answer_id == answer_id)
        )).all():
            if fname not in cites_by_claim.setdefault(cid, []):
                cites_by_claim[cid].append(str(fname))
        parent_result_id = (
            await session.execute(
                select(QueryRun.parent_result_id).where(QueryRun.answer_id == answer_id)
            )
        ).scalar_one_or_none()
        # The rows with their structure, for the checks arithmetic can make:
        # does every part have its conclusion, and does every inference rest
        # on a supported claim. Read as objects so an older answer, whose
        # rows carry no structure, is recognised and left to the judge.
        from app.models import Answer

        structured = (await session.execute(
            select(AnswerClaim).where(AnswerClaim.answer_id == answer_id)
            .order_by(AnswerClaim.sequence))).scalars().all()
        answer_row = await session.get(Answer, answer_id)
        question_parts = [
            p for p in (getattr(answer_row, "question_parts", None) or [])
            if isinstance(p, dict)]
    # The gate judges which sources the answer should have used, which is
    # planning-shaped work: it gets the planner's manifest line — class and
    # declared annotations included — not a bare filename and summary. It
    # cannot call a judgment "plainly more authoritative" than a bulletin
    # article without being shown which document is which.
    mrows = await _manifest_rows(parent_result_id) if parent_result_id else []
    claims = "\n".join(f"{seq}. {text}" for seq, text, _ in rows)
    claims_by_seq = {seq: cid for seq, _, cid in rows}
    # WITHOUT the CITED marks. The marks change every cycle and the set does
    # not, and this block is 90% of what is sent: marking it inline made the
    # largest stable thing in the run look different on every call, so the
    # prompt cache had nothing to match and rewrote all of it, every time.
    # Which documents the draft cites travels with the draft instead.
    source_lines = "\n".join(
        f"- {r['filename']} | {r['class'] or '-'} | {r['annotations'] or '-'} | "
        f"{r.get('properties') or '-'} | "
        f"{r['summary'].replace(chr(10), ' ')[:200]}"
        for r in mrows
    ) or "(working set unavailable)"
    # The decomposition is fixed for the run and judged here, not derived
    # here. An empty list means the split could not be read twice, and the
    # prompt falls back to deriving it, which is the behaviour this replaces.
    fixed = await _question_parts(sinas, run_id, run_question)
    # The other half of the conversation. Each of these is a request this
    # review made and the drafter declined WITH A REASON, and the review has
    # not yet said anything back. Showing them here is what makes the loop
    # two-way: the drafter's reasoning can retire a request instead of only
    # obeying it, and a request the review cannot answer is one it should not
    # be making a third time.
    open_refusals = await objections.outstanding(run_id)
    refusals_block = ("\n\nREQUESTS YOU MADE THAT THE DRAFTER DECLINED, EACH "
                      "WITH ITS REASON:\n" + "\n".join(
                          f"  [{o['id']}] you asked: {o.get('asked') or ''}\n"
                          f"       the drafter declined: "
                          f"{(o.get('reply') or {}).get('reason') or ''}"
                          for o in open_refusals)
                      + "\nRule on each one in objection_rulings. ACCEPT means "
                      "the reason settles it: you will not raise that point "
                      "again and the answer is not the poorer for it. RESTATE "
                      "means you press it, and pressing costs something you "
                      "have NOT said before — a different document, evidence "
                      "the drafter has not seen, or a narrower ask. A "
                      "restatement that repeats what you already said is read "
                      "as an acceptance, and so is saying nothing about a "
                      "request listed here."
                      ) if open_refusals else ""
    # Turn one is the brief: the question, its parts, the whole retrieved set
    # and every rule for judging. It is sent once per run and read back from
    # cache on every later cycle. Turn two onward carries only the draft.
    brief = (
        "QUESTION:\n" + run_question
        + "\n\nWORKING DOCUMENT SET (every document retrieved for this "
          "question; which of them the draft cites comes with the draft):\n"
        + source_lines
        + (('\n\nPARTS OF THE QUESTION (fixed for this run; judge'
            ' each against the claims, and do not add, merge or drop'
            ' one):\n'
            + "\n".join(f'  {i}. {_part_text(a)}' for i, a in enumerate(fixed, 1)))
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
        '\n\nSay how much each one matters, because the two cases are not the '
        'same and the answer treats them differently. "essential" means a '
        'part of the question is NOT PROPERLY ANSWERED without this source, '
        'and you must say in one line which part and why — an essential mark '
        'with no such line is read as supporting, so do not mark one you '
        'cannot justify. "supporting" means relevant: the answer would be '
        'better with it and is not wrong without it. Most named sources are '
        'supporting.'
        + '\n\nReply ONLY JSON: {"publishable": true|false,'
        + (' "parts": [{"n": <the number of the part above>, "covered": '
           'true|false, "covered_by": [<the sequence numbers of the claims '
           'that answer this part — name every one, and name none if the part '
           'is not covered>], '
           '"gap": "<what is missing, if not covered>"}],'
           if fixed else
           ' "parts": [{"asks": "<one thing the question asks>", "covered": '
           'true|false, "covered_by": [<the sequence numbers of the claims '
           'that answer this part>], '
           '"gap": "<what is missing, if not covered>"}],')
        + ' "missing": "<if not publishable: what the claims fail to deliver on>",'
        ' "unresponsive": [<sequence numbers of claims that only describe a source without advancing the answer>],'
        ' "tension": "<ONLY a pair of claims that CANNOT BOTH BE TRUE — quote the '
'two incompatible propositions verbatim. Claims that restate the same rule, '
'overlap, emphasise different aspects, or address different procedural '
'stages are NOT in tension; when in doubt, null. Or null.>",'
        ' "dangling": [<sequence numbers of claims that lean on another claim that is not there: they open with or depend on phrases like "that logic", "applying this reasoning", "the same principle" whose antecedent claim is absent or says something else>],'
        ' "no_conclusion": <true if no claim draws the overall conclusion the question asks for>,'
        ' "concludes_at": <the sequence number of the claim that draws that overall conclusion, or null if no claim does. A claim that states the answer to the question, not one that reports what a single source says.>,'
        ' "unused_sources": [{"filename": "<name from the working set>",'
        ' "point": "<the point it settles and why the answer is poorer without it — either plainly more direct or authoritative than the source cited for that point, or bearing squarely on a part of the question the claims treat thinly or not at all>",'
        ' "importance": "essential|supporting",'
        ' "essential_because": "<REQUIRED when essential: which part of the question is not properly answered without this source, in one line. Leave empty for supporting.>",'
        ' "part": <the number of the part it bears on, or null>}, ...]'
        + ', "objection_rulings": [{"id": "<one of the ids listed above>",'
          ' "ruling": "accept|restate",'
          ' "new": "<for a restatement only: what you are adding that you have'
          ' not already said>"}] — include this key only in a cycle whose'
          ' message lists requests to rule on'
        + '}'
    )
    # Every later cycle: the draft, what it cites, and what the drafter said
    # back. Never the working set, never the rules — those are turn one and
    # are read from cache.
    turn = (
        "CLAIMS OF THE DRAFT ANSWER (the number before each claim is its "
        "identifier, not its position: revision drops claims, so gaps in the "
        "numbering are expected and are not a defect — there is no claim "
        "missing from this list):\n" + claims
        + "\n\nCITED BY THIS DRAFT: "
        + (", ".join(sorted(cited)) if cited else "(nothing yet)")
        + "\nEvery other document in the working set is uncited."
        + refusals_block
        + "\n\nJudge this draft now and reply ONLY with the JSON object "
          "described in the brief."
    )
    reply = await _gate_turn(sinas, run_id, brief, turn)
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

    # Which cycle this is, read rather than counted: `_record_gate_cycle`
    # derives the same number from the same telemetry at the end of this
    # function, and neither call writes, so the argument's history is stamped
    # with the cycle it actually happened in.
    cycle_no = int((await _next_cycle_key(run_id, "validate", "gate"))
                   .removeprefix("gate_"))
    # The gate's half of the conversation, applied before anything is fed.
    # A refusal it accepts is settled here, so the same document cannot be
    # named again three lines further down; a restatement that carries
    # nothing new is an acceptance; and a request it simply did not answer is
    # an acceptance too, because reading silence as "still objecting" would
    # let it press every point forever by answering none of them.
    await objections.rule_all(run_id, data.get("objection_rulings") or [],
                              cycle=cycle_no)
    # Coverage is judged per part. One holistic verdict let an answer
    # addressing two of a question's three parts publish, and named one
    # gap at a time when it failed — so revision fixed them one cycle
    # each, or the run ran out of cycles first.
    #
    # Read before the parts because a part now records which of the claims it
    # names the verdict had already called merely descriptive. One derivation,
    # used twice: the `issues` line below reads the same list.
    unresponsive_seqs = _seq_list(data.get("unresponsive"))
    if fixed:
        # The list is what the runner asked about, not what came back. A
        # verdict naming a part outside it is dropped, and a part it does not
        # name is uncovered rather than assumed: those two rules are what make
        # a fixed decomposition binding instead of advisory. Without them the
        # drift returns through the verdict, which is how a part the question
        # does not ask was once added and immediately marked covered.
        claim_seqs = {seq for seq, _, _ in rows}
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
            {"asks": _part_text(a),
             "covered": bool((seen.get(n) or {}).get("covered")),
             "gap": str((seen.get(n) or {}).get("gap") or "").strip()
                    or ("" if n in seen else
                        "the review returned no verdict for this part"),
             **_audit_coverage(
                 _seq_list((seen.get(n) or {}).get("covered_by")),
                 claim_seqs, with_evidence, unresponsive_seqs)}
            for n, a in enumerate(fixed, start=1)
        ]
    else:
        claim_seqs = {seq for seq, _, _ in rows}
        parts = [
            {**x, **_audit_coverage(_seq_list(x.get("covered_by")), claim_seqs,
                                    with_evidence, unresponsive_seqs)}
            for x in (data.get("parts") or []) if isinstance(x, dict)
        ]
    # Before a part is called unanswered, re-read what the answer already
    # cites. Extraction reads per planned claim from that claim's anchors, so
    # a document opened for one passage can hold the material for a part in a
    # passage nobody read: on one measured question all four of the things a
    # reviewer asked for sat in documents the answer cited. This runs only for
    # parts about to be reported as gaps, so an answer that covers everything
    # pays nothing.
    parts, reread_found, reread_looked = await _reread_cited_for_parts(
        sinas, answer_id, parts)
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
    seqs = unresponsive_seqs
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
            "The answer never draws its overall conclusion. Add a claim of "
            'kind "conclusion" with "part": null that directly answers '
            "the question, supported by the evidence already cited; it leads "
            "the answer, so restore the order rather than append."
        )
    # The structure, judged without the model. A part whose conclusion is
    # missing, or written into the analysis so the reader meets it last, and
    # an inference resting on nothing supported, are correctness defects:
    # the answer is held back until the reviser restores the order. Judged
    # only over an answer that carries structure; the rows of an older
    # answer carry none and are left to the judge's `no_conclusion`.
    claim_dicts = [
        {"sequence": c.sequence, "id": c.id, "claim_kind": c.claim_kind,
         "claim_type": c.claim_type, "section": c.section,
         "part_index": c.part_index, "follows_from": c.follows_from,
         "claim_text": c.claim_text, "supported": c.sequence in with_evidence}
        for c in structured]
    structure_gaps = (answer_structure.conclusion_gaps(claim_dicts, question_parts)
                      + answer_structure.inference_gaps(claim_dicts))
    correctness += structure_gaps
    if structure_gaps:
        await _tele(run_id, "validate", structure_gaps=structure_gaps)
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
    #
    # And every request carries an importance the gate had to choose between,
    # because "you should have used this" covers two different findings: a
    # part that is not properly answered without the source, and a source that
    # would make a sound answer better. Only the first may ever reach the
    # run's verdict, and only when the gate could say in one line which part.
    named = _unused_sources(data, len(fixed))
    fresh: dict[str, str] = {}
    for src in named:
        fresh[src["filename"]] = src["point"]
        await obligations.record(run_id, src["filename"], src["point"])
    # A request the drafter refused and this gate accepted is over. It is not
    # re-raised and its document is not fed again — enforced here rather than
    # left to the prompt, because a gate that forgets is exactly the failure
    # the ledger exists to stop.
    settled = await objections.settled_subjects(run_id)
    # A source that reached the answer settles its own argument, whatever
    # either side last said about it.
    await objections.resolve(run_id, sorted(cited))
    # What is owed this round is the ledger's to decide, this round's findings
    # included. Asking what was owed and then appending whatever had not come
    # back read an absence as "no opinion", and an entry is withheld for three
    # different reasons: waived, already cited, or unreadable. Only the last is
    # no opinion; the other two are decisions, and adding over them put retired
    # and satisfied obligations back in front of the reviser at a count the cap
    # could not act on.
    owed = [u for u in await obligations.to_feed(run_id, answer_id, fresh)
            if u["doc"] not in settled]
    capped = [u for u in owed if u["fed"] >= obligations.MAX_FEEDS]
    for u in capped:
        await obligations.waive(
            run_id, u["doc"],
            f"not grounded after {u['fed']} revision attempts", by="system")
    if capped:
        await _tele(run_id, "validate",
                    obligations_system_waived=[u["doc"] for u in capped])
    feed = [u for u in owed if u["fed"] < obligations.MAX_FEEDS][:5]
    # Each fed source is an objection with an id, so the drafter can answer
    # THIS request rather than write its reasoning into a dropped claim where
    # nothing reads it. A source the ledger refuses to re-raise drops out of
    # the feed here: `raise_objection` returns None for a settled point.
    by_name = {s["filename"]: s for s in named}
    ids: dict[str, str] = {}
    for u in list(feed):
        src = by_name.get(u["doc"]) or {}
        oid = await objections.raise_objection(
            run_id, kind="source", subject=u["doc"], asked=u["note"],
            importance=src.get("importance"),
            why_essential=src.get("essential_because"),
            part=src.get("part"), cycle=cycle_no)
        if oid is None:
            feed.remove(u)
            continue
        ids[u["doc"]] = oid
    # Open each owed document for the point it is said to carry, BEFORE the
    # request goes out. The request has always told the drafter to waive "with
    # a rationale you can only give after reading its passages" — and nothing
    # opened it. Extraction reads per planned claim from that claim's anchors,
    # so a document the plan never pointed at has no passages, and a drafter
    # with nothing verbatim to quote can only refuse, whatever the document
    # says. The expert review named that failure six times over, each time by
    # the missing source's rank in the retrieved set.
    looked = await _look_owed(sinas, feed)
    if feed:
        await _tele(run_id, "validate", **{f"owed_looks_{cycle_no}": {
            "asked": [u["doc"] for u in feed],
            "carried": sorted(looked),
        }})
    stronger = [f"{u['note']} [obligated document: {u['doc']}]" for u in feed]
    for u in feed:
        hit = looked.get(u["doc"]) or {}
        quote = str(hit.get("quote") or "").strip()
        carried = (
            f"\nThe document was opened for this point and says, at lines "
            f"{hit.get('line_from')}-{hit.get('line_to')}: \"{quote[:700]}\""
            "\nCite THAT, verbatim, if it carries the point."
            if quote else
            "\nThe document was opened for this point and no passage stating "
            "it came back. Refusing is then the right answer, and the "
            "rationale is that — not a guess about what it might hold."
        )
        issues.append(
            f"Owed source unused: {u['doc']} — {u['note']} Cite it for the "
            "point it carries, or waive it with a rationale you can only "
            "give after reading its passages, or REFUSE it: reply with "
            f'{{"objection": "{ids[u["doc"]]}", "rationale": "<why this '
            'source cannot carry the point asked of it>"}, which is an '
            "answer and will be ruled on rather than ignored."
            + carried
        )
    await obligations.note_fed(run_id, [u["doc"] for u in feed])

    # What the answer's own strongest sources say about each part, beyond the
    # point they were read for. The gate cannot ask this — it names sources
    # the answer did NOT use, and these are used — and the plan could not,
    # because it was written from the question before any document was read.
    # The reviewer's findings are mostly of this shape: a rule the cited
    # judgment states and the answer does not.
    deeper = await _look_deeper(sinas, sorted(cited), fixed)
    if deeper:
        await _tele(run_id, "validate", **{f"deeper_looks_{cycle_no}": [
            {"doc": d["doc"], "part": d["part"][:80]} for d in deeper]})
        for d in deeper:
            q = str((d["hit"] or {}).get("quote") or "").strip()
            issues.append(
                f"A source this answer already cites carries more on one part "
                f"of the question than the answer uses. {d['doc']}, on \""
                f"{d['part']}\", at lines {(d['hit'] or {}).get('line_from')}-"
                f"{(d['hit'] or {}).get('line_to')}: \"{q[:700]}\" — if the "
                "answer does not already state this, add a claim that does, "
                "citing that passage. If it already does, or the passage does "
                "not bear on the part after all, leave the answer as it is."
            )

    # A claim that states a general proposition must rest on the
    # highest-standing source the retrieval actually returned that carries
    # it. Deterministic in what it finds — the class declared a rank and two
    # of them are compared — and argued rather than enforced: the higher
    # documents are read for the proposition first, and what the drafter gets
    # is a request it may refuse with a reason. Run after the gate's rulings
    # above, so a refusal this review has already accepted is not put again,
    # and before the cycle is recorded, so the ledger written below holds
    # what was argued this cycle rather than last.
    standing_issues, standing_points, standing_counts = (
        await _standing_objections(
            sinas, run_id, answer_id, mrows,
            [{"id": str(c.id), "sequence": c.sequence, "kind": c.claim_kind,
              "text": c.claim_text,
              "cites": cites_by_claim.get(c.id) or []} for c in structured],
            cycle_no))
    issues += standing_issues
    stronger += standing_points

    # A claim asserting a rule names the source it rests on. Deterministic —
    # the document's own identifier, or the words that identify its name,
    # looked for in the claim's text — so it costs no model call and behaves
    # the same whatever language the source is written in. That last part is
    # the point: the check this replaces looked for English attribution
    # words, and a third of the collection is French, so a claim resting on a
    # French source could not fail it.
    named_docs = {
        str(r.get("filename")): {
            "identifier": r.get("identifier"),
            "name": r.get("title"),
            "naming_required": bool(r.get("naming_required")),
        }
        for r in mrows if r.get("filename")
    }
    unnamed = naming.unnamed_sources(
        [{"claim_id": str(c.id), "sequence": c.sequence, "kind": c.claim_kind,
          "text": c.claim_text, "cites": cites_by_claim.get(c.id) or []}
         for c in structured],
        named_docs,
    )
    for entry in unnamed:
        issues.append(naming.objection(entry))
    if unnamed:
        await _tele(run_id, "validate", **{f"unnamed_sources_{cycle_no}": [
            {"sequence": e.get("sequence"),
             "sources": [u.get("filename") for u in e.get("unnamed") or []]}
            for e in unnamed]})

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
    # That tie no longer decides the run's verdict, and the reason is the
    # measured failure this loop was built for: a run whose every part was
    # covered, with nothing missing, unsupported or unresponsive, ended
    # `partial` because one policy source it had reasoned its way out of
    # citing was fed three times and never incorporated. "Partial" told the
    # reader the question could not be answered. It could, and was.
    #
    # So an unincorporated source is a note, with one exception the gate has
    # to earn: an `essential` request it justified, pressed, and could not
    # settle. That is two readers disagreeing about completeness, and it
    # surfaces as a reservation on the answer and a distinct outcome — never
    # as `partial`, which keeps meaning a part could not be answered.
    unaccounted = await obligations.unaccounted(run_id, answer_id)
    # Still read, still fed, still reported. What changed is that it drives
    # cycles rather than verdicts: while a cycle is left and a source is owed,
    # the run spends it; when the cycles run out, the answer publishes.
    blocking = await obligations.actionable(run_id, answer_id)
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
    # Read before the cycle is recorded, because the record carries the count
    # and the reviser's feedback is built from the same read. Two reads could
    # disagree, and a telemetry key that disagrees with the feedback it
    # describes is worse than no key.
    mismatched, mismatch_failed = await claim_naming.safe_mismatches_for(answer_id)
    # A check that cannot run says so where its findings go. An opted-in class
    # with no declared identifier shape yields no mismatches, and no mismatches
    # is what a clean answer yields too, so the silence rides `issues` rather
    # than a telemetry key nobody reads unless already suspicious.
    unshaped = await claim_naming.unshaped_message()
    # How far each check got, beside what it found. Recorded every run so a
    # batch can be compared with the one before it and a check that stopped
    # reaching anything is visible without anyone deciding to look.
    reach = await claim_naming.reach_for(answer_id)
    mismatch_notes = [
        claim_naming.mismatch_message(m)
        for m in mismatched[:claim_naming.MAX_FINDINGS]
    ]
    # Read once and used twice, for the reason the coverage read above is:
    # the per-cycle record and the run-scoped counts must describe the same
    # ledger, and two reads could disagree.
    ledger_record = await objections.record(run_id)
    await _record_gate_cycle(
        run_id, reparse=reparse, unaccounted=unaccounted,
        fed=[{"doc": u["doc"], "feeds": int(u["fed"]) + 1} for u in feed],
        system_waived=[u["doc"] for u in capped],
        parts=[{"asks": str(x.get("asks") or "")[:300],
                "covered": bool(x.get("covered")),
                "accounted": not unaccounted,
                "gap": str(x.get("gap") or "")[:300],
                "covered_by": x.get("covered_by") or [],
                "covered_by_missing": x.get("covered_by_missing") or [],
                "covered_by_unsupported": x.get("covered_by_unsupported") or [],
                "covered_by_unresponsive": x.get("covered_by_unresponsive") or []}
               for x in parts],
        coverage=_coverage_summary(parts),
        naming_mismatches=[
            {"claim": m.seq, "names": list(m.named), "cites": list(m.cited)}
            for m in mismatched
        ],
        checks=reach,
        # Recorded whether or not it found anything. A re-read that found
        # nothing and a re-read that never ran are the same silence
        # otherwise, and telling an absent limb from an unchecked one is half
        # of why this is worth having.
        reread={"looked": reread_looked, "found": reread_found},
        standing_counts=standing_counts,
        # The argument as it stands at this cycle: every request, what it
        # asked, the importance the gate had to justify, the drafter's reason,
        # the gate's ruling and the cycle each happened in. Recorded per cycle
        # rather than once at the end, because the interesting question about a
        # two-way loop is when a point turned, and an end-state snapshot cannot
        # answer it.
        objection_ledger=ledger_record,
        closing=_closing_record(data, claims_by_seq, parts))
    # The three numbers the standing rule has to be able to show, run-scoped
    # and flat beside the per-cycle counts above: how many standing
    # objections this run raised, how many the answer settled by citing the
    # higher-standing source, and how many the drafter refused with a reason.
    #
    # The third is the one that was never observable. Across three live runs
    # the drafter refused nothing, because evidence findings are usually
    # correct and there was nothing to argue — so whether a reasoned refusal
    # could happen at all was a question nobody could answer from a run's
    # telemetry. A standing objection is precisely the case where refusing is
    # the right move, and this is the count that says whether it is reached.
    await _tele(run_id, "validate", standing=standing.summary(ledger_record))
    # A claim can attribute something to a source and never say which source.
    # The evidence checker cannot see that: it asks whether stated provenance
    # is correct, and unstated provenance is not wrong. So it is checked here,
    # deterministically, and only ever as an issue. The claim is true; it is
    # written so the reader cannot follow it, which is not grounds to hold an
    # answer back.
    #
    # The mismatch notes go first. They report the opposite defect and a worse
    # one: not a source the claim declines to name, but a source it names
    # wrongly, which sends the reader somewhere rather than nowhere. They are
    # issues too, for now. One observed true positive against 475 published
    # claims is not evidence enough to hold answers back on, and moving them
    # to `correctness` once a sweep has measured the rate is a change to this
    # line alone.
    issues += mismatch_notes
    if unshaped:
        issues.append(unshaped)
    if mismatch_failed:
        issues.append(mismatch_failed)
    issues += await claim_naming.issues_for(answer_id)
    issues += await supersession.issues_for(answer_id)
    # every uncovered part is a gap the answer must close, not just one
    if uncovered:
        missing = "; ".join(uncovered)
    else:
        # The judge's own words first, the debt after it — not instead of it.
        # An owed source is still named here, because `missing` is what the
        # remediation message leads with and what `_gate_key` dedupes on, and a
        # cycle spent on an owed source has to say which one.
        #
        # What it may not do is speak FOR the judge. It used to replace the
        # judge's `missing` outright, so a run the judge rejected as a whole
        # reached the partial note with the debt as its only stated reason, and
        # the note opened by telling the reader the analysis could not cover
        # the material — on a run whose every part was covered. The debt never
        # was the reason; it is a fact beside it.
        missing = "; ".join(x for x in (
            str(data.get("missing") or ""),
            (f"{len(blocking)} source(s) this review named as bearing on the "
             "question are neither cited nor waived: "
             + ", ".join(blocking[:5])) if blocking else "",
        ) if x)
    # An owed source is not a reason to call the question unanswered. It buys
    # revision cycles through `issues` — the caller publishes only once the
    # cycles are spent — and what it cannot do any more is turn a fully
    # covered answer into a `partial` whose note tells the reader the
    # analysis could not cover the question. That note was false on every run
    # it was written for.
    publishable = bool(data.get("publishable")) and not uncovered
    # Which of the two held it, decided here because here is where both are
    # known. `accounting` is gone with the verdict it named: nothing that only
    # concerns an unincorporated source reaches a partial any more.
    #
    # `holistic` is the judge rejecting the answer as a whole: it said
    # publishable false while marking every part covered, so there is no part
    # to point at. Falling through to `coverage` there would report a coverage
    # failure for a run with no uncovered part, which is the same defect one
    # case further along.
    cause = (
        "coverage" if uncovered
        else "holistic" if not bool(data.get("publishable"))
        else ""
    )
    # Coverage gaps first: they are what blocks publication, and the reviser
    # is given passages for a bounded number of points.
    return (publishable, missing, issues + correctness, correctness,
            uncovered + stronger, cause)


def _unused_sources(data: dict, part_count: int = 0) -> list[dict]:
    """The sources the gate named, with how much each matters. Pure.

    Two shapes are read. The object form is what the gate is asked for now —
    filename, the point, an importance and the line that earns it. The old
    `"<filename>: <why>"` string is still read, as `supporting`: a verdict
    written before importance existed named no essential source, and quietly
    promoting one would put a request on the run's verdict that no gate ever
    marked.

    `essential` survives only with a reason. The check is the whole of what
    the word means here — an importance nobody had to justify is not a
    judgment, and this one can hold a reservation against a published answer.
    """
    out: list[dict] = []
    for src in (data.get("unused_sources") or []):
        if isinstance(src, dict):
            fn = str(src.get("filename") or "").strip()
            point = str(src.get("point") or "").strip()
            raw_imp, why = src.get("importance"), src.get("essential_because")
            part = src.get("part")
        else:
            fn, _, point = str(src).partition(":")
            fn, point = fn.strip(), (point.strip() or str(src))
            raw_imp, why, part = objections.SUPPORTING, "", None
        if not fn:
            continue
        importance, why = objections.importance_of(raw_imp, why)
        # The parts the gate is shown are numbered from 1; a claim's
        # `part_index` counts from 0, and a note that lands on the wrong part
        # is worse than one that lands on none. A number outside the
        # decomposition names no part and becomes one: a reservation attached
        # to a part that is never rendered would not be printed at all, which
        # is the one thing a reservation must not do.
        idx = (part - 1 if isinstance(part, int) and not isinstance(part, bool)
               and 1 <= part <= part_count else None)
        out.append({"filename": fn, "point": (point or fn)[:400],
                    "importance": importance, "essential_because": why,
                    "part": idx})
    return out


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
    #
    # And one key per sweep. `final_sweep_result` was flat, and `_tele` merges
    # by key and cannot delete, so a run that swept twice kept only the second
    # finding. That is the fifth field here to need numbering, after `round_N`,
    # `revision_N`, `cycle_N` and `gate_N`, and it loses the more interesting
    # half: the first sweep is the one that spends the repair attempt, so what
    # it objected to and whether the repair answered it were both unreadable.
    # One measured run swept twice and its first finding is gone.
    #
    # `final_sweeps` stays as it is. It is the control-flow counter this
    # function reads back to decide whether the repair chance is spent, and
    # numbering the detail does not change what it means.
    #
    # `final_sweep_dropped`, `final_sweep_dropped_detail` and
    # `final_sweep_published_after_drop` stay flat, and are safe: they are
    # written only in the `sweeps >= 1` branch below, which returns or raises,
    # so at most one sweep per run reaches them. They also share the prefix
    # without being cycles, which is exactly what `_is_numbered` is for.
    cycle = await _next_cycle_key(run_id, "validate", "final_sweep")
    # `judged` and `errors` say whether the sweep looked; `failed` and
    # `overreaching` say what it found. Only the second pair was recorded, and
    # the two readings are the same numbers.
    #
    # `validate_answer_evidence` returns errors beside failures, and an errored
    # span is skipped before any verdict is written: it is not in `failed`, not
    # in `overreaching`, and its row keeps whatever it had. So a sweep where
    # every span errored recorded `failed: 0, overreaching: 0` and returned
    # True, which is character for character what a sweep that judged all forty
    # and objected to nothing records.
    #
    # Recorded, not acted on. The early return below still reads only `failed`
    # and `overreaching`. Making an error object is a behaviour change on a
    # path that has not fired once in 231 published runs — no published answer
    # carries an unjudged or failing span — and it should arrive with evidence
    # from this record rather than on the strength of the argument for it.
    await _tele(run_id, "validate", final_sweeps=sweeps + 1, **{
        cycle: {
            "judged": fv["judged"],
            "errors": len(fv.get("errors") or []),
            "failed": len(fv["failed"]),
            "overreaching": len(f_over),
            "overreaching_claims": _overreach_detail(f_over)}})
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
            ids = [uuid.UUID(str(c)) for c in flagged_ids]
            swept = await _removal_record(s3, ids)
            for cid in ids:
                await s3.execute(
                    ClaimEvidence.__table__.delete()
                    .where(ClaimEvidence.claim_id == cid))
                await s3.execute(
                    AnswerClaim.__table__.delete()
                    .where(AnswerClaim.id == cid))
            await s3.commit()
        # An overreach verdict never fails an evidence row, so these claims are
        # removed on the sweep's judgment alone and nothing downstream would
        # otherwise say which they were.
        await _tele(run_id, "validate", final_sweep_dropped=len(flagged_ids),
                    final_sweep_dropped_detail=swept)
        await _record_removal(run_id, "final_sweep", swept)
        # A fixable defect in one claim must not demote the whole answer.
        # The flagged claims are gone — visibly: the drop is recorded and
        # the numbering keeps the gap — so re-ask the gate whether what
        # survives still answers the question. If it does, publish; the
        # partial state is reserved for the corpus genuinely not answering,
        # not for the repair budget running out one claim short.
        ok, missing, issues, correctness, _pts, sweep_cause = await _gate_answer(
            sinas, question, answer_id, run_id)
        # This pass opens a cycle like any other, and used to discard its
        # issues because the caller had no use for them. The record did: a
        # cycle holding a fed list and no issues cannot be read, and this one
        # is the re-ask after claims were dropped, which is the cycle a reader
        # most wants the reasons for.
        await _amend_gate_cycle(run_id, redraft=missing, issues=issues,
                                after_drop=True)
        if ok and not correctness:
            await _tele(run_id, "validate", final_sweep_published_after_drop=True)
            return True
        if sweep_cause == "holistic":
            raise PartialOutcome(
                "holistic",
                "after removing claims the final review could not support, the "
                "review rejected the answer as a whole without naming a part it "
                "fails to address" + (f" — {missing}" if missing else ""))
        # There is no `accounting` branch here any more, and its absence is
        # the point: after the drop, a source the review named and the answer
        # did not use cannot turn this into a partial either. The only two
        # partials left on this path are the two that mean the answer is not
        # there — a part gone uncovered, or the review rejecting the whole.
        raise PartialOutcome(
            "coverage",
            "after removing claims the final review could not support, the "
            "surviving claims no longer fully answer the question — "
            + (missing or " ".join(fb))[:600])
    await _revise_answer(sinas, run_id, answer_id, fb, last_attempt=True)
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


# An identifier is a token carrying a digit and joined by a separator:
# T-135/09, C-606/18, 41(1), 1/2003, C/1072/19, M.11936. The pattern is
# structural, not legal — it knows nothing about courts or articles, and the
# same shape picks up a Spanish CNMC case number and an EU merger number that
# a pattern written for EU court references would miss. Measured over 44
# published runs it recognises 31 distinct tokens, 30 of them real citations
# and one COVID-19.
_IDENTIFIER = re.compile(r"\b(?=[^\s]*\d)[A-Za-z0-9]+(?:[-/.]\w+|\(\w+\))+")
# A filename is a citation, and citations are counted separately and exactly.
_FILE_SUFFIX = re.compile(r"\.[a-z]{2,4}$")


def _identifiers(text: str) -> set[str]:
    """Identifier-shaped tokens in a claim. Pure.

    Two exclusions, both to stop this counting something the other check
    already counts exactly, or something that is not an identifier at all:
    a filename belongs to the citation check, and a token of digits and dots
    alone is a decimal or a line range.
    """
    return {
        t for t in _IDENTIFIER.findall(text or "")
        if not _FILE_SUFFIX.search(t) and not re.fullmatch(r"[\d.\-]+", t)
    }


def _deleted_claims(validate: dict) -> list[dict]:
    """Every claim a run deleted, gathered from the shapes that record one.

    Four keys hold deletions and two pairs of them overlap: the sweep writes
    its claims to both `final_sweep_dropped_detail` and a numbered `removed_N`,
    and the rounds-exhausted path writes both a flat `dropped_detail` and a
    numbered one. Deduplicated on the sequence and the opening of the text,
    which is what distinguishes two deletions of different claims.

    Reads the flat legacy keys as well as the numbered ones, so a run recorded
    before the numbering existed is still readable here — those records carry
    no citations, so only the mention check can say anything about them.

    Pure: telemetry in, records out.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for key in sorted(validate):
        v = validate[key]
        # Each shape names its own list. Read by a fallback chain instead and
        # `revision_N` hands back its `claims` count, which is an integer.
        if key in ("dropped_detail", "final_sweep_dropped_detail"):
            entries = v
        elif re.fullmatch(r"revision_\d+", key) and isinstance(v, dict):
            entries = v.get("dropped_detail")
        elif re.fullmatch(r"removed_\d+", key) and isinstance(v, dict):
            entries = v.get("claims")
        else:
            continue
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict) or not e.get("claim"):
                continue
            # Everything the record holds, because a sequence is reused as
            # claims are added and dropped and the text alone does not identify
            # a claim: `_removal_record` stores 400 characters, and 23 of the
            # 70 stored records are at that cap. Two long claims differing only
            # past it would read as one record written twice, and the second
            # one's citations would reach neither check. The citations are the
            # field these checks consume, so they belong in the identity.
            fp = (e.get("sequence"), str(e["claim"]),
                  tuple(sorted(str(c) for c in (e.get("cites") or []))))
            if fp in seen:
                continue
            seen.add(fp)
            out.append(e)
    return out


def _seqs(entries: list[dict]) -> list[int]:
    out = []
    for e in entries:
        try:
            out.append(int(e.get("sequence")))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _last_citation_losses(deleted: list[dict], live: set[str]) -> list[dict]:
    """Documents the answer cited before a deletion and does not cite now.

    NOT a coverage check. It does not know whether the point the deleted claim
    made survives somewhere else, and it is not evidence that anything is
    missing. It says one thing exactly: a document that was carrying a citation
    in this answer is no longer carrying one.

    That is worth its own record because nothing else in the pipeline would
    ever mention it. An answer that quietly stops citing a publisher's own
    material still reads as a good answer.

    Pure.
    """
    by: dict[str, list[dict]] = {}
    for e in deleted:
        for d in (e.get("cites") or []):
            if d and d not in live:
                by.setdefault(str(d), []).append(e)
    return [{"document": d, "sequences": _seqs(v)} for d, v in sorted(by.items())]


def _last_mention_losses(deleted: list[dict], live: set[str]) -> list[dict]:
    """Identifiers a deleted claim named that no surviving claim names.

    The companion to the citation check and the same kind of statement: the
    answer stopped naming T-135/09, not the answer no longer covers T-135/09.
    The two do not coincide. A deletion can take the last citation of a
    document while the identifier survives in a claim citing something else,
    and it can take the last mention of a case while the document it cited
    stays cited by a different claim.

    Neither check sees the case that prompted both. One run deleted a claim
    recording that inspectors imaged employees' drives and indexed them
    overnight, on the reasoning that a later judgment superseded it. Both the
    document and the case number survive elsewhere in that answer; what left
    was a fact inside a document still cited, and no set difference over
    citations or identifiers can see that.

    Pure.
    """
    by: dict[str, list[dict]] = {}
    for e in deleted:
        for t in _identifiers(str(e.get("claim") or "")):
            if t not in live:
                by.setdefault(t, []).append(e)
    return [{"identifier": t, "sequences": _seqs(v)} for t, v in sorted(by.items())]


async def _open_notes(raw: list[dict]) -> list[dict]:
    """The objection ledger as notes an answer can carry.

    One thing happens here that the ledger cannot do for itself: the source is
    resolved from the filename it was argued about to the citation a reader is
    given. A reservation printed in the answer must name the source the way
    every other sentence in the answer names one — by what it is, never by a
    storage name — and the renderer stays pure because the lookup happens
    here, once, at publish.

    A filename that resolves to nothing keeps the note and loses the name:
    what was asked and why it was declined is the substance, and a note that
    vanished because a document row moved would hide the disagreement rather
    than the filename.
    """
    names = sorted({str(n.get("source")) for n in raw if n.get("source")})
    if not names:
        return list(raw)
    docs: dict[str, str] = {}
    async with AsyncSessionLocal() as session:
        from app.models import DocumentClassProperty, PropertyValue
        from app.services.document_identity import document_title_subquery

        rows = (await session.execute(
            select(Document.id, Document.filename, DocumentClass.name,
                   DocumentClass.identifier_property,
                   document_title_subquery())
            .outerjoin(DocumentClass,
                       DocumentClass.id == Document.document_class_id)
            .where(Document.filename.in_(names))
        )).all()
        by_id = {did: {"title": title, "class": cls,
                       "identifier_property": ident_prop, "properties": {}}
                 for did, _fn, cls, ident_prop, title in rows}
        if by_id:
            for did, prop, value in (await session.execute(
                select(PropertyValue.document_id, DocumentClassProperty.name,
                       PropertyValue.value)
                .join(DocumentClassProperty,
                      DocumentClassProperty.id == PropertyValue.property_id)
                .where(PropertyValue.document_id.in_(list(by_id)))
            )).all():
                entry = by_id.get(did)
                if entry is not None and value is not None:
                    entry["properties"][str(prop)] = value
            # The renderer is pure and is handed the identity rather than the
            # properties to find it in: the same three fields `assemble`
            # writes, from the same declarations.
            roles = await declared_roles.resolve(session)
            for entry in by_id.values():
                props = {str(k): answer_structure.unwrap(v)
                         for k, v in (entry.get("properties") or {}).items()}
                cls_name = str(entry.get("class") or "")
                ident_prop = entry.pop("identifier_property", None)
                entry["identifier"] = (props.get(ident_prop) if ident_prop
                                       else None)
                entry["alternate_identifier"] = roles.value(
                    roles.alternate_identifier, props, cls_name)
                entry["date"] = answer_structure.document_date(
                    props, cls_name, roles.date)
        for did, fn, _cls, _ident_prop, _title in rows:
            docs[fn] = answer_render.citation(by_id.get(did))
    return [{**n, "source_citation": docs.get(str(n.get("source")) or "")}
            for n in raw]


async def _final_status(run_id: uuid.UUID) -> str:
    """`published`, or `published_contested` when a disagreement survived.

    A run is contested when the review called a source essential, said in one
    line which part is not properly answered without it, pressed the point,
    and the drafter still would not use it. That is not a failure — the answer
    is written and every part is covered — and it is not silence either: a
    human should read the reservation and decide. So it gets a status of its
    own, between `published` and `partial`, and `partial` keeps meaning what
    it has always meant.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        v = ((run.telemetry if run is not None else None) or {}).get("validate") or {}
    return "published_contested" if (v.get("contested") or []) else "published"


async def _publish_answer(run_id: uuid.UUID, answer_id: uuid.UUID, **tele: Any) -> None:
    from app.models import Answer

    # What was argued and did not settle, written onto the answer before the
    # prose is assembled so the reservations are IN the published text rather
    # than beside it. A reader who has only the answer must still learn that
    # the review thought a part incomplete.
    notes = await _open_notes(await objections.notes(run_id))
    contested = [n for n in notes if n.get("caveat")]
    async with AsyncSessionLocal() as session:
        row = await session.get(Answer, answer_id)
        row.status = "published"
        row.published_at = _now()
        row.open_notes = notes or None
        await _compact_claim_sequences(session, answer_id)
        await session.commit()
        # The prose, written once the claim numbering is final. Stored rather
        # than rendered on every read so the published text is a fact about
        # the answer and not about whatever the renderer does next month; the
        # endpoint re-assembles on demand for anything that changes after.
        # A failure here must not unpublish an answer that is otherwise
        # complete: the rows are the record and the text is regenerable.
        try:
            rendered = await answer_render.assemble(
                session, answer_id, fallback_as_at=_now().date())
            if rendered is not None:
                row.rendered_markdown = rendered.markdown
                if row.law_stated_as_at is None:
                    row.law_stated_as_at = rendered.law_stated_as_at
                await session.commit()
                tele = {**tele, "rendered_chars": len(rendered.markdown),
                        "rendered_citations": len(rendered.citations)}
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            _log.warning("run %s: could not render the answer markdown: %s",
                         run_id, exc)
            tele = {**tele, "render_error": str(exc)[:200]}
        # Read after the commit, and only here. "Last" is a claim about the
        # finished answer: during revision a document can lose its last
        # citation and get another one two cycles later, so the same
        # comparison made at deletion time reports losses that did not happen
        # and misses ones that had not happened yet.
        live = (await session.execute(
            select(AnswerClaim.claim_text, Document.filename)
            .outerjoin(ClaimEvidence, ClaimEvidence.claim_id == AnswerClaim.id)
            .outerjoin(Document, Document.id == ClaimEvidence.document_id)
            .where(AnswerClaim.answer_id == answer_id)
        )).all()
        run = await session.get(QueryRun, run_id)
        validate = ((run.telemetry if run is not None else None)
                    or {}).get("validate") or {}
    deleted = _deleted_claims(validate)
    live_docs = {fn for _, fn in live if fn}
    live_ids: set[str] = set()
    for text, _ in live:
        live_ids |= _identifiers(text or "")
    # Written on every publish, empty lists included. An empty list says the
    # comparison ran and found nothing; a missing key says the run predates
    # the comparison. Those are different facts and a reader needs both.
    await _tele(run_id, "validate", published=_iso(),
                lost_last_citation=_last_citation_losses(deleted, live_docs),
                lost_last_mention=_last_mention_losses(deleted, live_ids),
                open_notes=notes,
                # The whole argument in one place, flat, at the one moment it
                # is final. The per-cycle copies show how it got here; a reader
                # asking what was argued should not have to find the last gate
                # cycle to learn how it ended.
                objections=await objections.record(run_id),
                # And the other argument the loop has: the review against one
                # claim, round after round. Per claim, how many findings it
                # attracted, how it ended, and whether a reword of it was
                # refused — the three numbers that say whether the two-strike
                # rule is doing anything.
                claim_strikes=await strikes.record(run_id),
                # The one thing in the ledger that changes the run's outcome.
                # Written every publish, empty list included: an empty list
                # says nothing was left contested, a missing key would say the
                # run predates the loop, and `_final_status` reads it to decide
                # between `published` and `published_contested`.
                contested=contested,
                **tele)


def _spans_of(obj: dict) -> list[dict]:
    """Citable spans only: a filename and a line number, or it is not one."""
    return [
        {"filename": str(e["filename"]), "line_from": int(e["line_from"]),
         "line_to": int(e.get("line_to") or e["line_from"]),
         "locator": _locator_of(e)}
        for e in (obj.get("evidence") or [])
        if isinstance(e, dict) and e.get("filename")
        and str(e.get("line_from", "")).lstrip("-").isdigit()
    ]


# An abstention says, in the answer, that the sources do not settle a point.
# It carries no evidence, so it is the one claim the faithfulness machinery
# cannot check — which is why it is allowed only on the last revision, capped,
# and still judged by the gate.
MAX_ABSTENTIONS = 2


#: What a patch item may say about where a claim belongs. Four keys left
#: this tuple with the drafting schema: `section`, which follows from
#: `kind`, and the three notes about the source, which follow from the
#: document the claim cites. `conditions` and `test_name` are the flat shape
#: a test arrives in; `test` is the older nested one, still read.
_STRUCTURE_KEYS = ("part", "kind", "follows_from", "test", "conditions",
                   "test_name")


def _structure_of(c: dict) -> dict:
    """The structure fields a patch item carries, and only those it carries,
    so an absent key means "leave it" rather than "clear it". Pure."""
    return {k: c[k] for k in _STRUCTURE_KEYS if k in c and c[k] is not None}


def _derived_patch_item(c: dict) -> bool:
    """May this patch item stand without a span? Pure.

    A claim that rests on other claims rather than on passages: an inference
    always, and a conclusion that cites nothing and names what it follows
    from. That rule is `answer_structure.is_derived` and is asked here rather
    than restated, because a second copy of it is what this function was.

    It admitted inferences only. The gate's own correctness finding is "the
    answer has no overall conclusion: add one claim of kind conclusion with
    part null" — a claim that by construction cites nothing and follows from
    others. The drafter wrote it, this dropped it for having no span, the
    gate asked again, and the run ended "could not be made internally
    consistent" having been handed the thing it asked for every cycle.
    """
    kind = str(c.get("kind") or c.get("type") or "").strip().lower()
    return answer_structure.is_derived(
        kind, bool(_spans_of(c)), answer_structure.ref_list(c.get("follows_from")))


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

    drop_reasons: dict[int, str] = {}
    drop_objections: dict[int, str] = {}
    revise = []
    for c in (data.get("revise") or []):
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        spans = _spans_of(c)
        # An inference rests on the claims it follows from and needs no span
        # of its own; everything else needs at least one.
        derived = _derived_patch_item(c)
        if len(text) >= 30 and (spans or derived) \
                and str(c.get("seq", "")).lstrip("-").isdigit():
            revise.append({"seq": int(c["seq"]), "text": text, "evidence": spans,
                           "rationale": str(c.get("rationale") or "").strip(),
                           **_structure_of(c)})

    add = []
    abstentions = 0
    for c in (data.get("add") or []):
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        spans = _spans_of(c)
        if len(text) < 30:
            continue
        if spans or _derived_patch_item(c):
            add.append({"text": text, "type": c.get("type") or c.get("kind"),
                        "evidence": spans,
                        "rationale": str(c.get("rationale") or "").strip(),
                        **_structure_of(c)})
        elif (allow_abstention
              and str(c.get("type") or "").lower() == "abstention"
              and abstentions < MAX_ABSTENTIONS):
            abstentions += 1
            add.append({"text": text, "type": "abstention", "evidence": [],
                        "rationale": str(c.get("rationale") or "").strip()})

    # A drop costs a reason, like every other disposition. Revising needs text
    # and spans, adding needs those plus a type, keeping needs a rationale, and
    # waiving needs twenty characters of one. Dropping was a bare integer, and
    # it is the disposition that removed two claims from one run — a reasonable
    # time limit under Article 41(1), and the legality of copying a medium in
    # its entirety — with nothing recorded about why, and nothing surviving
    # that covers either.
    #
    # A drop without a reason is not applied, which is what this file already
    # does to a waive whose rationale is too short: a disposition that does not
    # meet its contract does not take effect. The refused sequences are
    # returned so the caller can record them, because the interesting failure
    # is the reviser declining to explain rather than the claim surviving one
    # more cycle.
    drop, drop_unexplained = [], []
    for x in (data.get("drop") or []):
        if isinstance(x, dict):
            # The same digit test revise and keep apply to a sequence, for the
            # same reason and with more at stake: int() reads 9.5 as claim 9
            # and True as claim 1, and what comes out of here gets deleted.
            # A sequence that is not written as a whole number names no claim.
            if not str(x.get("seq", "")).lstrip("-").isdigit():
                continue
            seq = int(x["seq"])
            why = str(x.get("rationale") or "").strip()
            (drop if len(why) >= 20 else drop_unexplained).append(seq)
            if len(why) >= 20:
                drop_reasons[seq] = why[:400]
                # The measured case, given somewhere to go. A claim dropped
                # BECAUSE the source the gate demanded cannot carry the point
                # is a refusal of that request, and the reason for the drop is
                # the reason for the refusal. Naming the request is what turns
                # the two into one move instead of a deletion the gate reads
                # as silence.
                oid = str(x.get("objection") or "").strip()
                if oid:
                    drop_objections[seq] = oid
        elif str(x).lstrip("-").isdigit():
            # The old bare-integer shape. Read as a drop the reviser declined
            # to explain rather than rejected outright, so the refusal is
            # visible in the telemetry instead of looking like a parse failure.
            drop_unexplained.append(int(x))

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
            keep.append({"seq": int(c["seq"]), "rationale": why,
                         # A keep can also be an answer to a request the gate
                         # made. When it names one, the reason travels to the
                         # objection ledger and the gate has to rule on it.
                         "objection": str(c.get("objection") or "").strip()})

    # A reasoned refusal: the gate asked for something and the drafter will
    # not do it, and says why. Before this existed the reasoning had nowhere
    # to go — one measured run wrote it into a dropped claim's rationale, from
    # where nothing read it, and the gate fed the same document back twice.
    # It is not a claim operation, so it carries no sequence and touches no
    # row; it is a reply, and the only thing it needs is the id of what it
    # answers and a reason worth ruling on.
    refuse = []
    for r in (data.get("refuse") or []):
        if not isinstance(r, dict):
            continue
        oid = str(r.get("objection") or r.get("id") or "").strip()
        why = str(r.get("rationale") or "").strip()
        if oid and len(why) >= 20:
            refuse.append({"objection": oid, "rationale": why[:400]})

    waives = []
    for w in (data.get("waive") or []):
        if isinstance(w, dict) and str(w.get("doc") or "").strip() \
                and len(str(w.get("rationale") or "").strip()) >= 20:
            waives.append({"doc": str(w["doc"]).strip(),
                           "rationale": str(w["rationale"]).strip()})
    if not (revise or add or drop or keep or waives or refuse
            or drop_unexplained):
        return None
    return {"revise": revise, "add": add, "drop": drop, "keep": keep,
            "drop_reasons": drop_reasons, "drop_objections": drop_objections,
            "drop_unexplained": drop_unexplained,
            "waive": waives, "refuse": refuse}


def _cycle_key(existing: dict, prefix: str) -> str:
    """The next per-cycle telemetry key, `prefix_1` upward.

    `_tele` merges by key and cannot delete, so a stage that writes the same
    key every cycle keeps only its last one. `round_N` already avoids that by
    numbering; this is the same trick for stages whose loop has no counter of
    its own to number by.

    Only `prefix_<digits>` counts. Matching on the prefix alone was safe while
    no flat key shared one, and stopped being safe the moment `gate` needed
    numbering: `gate_parts`, `gate_issues`, `gate_redraft`, `gate_reparse`,
    `gate_unaccounted` and `gate_unparseable` all start with `gate_`, so the
    first cycle of a run would have been called `gate_7`.
    """
    return f"{prefix}_{sum(1 for k in existing if _is_numbered(k, prefix)) + 1}"


def _is_numbered(key: str, prefix: str) -> bool:
    """`prefix_12` yes, `prefix_parts` no. Pure."""
    return key.startswith(prefix + "_") and key[len(prefix) + 1:].isdigit()


async def _next_cycle_key(run_id: uuid.UUID, stage: str, prefix: str) -> str:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        entry = ((run.telemetry if run is not None else None) or {}).get(stage) or {}
    return _cycle_key(entry, prefix)


def _claim_budget_line(live: int, refused_last_cycle: int = 0,
                       cap: int = MAX_CLAIMS) -> str:
    """What the reviser may add, said before it writes rather than after.

    `_admit_adds` refuses an addition once the answer is at MAX_CLAIMS and
    tells nobody: the claim is not written, the reviser is not informed, and
    the run continues as though it had been. Measured over 131 revision cycles,
    23 of the 56 additions the reviser proposed were discarded that way, 41% of
    everything it tried to add. Three cycles changed nothing at all and all
    three were cap refusals, so the run paid for a model call and got back a
    byte-identical answer. 38 of the 131 ran with the answer already full.

    The consequence is not that the reviser deletes too much. It dropped 14
    claims across those same cycles, fewer than the cap silently refused. It is
    that a full answer cannot take back anything an earlier revision narrowed
    away, and the only move that makes room is a drop, which is the one
    disposition that costs nothing to write.

    Pure: two counts in, the sentence out.
    """
    room = max(0, cap - live)
    out = f"The answer holds {live} claims and the hard maximum is {cap}. "
    if room:
        out += (f"You may add up to {room}. An addition beyond that is "
                "discarded, not queued. ")
    else:
        out += ("It is FULL. Any claim you add is discarded unless the same "
                "patch drops one to make room, so an addition you want must "
                'come with a "drop". ')
    if refused_last_cycle:
        out += (f"Your previous reply proposed {refused_last_cycle} addition(s) "
                "that were discarded for exactly this reason; they were never "
                "written. Free a slot if you still want them. ")
    # The target sits a little under the cap: a one-part question aims at 12
    # under its cap of 14 as it always did, and a question with more parts
    # aims correspondingly higher.
    target = 12 if cap <= MAX_CLAIMS else cap - 2
    return out + f"Aim for about {target} claims.\n\n"


def _admit_adds(adds: list[dict], live: int,
                cap: int = MAX_CLAIMS) -> tuple[list[dict], int]:
    """Split the reviser's additions into those the answer has room for and
    the count refused, given `live` claims already in it.

    Pure, and separate, because the refusal is silent everywhere else: an
    answer at MAX_CLAIMS takes nothing, the reviser is not told, and the run
    continues as though the claim had been written. Returning the refused
    count is what lets the caller record a cap hit instead of recording an
    addition that never happened.
    """
    room = max(0, cap - live)
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
                  "char_from": None, "char_to": None, "note": None,
                  "locator": sp.get("locator")},
            validated=False))


async def _cap_refusals_last_cycle(run_id: uuid.UUID) -> int:
    """How many additions the previous revision cycle lost to the cap.

    Read from `revision_N` rather than threaded through the caller: the refusal
    happens after the reviser has already replied, so the only place to tell it
    is the next prompt, and by then the number lives in telemetry. Highest
    cycle by number, not by string, since `revision_10` sorts before
    `revision_2` and runs pass ten cycles.
    """
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        v = ((run.telemetry if run is not None else None) or {}).get("validate") or {}
    cycles = [k for k in v
              if k.startswith("revision_") and k[len("revision_"):].isdigit()]
    if not cycles:
        return 0
    latest = max(cycles, key=lambda k: int(k[len("revision_"):]))
    return int((v.get(latest) or {}).get("add_dropped_at_cap") or 0)


async def _record_removal(run_id: uuid.UUID, path: str,
                          entries: list[dict]) -> None:
    """One numbered record per deletion event.

    `_tele` merges by key, and both paths that delete outside a revision cycle
    wrote a flat key. `_stage_validate_publish` recurses and the pre-publish
    sweep runs more than once, so a second deletion overwrote the first and the
    earlier claims and their citations vanished from the telemetry that exists
    to keep them.

    Numbered `removed_N` rather than `dropped_N`. When this was written
    `_cycle_key` counted any key starting with the prefix, and `dropped_claims`
    and `dropped_detail` are both flat keys under `validate`, so a run's first
    deletion would have been called `dropped_3`. The gate-cycle change has
    since tightened `_cycle_key` to `prefix_<digits>`, so `dropped_` would work
    now too; `removed_` stays because the history is a different thing from
    those two flat keys, which carry only the latest state, and one prefix per
    meaning keeps them apart when read.

    The reviser's drop is not written here. It already sits inside `revision_N`,
    which is numbered, and that is where a reader of a revision cycle looks for
    what that cycle removed.
    """
    if not entries:
        return
    key = await _next_cycle_key(run_id, "validate", "removed")
    await _tele(run_id, "validate", **{key: {
        "at": _iso(), "path": path, "count": len(entries), "claims": entries}})


async def _removal_record(session, claim_ids: list) -> list[dict]:
    """What these claims say and cite, read before they are deleted.

    Three paths delete a claim and each recorded something different: the
    reviser's drop kept a count, the sweep's forced drop kept a count, and only
    the rounds-exhausted drop kept the text. None of the three kept what the
    claim cited, and the evidence rows go with the claim, so afterwards there
    is no way to ask whether a deletion took the last citation of a source the
    gate had named. Measured over the stored runs: 160 claims deleted, 123 with
    their text, 0 with their citations.

    Called before the delete, in the caller's session, so what is recorded is
    what was actually there.

    `why` is only ever present on the rounds-exhausted path, where a failing
    verdict explains itself; the other two paths delete for a reason the caller
    already knows and states separately.
    """
    if not claim_ids:
        return []
    rows = (await session.execute(
        select(AnswerClaim.id, AnswerClaim.sequence, AnswerClaim.claim_text,
               Document.filename)
        .outerjoin(ClaimEvidence, ClaimEvidence.claim_id == AnswerClaim.id)
        .outerjoin(Document, Document.id == ClaimEvidence.document_id)
        .where(AnswerClaim.id.in_(list(claim_ids)))
    )).all()
    out: dict = {}
    for cid, seq, text, fn in rows:
        e = out.setdefault(cid, {"sequence": seq,
                                 "claim": (text or "")[:400], "cites": []})
        if fn and fn not in e["cites"]:
            e["cites"].append(fn)
    return sorted(out.values(), key=lambda d: d["sequence"])


def _apply_structure(row: AnswerClaim, item: dict, parts: list[dict],
                     id_by_seq: Mapping[int, uuid.UUID], added: bool = False,
                     sources: Mapping[str, dict] | None = None) -> None:
    """Write the structure fields a patch item carries onto a claim row.

    A revised claim keeps whatever the patch does not mention; an added claim
    is normalised whole, so it always lands in a section. `follows_from`
    arrives as sequence numbers and is stored as claim ids; a number naming
    no live claim, or the claim itself, is dropped.

    What the claim says about its SOURCE is not the patch's to carry: the
    label, the tier, the jurisdiction note and the currency note are set from
    the document the item cites, exactly as the drafting path sets them. A
    revision rebinds evidence, so a claim that moves to another document must
    move to that document's labels with it — leaving the old ones on the row
    is how an answer ends up calling a source something it is not.
    """
    carries = {k for k in ("part", "kind", "type", "test", "conditions",
                           "test_name") if item.get(k) is not None}
    if added or carries:
        cols = answer_structure.normalise_claim(
            {**item, "text": row.claim_text,
             "kind": item.get("kind") or item.get("type") or row.claim_kind,
             "part": item.get("part") if "part" in item else (
                 None if added or row.part_index is None else row.part_index + 1)},
            parts)
        if cols is not None:
            for k in ("section", "part_index", "part_label", "claim_kind",
                      "claim_type", "test"):
                if added or k in ("section", "part_index", "part_label") or \
                        k in carries or (k == "claim_kind" and "kind" in carries):
                    setattr(row, k, cols[k])
    _apply_source_facts(row, item.get("evidence"), sources)
    refs = answer_structure.ref_list(item.get("follows_from"))
    if refs:
        ids = [str(id_by_seq[r]) for r in refs
               if r in id_by_seq and id_by_seq[r] != row.id]
        row.follows_from = ids or None


def _apply_source_facts(row: AnswerClaim, evidence: Any,
                        sources: Mapping[str, dict] | None) -> None:
    """Set the four source-derived columns from the first document cited.

    Shared by drafting and revision so a claim carries the same labels
    whichever path last touched it. Silent when the claim cites nothing the
    retrieved set knows about: that is a claim whose evidence did not bind,
    and it has bigger problems than its label.
    """
    if not sources:
        return
    fn = next((str(e.get("filename")) for e in (evidence or [])
               if isinstance(e, dict) and e.get("filename")), None)
    src = sources.get(fn or "") if fn else None
    if not src:
        return
    row.authority_label = (str(src["label"])[:40] if src.get("label") else None)
    row.authority_tier = (int(src["tier"]) if src.get("tier") is not None
                          else None)
    row.jurisdiction_note = (str(src["jurisdiction"])[:300]
                             if src.get("jurisdiction") else None)
    row.currency_note = (str(src["currency"])[:500] if src.get("currency")
                         else None)


#: A source file named in prose. Every document in a collection is addressed
#: by its filename, and that is how a refusal names one — "claim 5 already
#: cites founding-judgment.md". Anything up to whitespace or a quote or
#: bracket, ending in the extension: filenames carry dots, dashes, underscores.
_FILE_TOKEN = re.compile(r"[^\s\"'`(),;\[\]<>]+\.md\b")


def _named_files(text: str) -> list[str]:
    """The source files a piece of prose names, in order, once each. Pure."""
    return list(dict.fromkeys(_FILE_TOKEN.findall(text or "")))


async def _cited_filenames(answer_id: uuid.UUID) -> set[str]:
    """Every file some claim of the answer currently cites."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Document.filename)
            .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
            .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
            .where(AnswerClaim.answer_id == answer_id))).scalars().all()
    return {str(f) for f in rows if f}


async def _record_refusals(run_id: uuid.UUID, answer_id: uuid.UUID,
                           patch: dict) -> int:
    """The patch's replies to the gate's requests, into the objection ledger.

    Three shapes, one meaning. An explicit `refuse` entry is a reply and
    nothing else. A `drop` or a `keep` that names an objection is a reply
    made by doing something to a claim — the claim citing the demanded source
    is removed, or the claim the gate wanted re-sourced stays — and its
    rationale is the reason. Recording all three here keeps the ledger's view
    of "the drafter answered" from depending on which disposition the reviser
    happened to reach for.

    One thing a reply may not do is assert a citation the answer does not
    carry. A refusal that names a source file is checked against the files
    the answer's claims cite — and against the files this same patch cites,
    since a revision landing alongside the refusal may be what adds it — and
    a refusal naming a file found in neither is rejected: the request stays
    open and the drafter is told why. Measured: a refusal of "claim 5 already
    cites the founding judgment" closed a correct objection on a run where
    nothing cited it. Only files are checked, because only files are
    checkable here without a model; a refusal that argues rather than asserts
    is left to the review. And only the patch's CLAIMS count as citing — not
    its replies, or the assertion would vouch for itself.

    Returns how many replies were recorded, for the cycle's telemetry.
    """
    seen: set[str] = set()
    cycle = int((await _next_cycle_key(run_id, "validate", "gate"))
                .removeprefix("gate_")) - 1
    replies = (
        [(r["objection"], r["rationale"]) for r in (patch.get("refuse") or [])]
        + [(oid, (patch.get("drop_reasons") or {}).get(seq, ""))
           for seq, oid in (patch.get("drop_objections") or {}).items()]
        + [(k["objection"], k["rationale"]) for k in (patch.get("keep") or [])
           if k.get("objection")]
    )
    cited: set[str] | None = None
    patch_cites = {str(e.get("filename") or "")
                   for key in ("revise", "add")
                   for c in (patch.get(key) or [])
                   for e in (c.get("evidence") or [])}
    for oid, why in replies:
        if not oid or oid in seen:
            continue
        seen.add(oid)
        named = _named_files(str(why or ""))
        if named:
            if cited is None:
                cited = await _cited_filenames(answer_id) | patch_cites
            missing = [f for f in named if f not in cited]
            if missing:
                _log.info("run %s: refusal of %s names %s, which the answer "
                          "does not cite; the request stays open",
                          run_id, oid, missing)
                await objections.rejected(run_id, oid, why, missing,
                                          cycle=cycle)
                seen.discard(oid)
                continue
        await objections.refused(run_id, oid, why, cycle=cycle)
    return len(seen)


async def _revise_answer(
    sinas: _Sinas, run_id: uuid.UUID, answer_id: uuid.UUID,
    feedback: list[str], extra_points: list[str] | None = None,
    last_attempt: bool = False, removed: list[dict] | None = None,
) -> int:
    """One round of the drafting conversation: what the review found, and what
    the drafter does about it.

    One operation replaces what used to be three — narrowing an overreaching
    claim, rebinding a failed one, and appending claims the gate asked for.
    They were all additive and all returned free text, so an answer could grow
    to 29 claims, could never be made coherent by removing something, and a
    model's reply about the task could land in the answer as a claim.

    What this round SENDS is the change. It used to be a fresh one-shot call
    carrying the passages, the playbook, the structure rules, the standing
    revision instructions and every current claim with the passages bound to
    it. All of that now sits in the brief, at the head of a conversation the
    drafter is still in, so a round carries only what this round found: the
    feedback, the requests outstanding, the engine's numbering, and anything
    the engine removed without being asked.

    The drafter answers with a patch. The patch is applied to the rows; a
    reply that is not a claim object is not a claim, and no text matching
    decides it.
    """
    if not feedback:
        return 0
    from app.models import Answer

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
        answer_row = await session.get(Answer, answer_id)
        parts = [p for p in (getattr(answer_row, "question_parts", None) or [])
                 if isinstance(p, dict)]
    cap = answer_structure.claim_cap(len(parts)) if parts else MAX_CLAIMS
    manifest_rows = await _manifest_rows(parent_id)
    # The list SHOWN to the reviser is capped, because it is prompt and a
    # prompt has a budget.
    corpus_rows = manifest_rows[:60]
    corpus = [r["filename"] for r in corpus_rows if r.get("filename")]
    # The facts are a LOOKUP, and are not capped. A revised claim can cite a
    # document from anywhere in the retrieved set — the gate names owed
    # sources by rank, and obligations reach past sixty — and a document with
    # no entry here silently loses its authority label, its tier, and its
    # jurisdiction and currency notes in the rendered answer. Capping a
    # lookup to the size of a prompt was the whole mistake.
    src_facts = _source_context(manifest_rows)

    # Two claims restating one proposition from one source are found here,
    # by arithmetic, and handed to the reviser as a merge to judge. The
    # finding names both claims, so both arrive with their passages below.
    dup_input: dict[int, dict] = {}
    for claim, _ev, fn, _content in rows:
        d = dup_input.setdefault(claim.sequence, {"sequence": claim.sequence,
                                                  "text": claim.claim_text,
                                                  "docs": set()})
        if fn:
            d["docs"].add(fn)
    dup_pairs = answer_structure.duplicate_pairs(list(dup_input.values()))
    if dup_pairs:
        feedback = list(feedback) + answer_structure.duplicate_feedback(dup_pairs)
        await _tele(run_id, "validate", duplicate_pairs=[list(p) for p in dup_pairs])

    # The answer as it stands, and nothing about it that the drafter can
    # already see. Its claims are its own turns; its passages are the brief.
    # What travels is the engine's NUMBERING, because that is the one thing
    # about its own answer the drafter cannot know: claims are reordered on
    # the way into the database and renumbered again as rounds drop and add
    # them, and a patch keys on that number.
    #
    # This is what a round used to be made of. Every current claim, with the
    # passages bound to it, re-sent in a call that had never seen any of it —
    # the reviser call being where 70% of a run's wall clock went.
    by_claim: dict[int, dict] = {}
    for claim, _ev, _fn, _content in rows:
        by_claim.setdefault(claim.sequence, {"text": claim.claim_text})
    numbering = drafting_chat.numbering_key(
        [(seq, c["text"]) for seq, c in sorted(by_claim.items())])

    # What each claim stands on RIGHT NOW, in the same grain a patch item
    # names its spans in. This is what a revision of a struck claim is
    # measured against: a defence has to reach a passage the claim does not
    # already cite, and that question is only answerable against the rows as
    # they are at the moment the patch arrives.
    cited_now: dict[int, list[dict]] = {}
    for claim, ev, fn, _content in rows:
        if ev is None or not fn:
            continue
        span = ev.span or {}
        cited_now.setdefault(claim.sequence, []).append(
            {"filename": fn, "line_from": span.get("line_from"),
             "line_to": span.get("line_to")})
    # The claims the review has now failed more than once, by the sequence the
    # patch will key on — resolved from the row id every round, so a claim
    # that was renumbered between rounds is still the same claim here.
    strike_ledger = await strikes.entries(run_id)
    on_strike: dict[int, dict] = {}
    for claim, *_rest in rows:
        entry = strike_ledger.get(str(claim.id))
        if entry is None or claim.sequence in on_strike:
            continue
        if int(entry.get("findings") or 0) < strikes.STRIKE_LIMIT:
            continue
        on_strike[claim.sequence] = {
            "claim_id": str(claim.id),
            "sequence": claim.sequence,
            "findings": int(entry.get("findings") or 0),
            "rejected_rewords": int(entry.get("rejected_rewords") or 0),
            "evidence": strikes.fingerprint(cited_now.get(claim.sequence) or []),
        }

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

    chat = await _drafting_chat(run_id, sinas)
    if not chat.chat_id:
        # Drafting opened the conversation. Reaching a revision without one
        # means the run is resuming across a code change or a lost write, and
        # a round with no brief behind it would be a model asked to patch an
        # answer it has never seen.
        _log.warning("run %s: no drafting conversation to revise in", run_id)
        return 0

    # Room for this round is made BEFORE any of it is sent. The new passages
    # below are part of the round, and compacting after them would open a
    # chat they had never reached.
    await chat.prepare()

    # New passages arrive as their own turn, before the feedback that needs
    # them. They are the one thing besides the brief that a round may add to
    # what the drafter knows, and they are sent once: the brief is not
    # rewritten and the passages are not repeated next round.
    if fresh:
        await chat.ask(
            "NEW VERIFIED PASSAGES — extracted since the brief, for the "
            "findings in the next message. They join the passages you already "
            "have and may be cited on the same terms. Nothing else has "
            f"changed.\n{fresh}\n\nReply with the single word "
            f"{drafting_chat.ACK} and wait for the findings.")

    turn = (
        f"REVIEW FINDINGS — round {chat.rounds}. This is what the review "
        "found; everything else stands.\n"
        + numbering
        + drafting_chat.removed_by_the_engine(removed or [])
        + drafting_chat.strike_block(
            [on_strike[s] for s in sorted(on_strike)])
        + "\n"
        + _claim_budget_line(len(by_claim), cap=cap,
                             refused_last_cycle=await _cap_refusals_last_cycle(run_id))
        + "\n\nFINDINGS:\n- " + "\n- ".join(feedback[:MAX_FEEDBACK_ITEMS]) + "\n"
        + drafting_chat.objections_block(await objections.open_points(run_id))
        + ("\nThis is the final revision. If the passages available genuinely "
           "cannot settle a point the question asks about, do not stretch a "
           "source to cover it and do not leave the point unmentioned: add a "
           'claim with "type": "abstention" and no evidence, stating plainly '
           "which part of the question the available sources do not answer. "
           "Say what is missing, not that you are unable — 'The documents "
           "before us do not address X' rather than 'I cannot determine X'. "
           f"At most {MAX_ABSTENTIONS} such claims, and never for the central "
           "question if the sources do answer it.\n" if last_attempt else "")
        + "\nReply with the patch, as JSON and nothing else."
    )
    reply = await chat.turn(turn, label=f"round {chat.rounds}")

    patch = _parse_patch(reply, allow_abstention=last_attempt)
    # The two-strike rule, applied to the reply rather than asked of it. A
    # revision of a claim on its second finding is applied only if it rebinds
    # the claim to evidence it does not already stand on; one that returns the
    # same citation is removed from the patch here and recorded as refused, so
    # a third round spent on the same wording cannot happen by being obeyed.
    rejected_rewords: list[dict] = []
    if patch and on_strike:
        patch, rejected_rewords = strikes.screen_patch(patch, on_strike)
        for r in rejected_rewords:
            await strikes.rejected_reword(run_id, r["claim_id"], r["sequence"],
                                          round_no=chat.rounds)
        if rejected_rewords:
            _log.info("run %s: refused %d reword(s) of struck claim(s) %s",
                      run_id, len(rejected_rewords),
                      [r["sequence"] for r in rejected_rewords])
    refusals = 0
    if patch:
        # A waive is a discharge with a recorded reason, not a dropped
        # message: it is honored even when the rest of the patch is empty.
        for w in patch.get("waive") or []:
            await obligations.waive(run_id, w["doc"], w["rationale"])
        # A refusal is honored on the same terms and for the same reason: it
        # is a REPLY, so it must survive a patch that changes no claim. The
        # reply that mattered on the measured run was exactly that — a reason
        # why a source could not carry the point — and the shape that lost it
        # was "nothing was applied, so nothing is recorded".
        refusals = await _record_refusals(run_id, answer_id, patch)
    if not patch or not (patch["revise"] or patch["add"] or patch["drop"]
                         or patch["keep"]):
        # Numbered like any other cycle, though it changed nothing. The reviser
        # replied, so this IS a cycle, and `_cap_refusals_last_cycle` reads the
        # latest one as "your previous reply". Leaving it unnumbered left an
        # older cycle as the latest, and the next prompt then told the reviser
        # that its last reply had lost additions it never proposed, which is an
        # invitation to drop a sound claim to make room for nothing.
        #
        # Counts are all zero because nothing was applied. `kept_with_reason`
        # is zero here for the same reason and now means it: a patch carrying
        # keeps no longer reaches this branch. It used to — keeps were absent
        # from the condition above, so a reply whose only content was "this
        # citation stands, and here is why" was parsed, counted nowhere and
        # discarded, which is why `kept_with_reason` read 0 on every round of
        # every run. The one disposition built for the reviser to answer back
        # with was the one the early return threw away.
        #
        # Refused drops are the exception, and they have to be. A reply whose
        # only content is drops with no reason lands here rather than below,
        # because nothing was applied — and that reply is the whole point of
        # asking for a reason. If it went unrecorded, the case where the
        # reviser will remove a claim but not say why would be the one case
        # invisible in the telemetry.
        cycle = await _next_cycle_key(run_id, "validate", "revision")
        await _tele(run_id, "validate", revision_yielded_no_change=True, **{
            cycle: {
                "claims": len(by_claim), "revised": 0, "added": 0,
                "dropped": 0, "kept_with_reason": 0, "abstentions": 0,
                "add_dropped_at_cap": 0, "untouched": len(by_claim),
                "feedback_items": len(feedback), "yielded_no_change": True,
                "refusals": refusals,
                "dropped_unexplained": (patch or {}).get("drop_unexplained")
                or [],
                # A patch whose only content was a reword of a struck claim
                # lands here, and this is the one line that says why nothing
                # changed. Without it the cycle reads as a drafter that had
                # nothing to say, when it made a move the rule does not allow.
                "rewords_refused": rejected_rewords}})
        chat.note(f"{refusals} refusal(s); no claim changed"
                  + (f"; {len(rejected_rewords)} reword(s) of a twice-failed "
                     "claim refused" if rejected_rewords else ""))
        await _save_drafting_chat(run_id, chat)
        return 0

    by_seq = {c.sequence: c for c, *_ in rows}
    touched = 0
    # Keeps APPLIED, not keeps proposed. A keep naming a sequence that is not
    # in this answer changes nothing and is skipped below, and counting it
    # would put the same defect back one layer down: a number that says the
    # reviser answered when nothing recorded the answer.
    kept = 0
    # Bound before the add block, which does not run when the patch adds
    # nothing; the telemetry below reads both either way.
    admitted: list[dict] = []
    add_dropped_at_cap = 0
    async with AsyncSessionLocal() as session:
        # Read before deleting. A drop is the one disposition the reviser can
        # write without a rationale, so the text and the citations are the only
        # account there will ever be of what left the answer here.
        dropped_here = await _removal_record(
            session, [c.id for c in (by_seq.get(s) for s in patch["drop"])
                      if c is not None])
        # The reviser's own words for why, beside what the claim said and
        # cited. `_removal_record` reads the database and cannot know them.
        for entry in dropped_here:
            why = (patch.get("drop_reasons") or {}).get(entry["sequence"])
            if why:
                entry["why"] = [why]
        for seq in patch["drop"]:
            claim = by_seq.get(seq)
            if claim is None:
                continue
            await session.execute(ClaimEvidence.__table__.delete()
                                  .where(ClaimEvidence.claim_id == claim.id))
            await session.execute(AnswerClaim.__table__.delete()
                                  .where(AnswerClaim.id == claim.id))
            touched += 1

        id_by_seq = {seq: c.id for seq, c in by_seq.items()}
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
            # The structure moves with the text when the patch says so; a
            # revision that says nothing about it leaves the row where it is.
            _apply_structure(row, item, parts, id_by_seq, sources=src_facts)
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
            admitted, add_dropped_at_cap = _admit_adds(patch["add"], live, cap)
            for item in admitted:
                row = AnswerClaim(answer_id=answer_id, sequence=nxt,
                                  claim_text=item["text"][:4000],
                                  rationale=(item.get("rationale") or "")[:2000]
                                  or None,
                                  claim_type=str(
                                      item.get("type")
                                      or answer_structure.DEFAULT_CLAIM_KIND
                                  )[:50])
                session.add(row)
                await session.flush()
                # An added claim lands in its section and part, positioned
                # after what is there, so the order survives the addition.
                _apply_structure(row, item, parts, id_by_seq, added=True,
                                 sources=src_facts)
                if row.section:
                    same_part = (AnswerClaim.part_index.is_(None)
                                 if row.part_index is None
                                 else AnswerClaim.part_index == row.part_index)
                    row.position = ((await session.execute(
                        select(func.max(AnswerClaim.position))
                        .where(AnswerClaim.answer_id == answer_id)
                        .where(AnswerClaim.section == row.section)
                        .where(same_part)
                    )).scalar() or 0) + 1
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
                # Counted as work done, because it is. A keep is the reviser
                # answering the feedback rather than obeying it, and a round
                # that returns 0 is read by the caller as "revision produced
                # nothing usable" — which would end the loop on the one reply
                # that most needs the next cycle to read it.
                touched += 1
                kept += 1
        await session.commit()

    # How each struck claim's argument ended, recorded against the claim's
    # row id and only for the dispositions that were actually applied. Three
    # moves were on the table and the ledger says which one was taken, so the
    # rule can be read after the fact as an outcome rather than as a rule.
    for seq in patch["drop"]:
        if seq in on_strike and by_seq.get(seq) is not None:
            await strikes.settle(run_id, on_strike[seq]["claim_id"],
                                 strikes.DROPPED, round_no=chat.rounds)
    for item in patch["revise"]:
        if item["seq"] in on_strike and by_seq.get(item["seq"]) is not None:
            await strikes.settle(run_id, on_strike[item["seq"]]["claim_id"],
                                 strikes.DEFENDED, round_no=chat.rounds)
    for item in patch["keep"]:
        if item["seq"] in on_strike and by_seq.get(item["seq"]) is not None:
            await strikes.settle(run_id, on_strike[item["seq"]]["claim_id"],
                                 strikes.REFUSED, round_no=chat.rounds)

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
            "kept_with_reason": kept,
            "abstentions": sum(1 for a in admitted
                               if a.get("type") == "abstention"),
            "dropped": len(patch["drop"]), "dropped_detail": dropped_here,
            # Claims the reviser asked to drop and declined to explain. They
            # were not removed. If this is where the drops go, the requirement
            # is suppressing the disposition rather than documenting it, and
            # that is the thing to know first.
            "dropped_unexplained": patch.get("drop_unexplained") or [],
            # Replies to the gate's requests: refusals, plus the drops and
            # keeps that named one. Beside the claim counts because they are
            # the same reply — what the reviser did AND what it said about
            # what it was asked to do.
            "refusals": refusals,
            # Beside the refusals because it is the same kind of fact: what
            # the drafter tried and the engine would not take. A cycle that
            # revised two claims and had a third reword refused is a different
            # cycle from one that revised two and was asked for nothing else.
            "rewords_refused": rejected_rewords,
            "struck_claims": sorted(on_strike),
            "added": len(admitted),
            "add_dropped_at_cap": add_dropped_at_cap,
            "untouched": len(by_claim) - touched,
            "feedback_items": len(feedback)}})
    # What this round settled, in one line, for the summary that will stand in
    # for it once the conversation reaches its cap. Written from the engine's
    # own record rather than from the transcript: a patch that parsed is not a
    # patch that applied, and the difference is exactly what a later round
    # needs to know.
    chat.note(
        f"revised {len(patch['revise'])}, added {len(admitted)}, dropped "
        f"{len(patch['drop'])}, kept {kept} with a reason, "
        f"{refusals} refusal(s)")
    await _save_drafting_chat(run_id, chat)
    return touched


def _finding_subjects(verdict: dict) -> list[tuple[str, int | None]]:
    """Whose claim each finding is about, in the order `_round_feedback`
    writes them. Pure.

    `(claim id, the sequence it is named by)`, one entry per finding line, so
    the caller can slice this list by exactly the cap it slices the feedback
    by and strike the claims the round actually asks about.

    The id is what the strike ledger counts on. The sequence travels beside it
    for the prompt, which addresses claims by number and knows nothing of row
    ids — and for the record, so a reader can see which number a claim wore
    when it was found.

    Kept next to `_round_feedback` and iterating the same two lists in the
    same order, because the two must stay in step: an entry here that is not
    a line there would strike a claim nobody was asked about.
    """
    out: list[tuple[str, int | None]] = []
    for group in ("failed", "overreaching"):
        for f in (verdict.get(group) or []):
            if not isinstance(f, dict):
                continue
            out.append((str(f.get("claim_id") or ""),
                        _whole(f.get("claim_sequence"))))
    return out


def _whole(raw: Any) -> int | None:
    """A claim sequence, or None if it is not one. Pure.

    The same digit test the verdict readers below apply, and for the same
    reason: int() reads 9.5 as claim 9 and True as claim 1.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return int(n) if n.is_integer() else None


def _round_feedback(verdict: dict, struck: set[int] | None = None) -> list[str]:
    """What this round found, in the words the drafter is asked to act on. Pure.

    The wording carries a decision. "Narrow it to what the passages say" is an
    instruction to adjust, and a model that has just written a claim will
    adjust it — it will reword the same assertion and send it back, and the
    next round will find the same defect. On the measured loop that is what
    ten revision rounds looked like: most touched one or two claims, one
    touched none.

    So abandoning is named as an option in every finding, and named as the
    LIKELY one for a claim that has already been raised once and survived.
    A claim the review has now failed twice is not a wording problem.

    `struck` is the sequences the strike ledger says are on their second
    finding. It used to be assembled here from two lists of sequence numbers
    held in the validation loop's locals, and both halves of that were wrong:
    a sequence is not a claim's identity, and a local does not survive the
    loop restarting itself once per gate cycle. What arrives now is a set of
    CURRENT sequence numbers resolved from claim row ids, and the finding line
    says what the engine will and will not accept rather than asking nicely.
    """
    struck = struck or set()

    def _again(seq: Any) -> str:
        return (" This is the second round that has failed this claim, and "
                "rewording is no longer one of your moves on it: "
                + drafting_chat.PERMITTED_MOVES
                + " Do not reword it again — a revision that comes back with "
                "the citation it already has is not applied."
                if _whole(seq) in struck else "")

    out = [f"Claim {f['claim_sequence']}: {f['reason']} Rebind it to a passage "
           "that carries it, or — if no passage available does — ABANDON the "
           "claim: drop it with a reason. Do not reword it and keep the same "
           "citation." + _again(f.get("claim_sequence"))
           for f in verdict.get("failed") or []]
    out += [f"Claim {o.get('claim_sequence')} asserts more than its "
            f"passages establish: {o.get('uncovered')}. Narrow it to what "
            "the passages say, or bind evidence that carries the rest — and "
            "where what is left after narrowing would say nothing worth "
            "claiming, ABANDON it instead of shrinking it to a truism."
            + _again(o.get("claim_sequence"))
            for o in verdict.get("overreaching") or []]
    return out


def _overreach_seqs(verdict: dict) -> set[int]:
    """The claim numbers this round found overreaching. Pure.

    A verdict entry carries `claim_sequence`; anything that is not a whole
    number names no claim and is dropped rather than guessed at.
    """
    out: set[int] = set()
    for o in (verdict.get("overreaching") or []):
        if not isinstance(o, dict):
            continue
        raw = o.get("claim_sequence")
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            n = float(raw)
        except (TypeError, ValueError):
            continue
        if n.is_integer():
            out.add(int(n))
    return out


def _seqs_of(entries) -> set[int]:
    """The claim numbers named by a list of verdict entries. Pure.

    Same digit test the overreach reader applies, and for the same reason:
    int() reads 9.5 as claim 9 and True as claim 1, and a sequence that is not
    a whole number names no claim.
    """
    out: set[int] = set()
    for f in (entries or []):
        if not isinstance(f, dict):
            continue
        raw = f.get("claim_sequence")
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            n = float(raw)
        except (TypeError, ValueError):
            continue
        if n.is_integer():
            out.add(int(n))
    return out


def _failed_seqs(verdict: dict) -> set[int]:
    """The claim numbers with a span that failed validation this round. Pure."""
    return _seqs_of(verdict.get("failed"))


def _errored_seqs(verdict: dict) -> set[int]:
    """The claim numbers whose span could not be judged this round. Pure.

    An errored row is never marked validated, so it stays pending exactly as a
    failing one does. Errors carried no sequence until this was needed, so a
    verdict from before then names none and this is empty rather than wrong.
    """
    return _seqs_of(verdict.get("errors"))


def _pending_seqs(verdict: dict) -> set[int]:
    """The claim numbers holding a row that will still be pending next round.

    Failed and errored together, because the criterion below cares about one
    property they share: the row is not validated, so the claim is re-judged
    next round whether or not anybody touched it.
    """
    return _failed_seqs(verdict) | _errored_seqs(verdict)


def _still_narrowing(overreach: list[set[int]], pending: list[set[int]]) -> bool:
    """Whether a claim was re-marked overreaching *because it was rewritten*.

    Repetition alone does not prove a rewrite, and the difference is the whole
    correctness of this criterion.

    Evidence is judged with `pending_only=True`. A claim whose spans have ALL
    PASSED holds no pending row, is not re-judged, and produces no coverage
    verdict, so it stops being reported rather than being reported again. For
    that claim, a repeat can only mean the revision bound new spans — revising
    deletes the claim's evidence and re-binds it, and `_bind_spans` writes
    `validated=False` — which puts it back in front of the judge. Repetition is
    proof of movement.

    But a claim carrying a FAILING span keeps that row pending whether anybody
    touches it or not. It is re-judged every round for free, and its coverage
    verdict comes back with it. Repetition there proves nothing, and counting
    it would buy rounds for a claim nobody is working on.

    A span the judge could not read at all does the same thing. An errored row
    is never marked validated, so it is pending on exactly the same terms, and
    the claim comes back for free. It reaches here through `_pending_seqs`,
    which is failed and errored together, because what matters is the property
    the two share rather than which of them happened.

    That is a fact about the EARLIER round, and only the earlier one. A claim
    clean in the earlier round held no pending row, so it came back solely
    because its evidence was re-bound, and it was rewritten. If the rewrite
    then bound a span that failed, that failure is the product of the work,
    not evidence the claim was riding along untouched. Excluding it reads a
    new defect as proof no revision happened, which inverts the signal.

    So the exclusion is `pending[-2]` alone: a sequence holding an unvalidated
    row in the earlier of the two rounds. What survives is a claim that was clean on
    the evidence, was re-judged anyway, and can only have been re-judged
    because it was rebuilt.

    One measured run is the case. Seq 12 was marked overreaching in round 3
    with the round's `failed` at zero, was rewritten from a broad assertion
    into a narrower statement of what bounds it, was marked again in round 4,
    and was deleted when the budget ran out. Under
    `failed[-1] | failed[-2]` it does not qualify, because the rewrite's own
    span failed.

    A claim cannot ride this indefinitely: qualifying requires being clean in
    the earlier round, so one that keeps failing is excluded at the very next
    decision, and HARD_VALIDATE_ROUNDS still binds.

    What this still cannot say is how much better the claim got: the coverage
    verdict is `full` or `partial` with no degree, so "partial again" reads the
    same whether the claim shrank by half or barely changed. Reliable that work
    is happening, silent on how much.

    Pure.
    """
    if len(overreach) < 2 or len(pending) < 2:
        return False
    return bool((overreach[-1] & overreach[-2]) - pending[-2])


async def _stage_validate_publish(
    run_id: uuid.UUID, sinas: _Sinas, gate_cycles: int | None = None
) -> None:
    from app.services.faithfulness import validate_answer_evidence

    await _tele(run_id, "validate", started=_iso())
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        answer_id = run.answer_id
        caller = _runner_caller(run)
        if gate_cycles is None:
            gate_cycles = EFFORT_GATE_CYCLES.get(
                run.effort or "medium", ANSWER_GATE_CYCLES)

    await _mark(run_id, status="validating")
    failed_history: list[int] = []
    # Kept apart from `failed_history` on purpose. That list counts failed
    # evidence rows and `answer_regress` reads the same counts out of
    # `round_N` by prefix; folding overreach into it would change what every
    # stored run means after the fact.
    overreach_history: list[set[int]] = []
    # Which claims hold an unvalidated row, per round: failed spans and spans
    # the judge could not read. Either way the row stays pending, so the claim
    # is re-judged every round whether or not anybody touched it, and its
    # coverage verdict repeating says nothing about revision.
    pending_seq_history: list[set[int]] = []
    round_no = 0
    while True:
        round_no += 1
        spent = await _run_cost_usd(run_id)
        if spent > RUN_COST_CAP_USD:
            raise PartialOutcome(
                "budget_ceiling",
                f"run spend reached ${spent:.2f} (cap ${RUN_COST_CAP_USD:.0f}) "
                f"in validation round {round_no}")
        extended_because = None
        if round_no > MAX_VALIDATE_ROUNDS:
            converging = (len(failed_history) >= 2
                          and failed_history[-1] < failed_history[-2])
            # A second reason to grant a round, disjoint from the first rather
            # than folded into it: a claim marked overreaching twice running is
            # a claim the reviser is rewriting, and the failed-row count cannot
            # see that because a coverage verdict never fails a row.
            narrowing = _still_narrowing(overreach_history, pending_seq_history)
            if round_no > HARD_VALIDATE_ROUNDS or not (converging or narrowing):
                break
            extended_because = "converging" if converging else "narrowing"
            await _tele(run_id, "validate", extended_to_round=round_no)
        async with AsyncSessionLocal() as session:
            verdict = await validate_answer_evidence(session, caller, answer_id,
                                              pending_only=True, run_id=run_id)
        failed_history.append(len(verdict["failed"]))
        overreach_history.append(_overreach_seqs(verdict))
        pending_seq_history.append(_pending_seqs(verdict))
        await _tele(run_id, "validate", **{f"round_{round_no}": {
            "judged": verdict["judged"], "passed": verdict["passed"],
            "failed": len(verdict["failed"]), "errors": len(verdict["errors"]),
            "overreaching": len(verdict.get("overreaching") or []),
            # Why this round exists at all, when it is past the base budget.
            # Carried in a local from the decision to the one write at the end
            # of the round, rather than written where it happens: a flat key
            # would keep only the last extension of the run.
            **({"extended_because": extended_because} if extended_because else {}),
            # The count stays where it is: answer_regress reads it by prefix.
            # This is the same finding with its subject attached, so a run can
            # be asked which claim was objected to and on what ground.
            "overreaching_claims": _overreach_detail(
                verdict.get("overreaching") or []),
            # The sequences behind the `failed` count above. Without them the
            # exclusion this criterion turns on cannot be checked after the
            # fact: establishing that one run's seq 12 was the round-4 failure meant
            # matching the rounds-exhausted removal record against the claim
            # text, which is an inference, where `failed` at zero in round 3 is
            # a measurement. Sorted so the key is stable to compare across runs.
            "failed_claims": sorted(_failed_seqs(verdict)),
            # Beside them because they mean the same thing to the criterion:
            # an unjudged row is as pending as a failed one, and the exclusion
            # is only checkable after the fact if both are recorded.
            "errored_claims": sorted(_errored_seqs(verdict)),
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
                ok, missing, issues, correctness, points, gate_cause = await _gate_answer(
                    sinas, question, answer_id, run_id)
                # Amended here, where the gate's output is final, rather than
                # on the branches below. `issues` is complete the moment
                # `_gate_answer` returns and is only read afterwards, and two
                # paths out of this block return before any later write:
                # publishing, and the sweep asking for a repair cycle, which
                # recurses into a fresh validate. Amending on the branches left
                # those cycles recorded without the issues that produced them,
                # which is this same gap in a smaller place.
                await _amend_gate_cycle(run_id, redraft=missing, issues=issues)
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
                        await _amend_gate_cycle(run_id, bonus_cycle=True)
                        await _record_fed(run_id, key, bonus=True)
                        await _revise_answer(
                            sinas, run_id, answer_id,
                            correctness + issues,
                            points or ([missing] if missing else []),
                            last_attempt=True)
                        return await _stage_validate_publish(run_id, sinas, 0)
                    if not ok:
                        # `accounting` was a cause here and is gone. A named
                        # source the answer did not incorporate cannot make a
                        # run partial any more: `partial` says a part could not
                        # be answered, and that was false on every run this
                        # branch fired for. Unincorporated material is a note
                        # on the answer and a line in the objection ledger; an
                        # essential request that the review pressed and could
                        # not settle is a reservation the reader sees.
                        if gate_cause == "holistic":
                            raise PartialOutcome(
                                "holistic",
                                "the review rejected the answer as a whole without "
                                "naming a part it fails to address"
                                + (f" — {missing}" if missing else ""))
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
                    sinas, run_id, answer_id,
                    correctness + issues,
                    points or ([missing] if missing else []),
                    last_attempt=gate_cycles <= 1 or repeated)
                return await _stage_validate_publish(
                    run_id, sinas, gate_cycles - 1)
        # One revision per round, over everything this round found. A claim
        # this round names for the second time is struck: rewording stops
        # being a move it may make, here in the sentence it reads and in the
        # patch screen that applies the reply — see `services/strikes`.
        #
        # Only the findings this round will actually SEND are counted. The
        # ones past the cap are not put to the drafter, and a claim cannot be
        # said to have failed to act on a request it never saw.
        subjects = _finding_subjects(verdict)[:MAX_FEEDBACK_ITEMS]
        tally = await strikes.record_findings(run_id, subjects,
                                              round_no=round_no)
        struck_seqs = {seq for cid, seq in subjects if seq is not None
                       and tally.get(cid, 0) >= strikes.STRIKE_LIMIT}
        if struck_seqs:
            # `struck_N`, not `round_N_struck`. `answer_regress` reads every
            # key under `validate` that starts with `round_` as a round's
            # counts and takes the last one by name, so a sibling key sharing
            # that prefix would be read as the final round of the run.
            await _tele(run_id, "validate", **{
                f"struck_{round_no}": sorted(struck_seqs)})
        fb = _round_feedback(verdict, struck_seqs)
        if fb and await _revise_answer(sinas, run_id, answer_id, fb):
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
        dropped = await _removal_record(session, list(failing_ids))
        by_seq_dropped = {d["sequence"]: d for d in dropped}
        for cid in failing_ids:
            claim = await session.get(AnswerClaim, cid)
            if claim is not None:
                reasons = (await session.execute(
                    select(ClaimEvidence.validation_reasoning)
                    .where(ClaimEvidence.claim_id == cid)
                    .where(ClaimEvidence.validated.is_(False))
                )).scalars().all()
                entry = by_seq_dropped.get(claim.sequence)
                if entry is not None:
                    entry["why"] = [r for r in reasons if r][:3]
            await session.execute(
                ClaimEvidence.__table__.delete().where(ClaimEvidence.claim_id == cid)
            )
            await session.execute(
                AnswerClaim.__table__.delete().where(AnswerClaim.id == cid)
            )
        await session.commit()
    if dropped:
        # The flat key stays: `answer_regress` reads `dropped_claims` beside it
        # and both describe the latest state. The numbered record is the history.
        await _tele(run_id, "validate", dropped_detail=dropped)
        await _record_removal(run_id, "rounds_exhausted", dropped)
    # A claim the rounds ran out on did not end by a disposition, and the
    # strike record must not read as though the drafter chose anything. It is
    # the outcome the rule exists to make rarer, so it is named.
    for cid in failing_ids:
        await strikes.settle(run_id, str(cid), strikes.EXHAUSTED,
                             round_no=round_no)
    async with AsyncSessionLocal() as session:
        question = (await session.get(QueryRun, run_id)).question
    ok, missing, issues, correctness, points, gate_cause = await _gate_answer(
        sinas, question, answer_id, run_id)
    # Same reason as the other call site: final here, and some paths below
    # return before any later write.
    await _amend_gate_cycle(run_id, redraft=missing, issues=issues)
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
            await _amend_gate_cycle(run_id, bonus_cycle=True)
            await _record_fed(run_id, key, bonus=True)
            await _revise_answer(sinas, run_id, answer_id,
                                 correctness + issues,
                                 points or ([missing] if missing else []),
                                 last_attempt=True, removed=dropped)
            return await _stage_validate_publish(run_id, sinas, 0)
        if not ok and gate_cause == "holistic":
            raise PartialOutcome(
                "holistic",
                "validation exhausted and the review still rejected the answer as "
                "a whole without naming a part it fails to address"
                + (f" — {missing}" if missing else ""))
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
    await _amend_gate_cycle(run_id, dropped_claims=len(failing_ids))
    await _record_fed(run_id, key)
    await _revise_answer(sinas, run_id, answer_id,
                         correctness + issues,
                         points or ([missing] if missing else []),
                         last_attempt=gate_cycles <= 1, removed=dropped)
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
        # The stored result, and the answer built over it, belong to whoever
        # asked — a result owned by anyone else is invisible to them.
        owner_id, roles = run.owner_id, list(run.roles or [])
        if run.parent_result_id:  # resume: retrieval already stored
            return
    await _mark(run_id, status="retrieving")
    await _tele(run_id, "retrieval", started=_iso())
    # Between each step, not just at the stage boundary: these are the four
    # billable calls of the stage, and a checkpoint is only worth having
    # where it can still stop the next one from being made.
    plan = await rf.plan_question(question, effort=effort, run_id=run_id)
    if plan.get("warnings"):
        # On the run row, where the API and the UI read it; the plan itself
        # is stored with the result, but nobody opens a result to learn the
        # planner was working blind.
        await _tele(run_id, "retrieval", warnings=list(plan["warnings"]))
    await _check_cancel(run_id)
    ranked = await rf.retrieve_and_rank(plan)
    await _check_cancel(run_id)
    briefing = await rf.build_briefing(ranked, effort)
    await _check_cancel(run_id)
    rid = await rf.store_result(question, ranked, briefing, plan,
                                owner_id=owner_id, roles=roles)
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
        # Not always `published`. A run whose review pressed an essential
        # source the drafter refused ends `published_contested`: the answer is
        # there, complete and readable, with a reservation naming what two
        # readers disagreed about. See `_final_status`.
        status = await _final_status(run_id)
        await _mark(run_id, status=status, completed_at=_now())
        _log.info("query run %s %s", run_id, status)
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
