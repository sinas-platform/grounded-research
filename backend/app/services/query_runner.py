"""Server-supervised question pipeline (the query-side ingestion_runner).

The choreography of a question — dispatch sub-searches, wait, merge, brief
synthesis, validate, publish — lives HERE, in code, with per-stage state
checkpointed on the QueryRun row. Agents are consulted only for judgment:

  decompose   one forced-JSON call (search-orchestrator agent, single turn)
  search      sgr/deep-search-agent chats — the agentic retrieval core
  draft       sgr/synthesis-agent chat, scoped to DRAFTING ONLY
  verdicts    the stateless evidence-check fan-out (services/faithfulness)

Supervision rules: stage completion is observed in the DATABASE, never on a
held HTTP connection; a silent chat (no new messages, no artifact progress)
is nudged at most MAX_NUDGES times, then a search is re-dispatched once and
anything else fails the run explicitly. Every transition lands in
QueryRun.telemetry. A failed run can be resumed: completed stages short-
circuit off the persisted state.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, select

from app.auth import CallerIdentity
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import AnswerClaim, ClaimEvidence, Document, DocumentClass, DocumentVersion, Result, ResultDocument
from app.models.query import QueryRun

POLL_S = 12
SEARCH_TIMEOUT_S = 25 * 60
# Decompose runs on the same chat pattern as the other stages; the window is
# generous because an over-eager orchestrator may work before replying, and
# the run degrades to the undecomposed question rather than failing.
DECOMPOSE_TIMEOUT_S = 10 * 60
DRAFT_TIMEOUT_S = 20 * 60
IDLE_DEAD_S = 150
MAX_NUDGES = 2
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
# effort → maximum sub-query fan-out. The bound is enforced here (truncation)
# AND stated in the decompose instruction; no magic numbers in agent prose.
EFFORT_FANOUT = {"low": 1, "medium": 2, "high": 3}

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
        async with httpx.AsyncClient(timeout=600.0) as c:
            r = await c.post(
                f"{self.base}/agents/{agent}/invoke",
                headers=self.headers,
                json={"message": message},
            )
            r.raise_for_status()
            data = r.json()
        await record_llm_call(self.run_id, data.get("chat_id"), agent)
        return data.get("reply", "") or ""

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

    async def chat_last_activity(self, chat_id: str) -> datetime | None:
        msgs = await self.chat_messages(chat_id)
        if not msgs:
            return None
        ts = msgs[-1].get("created_at")
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
                return None


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


async def _chat_is_idle(sinas: _Sinas, chat_id: str) -> bool:
    last = await sinas.chat_last_activity(chat_id)
    if last is None:
        return True
    return (_now() - last).total_seconds() > IDLE_DEAD_S


def _chat_ids_for_cleanup(telemetry: dict | None, searches: dict | None) -> list[str]:
    """Every sinas chat a run has opened, read from the state the stages
    already record: telemetry entries carrying a chat_id (decompose, draft,
    discovery) and the per-sub-query search chats. Order-stable, deduped."""
    ids: list[str] = []
    for entry in (telemetry or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("chat_id"), str):
            ids.append(entry["chat_id"])
    for meta in (searches or {}).values():
        if isinstance(meta, dict) and isinstance(meta.get("chat_id"), str):
            ids.append(meta["chat_id"])
    return list(dict.fromkeys(ids))


async def _teardown_chats(sinas: _Sinas, chat_ids: list[str]) -> None:
    """Delete each chat a failed run opened, so no agent keeps working for
    nobody. Best-effort throughout: one failed delete never blocks the rest."""
    for chat_id in chat_ids:
        try:
            await sinas.chat_delete(chat_id)
        except Exception:
            _log.warning("teardown of chat %s failed", chat_id)


async def _await_reply(
    sinas: _Sinas, chat_id: str, window_s: float, poll_s: float = POLL_S
) -> str | None:
    """Poll a chat until the agent posts a non-empty assistant message;
    return its content, or None when the window closes first."""
    deadline = asyncio.get_event_loop().time() + window_s
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_s)
        for m in reversed(await sinas.chat_messages(chat_id)):
            if m.get("role") == "assistant" and (m.get("content") or "").strip():
                return m["content"]
    return None


def _parse_subqueries(reply: str, question: str, max_fanout: int) -> tuple[list[str], bool]:
    """The decompose reply must be a JSON array of strings; anything else
    falls back to the question itself — decomposition is an optimization,
    never a failure mode. Returns (subqueries, parsed_ok)."""
    try:
        cleaned = reply.strip().strip("`")
        cleaned = cleaned.removeprefix("json").strip()
        subs = json.loads(cleaned)
        assert isinstance(subs, list) and subs and all(isinstance(x, str) for x in subs)
    except Exception:
        return [question], False
    return subs[:max_fanout], True


# ── stages ──────────────────────────────────────────────────────────────────


async def _stage_decompose(run_id: uuid.UUID, sinas: _Sinas) -> list[str]:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if run.subqueries:
            return list(run.subqueries)
        question = run.question
        max_fanout = EFFORT_FANOUT.get(run.effort, 2)
    await _mark(run_id, status="decomposing")
    await _tele(run_id, "decompose", started=_iso(), max_fanout=max_fanout)
    # House pattern (chat + observed completion) instead of a one-shot invoke:
    # a long-lived HTTP read is a timeout waiting to happen when the agent
    # decides to work before replying, and the server keeps executing after
    # the client gives up. The chat id lands in telemetry so a failed run's
    # teardown can find it.
    chat_id = await sinas.chat_create("sgr/search-orchestrator", "[query-run] decompose")
    await _tele(run_id, "decompose", chat_id=chat_id)
    sinas.send_detached(
        chat_id,
        "Decompose the following question into independent retrieval sub-queries. "
        f"Use AT MOST {max_fanout} sub-quer{'y' if max_fanout == 1 else 'ies'}; "
        "fewer is better when the question does not demand parallel angles. "
        "Reply with ONLY a JSON array of strings. Do not run searches or call "
        "any tools first; reply directly.\n\n"
        f"Question: {question}",
    )
    reply = await _await_reply(sinas, chat_id, DECOMPOSE_TIMEOUT_S)
    if reply is None:
        subs, ok = [question], False
    else:
        subs, ok = _parse_subqueries(reply, question, max_fanout)
    if not ok:
        # Window closed or off-script reply: proceed with the question itself
        # and stop the chat so no agent keeps working for a stage that moved on.
        await sinas.chat_delete(chat_id)
    subs = subs[:max_fanout]
    await _mark(run_id, subqueries=subs)
    await _tele(run_id, "decompose", completed=_iso(), subqueries=subs)
    return subs


_UUID_RE = __import__("re").compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


async def _search_result_for(
    sinas: "_Sinas", chat_id: str
) -> tuple[str, str] | None:
    """Find the result the deep-search agent published IN THIS CHAT.

    The agent rephrases its result's query, so matching results by query text
    is unreliable (it silently never matches, and the runner nudges forever).
    Instead read the chat: create_draft_result / add_files_to_result /
    publish_result tool calls all carry the result id. Collect the candidate
    ids from the chat and return the one that is a real, published (or draft)
    result owned by this run's search — checked against the DB.
    """
    ids: list[str] = []
    for m in await sinas.chat_messages(chat_id):
        content = m.get("content")
        if not isinstance(content, str) or "result" not in content.lower():
            continue
        for uid in _UUID_RE.findall(content):
            if uid not in ids:
                ids.append(uid)
    if not ids:
        return None
    async with AsyncSessionLocal() as session:
        rows = {
            str(r[0]): r[1]
            for r in (
                await session.execute(
                    select(Result.id, Result.status).where(
                        Result.id.in_([uuid.UUID(i) for i in ids])
                    )
                )
            ).all()
        }
    # prefer a published result; else the latest draft the chat created
    for uid in ids:
        if rows.get(uid) == "published":
            return uid, "published"
    for uid in ids:
        if uid in rows:
            return uid, rows[uid]
    return None


async def _stage_search(run_id: uuid.UUID, sinas: _Sinas) -> list[str]:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        question, subs = run.question, list(run.subqueries)
        searches: dict[str, dict] = dict(run.searches or {})
        done = {s: m["result_id"] for s, m in searches.items() if m.get("result_id")}
        if len(done) == len(subs):
            return [done[s] for s in subs]
    await _mark(run_id, status="searching")
    await _tele(run_id, "search", started=_iso())

    dispatch_msg = (
        "Run your retrieval workflow for this sub-query and publish the result. "
        "Do not end your turn before publish_result succeeds.\n\n"
        "Sub-query: {sq}\n\nContext — the user's full question: {q}"
    )
    for sq in subs:
        if sq not in searches:
            chat = await sinas.chat_create("sgr/deep-search-agent", f"[query-run] {sq[:50]}")
            searches[sq] = {"chat_id": chat, "started": _iso(), "nudges": 0, "redispatched": False}
            sinas.send_detached(chat, dispatch_msg.format(sq=sq, q=question))
    await _mark(run_id, searches=searches)

    deadline = asyncio.get_event_loop().time() + SEARCH_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(POLL_S)
        changed = False
        for sq, meta in searches.items():
            if meta.get("result_id"):
                continue
            found = await _search_result_for(sinas, meta["chat_id"])
            if found and found[1] == "published":
                meta["result_id"] = found[0]
                changed = True
                continue
            if not await _chat_is_idle(sinas, meta["chat_id"]):
                continue
            if meta["nudges"] < MAX_NUDGES:
                meta["nudges"] += 1
                changed = True
                sinas.send_detached(
                    meta["chat_id"],
                    "Continue your retrieval workflow from where you stopped"
                    + (" — your draft result is unpublished; finish validation and publish it."
                       if found else " — create and publish the result.")
                    + " Do not end your turn before publish_result succeeds.",
                )
            elif not meta["redispatched"]:
                meta.update(redispatched=True, nudges=0, started=_iso())
                chat = await sinas.chat_create("sgr/deep-search-agent", f"[query-run retry] {sq[:40]}")
                meta["chat_id"] = chat
                sinas.send_detached(chat, dispatch_msg.format(sq=sq, q=question))
                changed = True
            else:
                raise RuntimeError(f"sub-search dead after nudges+retry: {sq[:60]!r}")
        if changed:
            await _mark(run_id, searches=searches)
        if all(m.get("result_id") for m in searches.values()):
            await _tele(run_id, "search", completed=_iso(),
                        results={s: m["result_id"] for s, m in searches.items()})
            return [searches[s]["result_id"] for s in subs]
    raise RuntimeError("search stage timed out")


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
        out.append({
            "document_id": did, "filename": fn, "class": cls or "",
            "annotations": ann, "reason": (reason or ""),
            "summary": (summary or ""),
            "briefing": briefing_by_doc.get(str(did)),
        })
    return out


def _manifest_line(r: dict) -> str:
    return (f"- {r['filename']} | {r['class'] or '-'} | "
            f"{r['annotations'] or '-'} | {r['reason'][:120]} | "
            f"{r['summary'][:200]}")


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
                          prompt_tokens * 0.05 + completion_tokens * 0.20
                        WHEN model ILIKE '%gemini%' THEN
                          prompt_tokens * 0.30 + completion_tokens * 2.50
                        WHEN model ILIKE '%haiku%' THEN
                          (prompt_tokens - cache_read_tokens - cache_write_tokens) * 1.0
                          + cache_write_tokens * 1.25 + cache_read_tokens * 0.10
                          + completion_tokens * 5.0
                        ELSE
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


async def _fetch_numbered(filenames: list[str], cap_chars: int = 140000) -> dict[str, str]:
    """Numbered content per filename (line-numbered so extraction quotes
    carry verifiable line refs), capped per document."""
    out: dict[str, str] = {}
    async with AsyncSessionLocal() as session:
        for fn in filenames:
            content = (
                await session.execute(
                    select(DocumentVersion.content_md)
                    .join(Document, Document.current_version_id == DocumentVersion.id)
                    .where(Document.filename == fn)
                )
            ).scalar()
            if not content:
                continue
            lines = content.splitlines()
            numbered = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines))
            out[fn] = numbered[:cap_chars]
    return out


def _verify_passage(numbered: str, line_from: int, line_to: int, quoted: str) -> bool:
    """Deterministic anti-hallucination: the quoted text must actually occur
    in the claimed line range (whitespace-normalized containment)."""
    import re as _re

    want = _re.sub(r"\s+", " ", quoted or "").strip().lower()
    if len(want) < 20:
        return False
    span_lines = []
    for line in numbered.splitlines():
        num, _, rest = line.partition(": ")
        try:
            n = int(num)
        except ValueError:
            continue
        if line_from <= n <= line_to + 2:
            span_lines.append(rest)
    have = _re.sub(r"\s+", " ", " ".join(span_lines)).strip().lower()
    return want[:200] in have


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


async def _extract_passages(
    sinas: _Sinas, plan_claims: list[dict], run_id: uuid.UUID | None = None
) -> list[dict]:
    """Inverted split, reading half: the cheap model pulls verbatim passages
    (with line refs) per planned claim from its anchor documents. Every quote
    is then verified against the actual lines — fabricated quotes are dropped,
    so the drafter can only ever see text that exists."""
    sem = asyncio.Semaphore(4)

    async def one(c: dict) -> dict:
        anchors = [str(a) for a in (c.get("anchors") or [])[:8]]
        docs = await _fetch_numbered(anchors)
        if not docs:
            return {"n": c.get("n"), "passages": [], "proposed": 0, "read": 0}
        doc_blob = "\n\n".join(
            f"=== {fn} ===\n{txt}" for fn, txt in docs.items())
        async with sem:
            try:
                reply = await sinas.invoke(
                    "sgr/passage-extractor-agent",
                    "From the numbered documents below, extract the passages "
                    "that bear on: " + str(c.get("establishes") or "") + "\n"
                    + (("Focus: " + str(c.get("hint")) + "\n") if c.get("hint") else "")
                    + 'Reply ONLY JSON: {"passages": [{"filename": "...", '
                    '"line_from": <int>, "line_to": <int>, "text": "<verbatim '
                    'quote>"}]} — max 4 passages, each 2-25 lines, text EXACTLY '
                    "as printed (without the line-number prefixes).\n\n" + doc_blob,
                )
                cleaned = reply.strip().strip("`").removeprefix("json").strip()
                data = json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])
            except Exception as exc:  # noqa: BLE001
                # A transport failure is not "this document says nothing".
                # Swallowing it produced runs that ended `partial` — which
                # here means the corpus cannot answer the question — when the
                # real cause was an HTTP 429. Record it so the caller can
                # tell an empty document from an unreachable one.
                return {"n": c.get("n"), "passages": [], "proposed": 0,
                        "read": len(docs), "error": str(exc)[:200]}
        proposed = (data.get("passages") or [])[:4]
        good = []
        for p in proposed:
            fn = str(p.get("filename") or "")
            try:
                lf, lt = int(p.get("line_from")), int(p.get("line_to"))
            except (TypeError, ValueError):
                continue
            if fn in docs and _verify_passage(docs[fn], lf, lt, str(p.get("text") or "")):
                good.append({"filename": fn, "line_from": lf, "line_to": lt,
                             "text": str(p.get("text"))[:2000]})
        return {"n": c.get("n"), "establishes": c.get("establishes"),
                "passages": good, "proposed": len(proposed),
                "read": len(docs)}

    out = list(await asyncio.gather(*(one(c) for c in plan_claims)))
    errors = [r.get("error") for r in out if r.get("error")]
    if errors and not any(r.get("passages") for r in out):
        # every extraction failed and none succeeded: infrastructure, not
        # a judgment about the sources
        raise RuntimeError(
            f"passage extraction failed for all {len(out)} claims — "
            f"first error: {errors[0]}")
    if run_id is not None:
        # The verified/proposed gap is the point of this stage: quotes that
        # did not match the source text never reach the drafter. A gap that
        # stops being small means the extractor is drifting.
        await _tele(
            run_id, "extract",
            claims=len(out),
            documents_read=sum(r.get("read", 0) for r in out),
            passages_proposed=sum(r.get("proposed", 0) for r in out),
            passages_verified=sum(len(r.get("passages") or []) for r in out),
            extraction_errors=len(errors),
        )
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
) -> str:
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
            return ""
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
        f"{r['summary'].replace(chr(10), ' ')[:200]}"
        for r in mrows
    ) or "(working set unavailable)"
    reply = await sinas.invoke(
        "sgr/answer-gate-agent",
        "QUESTION:\n" + run_question
        + "\n\nCLAIMS OF THE DRAFT ANSWER (the number before each claim is "
          "its identifier, not its position: revision drops claims, so gaps "
          "in the numbering are expected and are not a defect — there is no "
          "claim missing from this list):\n" + claims
        + "\n\nWORKING DOCUMENT SET (each marked CITED if the answer uses it):\n" + source_lines
        + '\n\nFirst split the QUESTION into the distinct things it asks — '
        'a question asking what the conditions are, whether a regulation '
        'applies, and whether a step is mandatory asks three things, not one. '
        'Judge each separately against the claims. A part is COVERED when '
        'the claims answer it either way: claims that rebut the premise of '
        'the question with grounds — the question asks about liability '
        'without fault, the claims establish fault is always required — '
        'ANSWER that part; do not demand a claim affirming a premise the '
        'sources reject. A claim that states plainly that the available '
        'sources do not address a part (an abstention) also COVERS that '
        'part: telling the reader what the sources cannot establish is the '
        'honest answer when the corpus lacks the authority, not a gap.'
        '\n\nReply ONLY JSON: {"publishable": true|false,'
        ' "parts": [{"asks": "<one thing the question asks>", "covered": '
        'true|false, "gap": "<what is missing, if not covered>"}],'
        ' "missing": "<if not publishable: what the claims fail to deliver on>",'
        ' "unresponsive": [<sequence numbers of claims that only describe a source without advancing the answer>],'
        ' "tension": "<ONLY a pair of claims that CANNOT BOTH BE TRUE — quote the '
'two incompatible propositions verbatim. Claims that restate the same rule, '
'overlap, emphasise different aspects, or address different procedural '
'stages are NOT in tension; when in doubt, null. Or null.>",'
        ' "dangling": [<sequence numbers of claims that lean on another claim that is not there: they open with or depend on phrases like "that logic", "applying this reasoning", "the same principle" whose antecedent claim is absent or says something else>],'
        ' "no_conclusion": <true if no claim draws the overall conclusion the question asks for>,'
        ' "unused_sources": ["<filename>: <why it is plainly more direct or authoritative for a point made than the source cited for it>", ...]}',
    )
    # Only the parse is guarded. A wide try around the whole body turns a
    # fault in this function into "the gate had no objection" — which is what
    # happened here for three hours — so everything after the parse runs
    # unguarded and fails the run loudly if it is broken.
    try:
        cleaned = reply.strip().strip("`").removeprefix("json").strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        data = json.loads(cleaned[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("gate verdict was not an object")
    except Exception as exc:  # noqa: BLE001
        # An unparseable verdict must never block publication of a fully
        # validated answer. But treating it as a pass silently is how a
        # broken gate looks exactly like a clean one, so it is recorded.
        await _tele(run_id, "validate", gate_unparseable=str(exc)[:200])
        return True, "(gate verdict unparseable — treated as pass)", [], [], []

    # Coverage is judged per part. One holistic verdict let an answer
    # addressing two of a question's three parts publish, and named one
    # gap at a time when it failed — so revision fixed them one cycle
    # each, or the run ran out of cycles first.
    parts = [x for x in (data.get("parts") or []) if isinstance(x, dict)]
    uncovered = [
        str(x.get("gap") or x.get("asks") or "").strip()
        for x in parts if not x.get("covered")
    ]
    uncovered = [u for u in uncovered if u]

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
    stronger = [str(src) for src in (data.get("unused_sources") or [])[:3] if src]
    for src in stronger:
        issues.append(
            "Stronger source unused: " + src + " Use it for the point it speaks "
            "to (or keep the current citation only if it is genuinely the better fit)."
        )
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

    if not (revise or add or drop or keep):
        return None
    return {"revise": revise, "add": add, "drop": drop, "keep": keep}


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
    def _anchors_for(point: str) -> list[str]:
        """Documents that could contain this point, best first, plus a couple
        of top-ranked ones for context. Same matcher planning uses."""
        picked = _relevant_docs(point, corpus_rows, take=6)
        return picked + [fn for fn in corpus[:4] if fn not in picked]

    fresh = ""
    if extra_points and corpus:
        plan = [{"n": i, "establishes": pt[:600], "anchors": _anchors_for(pt),
                 "hint": "extract the passage that states this; a case caption "
                         "or party list is not a holding"}
                for i, pt in enumerate(extra_points[:5], start=1)]
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
    if not patch:
        await _tele(run_id, "validate", revision_yielded_no_change=True)
        return 0

    by_seq = {c.sequence: c for c, *_ in rows}
    touched = 0
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
            for item in patch["add"][:max(0, MAX_CLAIMS - live)]:
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

    await _tele(run_id, "validate",
                revision={"claims": len(by_claim), "revised": len(patch["revise"]),
                          "kept_with_reason": len(patch.get("keep") or []),
                          "abstentions": sum(
                              1 for a in patch["add"]
                              if a.get("type") == "abstention"),
                          "dropped": len(patch["drop"]), "added": len(patch["add"]),
                          "untouched": len(by_claim) - touched,
                          "feedback_items": len(feedback)})
    return touched


async def _stage_validate_publish(
    run_id: uuid.UUID, sinas: _Sinas, gate_cycles: int | None = None
) -> None:
    from app.services.faithfulness import validate_answer_evidence

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
                    raise PartialOutcome(
                        "coverage",
                        (f"the validated claims no longer answer the question — {missing}")
                        if not ok else
                        ("the answer could not be made internally consistent — "
                         + " ".join(correctness)[:600]))
                await _tele(run_id, "validate", gate_redraft=missing, gate_issues=issues)
                await _record_fed(run_id, key)
                await _revise_answer(
                    sinas, run_id, answer_id, question,
                    correctness + issues,
                    points or ([missing] if missing else []),
                    last_attempt=gate_cycles <= 1)
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
        message = await sinas.invoke(
            "sgr/answer-gate-agent",
            "Reply with ONLY the note text itself — no preamble, no commentary "
            "about the task, no restatement of these instructions. "
            "Write a note (max 200 words) to "
            + get_settings().sgr_audience
            + ", in the SAME "
            "LANGUAGE as the question below. Structure: (1) state plainly which "
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
    plan = await rf.plan_question(question, effort=effort)
    ranked = await rf.retrieve_and_rank(plan)
    briefing = await rf.build_briefing(ranked, effort)
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
        if mode in ("full", "retrieval"):
            await _stage_retrieve_first(run_id)
        if mode == "retrieval":
            await _mark(run_id, status="published", completed_at=_now())
            _log.info("query run %s retrieval published", run_id)
            return
        # synthesis mode requires parent_result_id supplied at creation
        await _stage_synthesize(run_id, sinas)
        await _stage_validate_publish(run_id, sinas)
        await _mark(run_id, status="published", completed_at=_now())
        _log.info("query run %s published", run_id)
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
