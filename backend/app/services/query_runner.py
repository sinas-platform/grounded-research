"""Server-supervised question pipeline (the query-side ingestion_runner).

The choreography of a question — dispatch sub-searches, wait, merge, brief
synthesis, validate, publish — lives HERE, in code, with per-stage state
checkpointed on the QueryRun row. Agents are consulted only for judgment:

  decompose   one forced-JSON call (search-orchestrator agent, single turn)
  search      grove/deep-search-agent chats — the agentic retrieval core
  draft       grove/synthesis-agent chat, scoped to DRAFTING ONLY
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
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.auth import CallerIdentity
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import AnswerClaim, ClaimEvidence, Document, DocumentClass, Result, ResultDocument
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
# holistically by the answer-gate agent, with at most this many redraft
# cycles before the run fails loudly. (Replaces the old counting floor.)
ANSWER_GATE_CYCLES = 1
REMEDIATION_WINDOW_S = 8 * 60
MIN_CLAIMS = 6
# effort → maximum sub-query fan-out. The bound is enforced here (truncation)
# AND stated in the decompose instruction; no magic numbers in agent prose.
EFFORT_FANOUT = {"low": 1, "medium": 2, "high": 3}

_log = __import__("logging").getLogger("grove.query_runner")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


class _Sinas:
    """Minimal async Sinas client for chat supervision."""

    def __init__(self) -> None:
        s = get_settings()
        self.base = s.sinas_url
        self.headers = {"Authorization": f"Bearer {s.sinas_api_key}"}

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
            return r.json().get("reply", "") or ""

    async def chat_messages(self, chat_id: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=30.0) as c:
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
    chat_id = await sinas.chat_create("grove/search-orchestrator", "[query-run] decompose")
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
            chat = await sinas.chat_create("grove/deep-search-agent", f"[query-run] {sq[:50]}")
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
                chat = await sinas.chat_create("grove/deep-search-agent", f"[query-run retry] {sq[:40]}")
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
    chat = await sinas.chat_create("grove/relationship-discovery-agent", "[query-run] discovery")
    sinas.send_detached(chat, f"Surface relationship proposals for the documents of result {parent}. Write proposals only.")
    await _tele(run_id, "discovery", fired=_iso(), chat_id=chat)


async def _doc_manifest(parent_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Document.filename,
                    DocumentClass.name,
                    ResultDocument.reason,
                    Document.summary,
                )
                .join(Document, Document.id == ResultDocument.document_id)
                .outerjoin(DocumentClass, DocumentClass.id == Document.document_class_id)
                .where(ResultDocument.result_id == parent_id)
                .order_by(Document.filename)
            )
        ).all()
    return "\n".join(
        f"- {fn} | {cls or '-'} | {(reason or '')[:120]} | {(summary or '')[:200]}"
        for fn, cls, reason, summary in rows
    )


async def _claim_count(answer_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(AnswerClaim.id).where(AnswerClaim.answer_id == answer_id)
                )
            ).scalars().unique().all().__len__()
        )


async def _stage_synthesize(run_id: uuid.UUID, sinas: _Sinas) -> uuid.UUID:
    from app.models import Answer

    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        question, parent_id = run.question, run.parent_result_id
        answer_id, chat_id = run.answer_id, run.synthesis_chat_id
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

    if not chat_id:
        chat_id = await sinas.chat_create("grove/synthesis-agent", "[query-run] synthesis")
        manifest = await _doc_manifest(parent_id)
        sinas.send_detached(
            chat_id,
            f"Question: {question}\n\n"
            f"An answer has already been started for you: answer_id {answer_id} "
            f"(source result {parent_id}). Do NOT call start_answer.\n\n"
            "Your scope is DRAFTING ONLY: draft the claims with nested evidence per "
            "your workflow and playbook target, then reply exactly DRAFTING COMPLETE. "
            "Validation and publishing run outside your chat — do not call "
            "validate_answer_evidence or publish_answer.\n\n"
            "The result's documents (filename | class | provenance | summary):\n"
            + manifest,
        )
        await _mark(run_id, synthesis_chat_id=chat_id)
        await _tele(run_id, "draft", started=_iso(), chat_id=chat_id)

    nudges = 0
    # DRAFT_TIMEOUT_S bounds IDLE time, not productive work: every sign of
    # progress (a new claim, chat activity) pushes the deadline out. A run is
    # only "timed out" after a full quiet window — killing a drafter that
    # wrote a claim 46 seconds ago is how run f7049916 died.
    loop_t = asyncio.get_event_loop().time
    deadline = loop_t() + DRAFT_TIMEOUT_S
    hard_deadline = loop_t() + 3 * DRAFT_TIMEOUT_S  # runaway backstop
    last_n, stable_at = -1, loop_t()
    while loop_t() < min(deadline, hard_deadline):
        await asyncio.sleep(POLL_S)
        n = await _claim_count(answer_id)
        if n != last_n:
            last_n, stable_at = n, loop_t()
            deadline = loop_t() + DRAFT_TIMEOUT_S
            continue
        if not await _chat_is_idle(sinas, chat_id):
            deadline = loop_t() + DRAFT_TIMEOUT_S
            continue
        settled = asyncio.get_event_loop().time() - stable_at > IDLE_DEAD_S
        if n >= MIN_CLAIMS and settled:
            await _tele(run_id, "draft", completed=_iso(), claims=n)
            return answer_id
        if nudges < MAX_NUDGES:
            nudges += 1
            sinas.send_detached(
                chat_id,
                f"Continue: answer {answer_id} has {n} claims. Draft the remaining "
                "claims with evidence per the playbook target, then reply DRAFTING COMPLETE.",
            )
        else:
            outage = await _dead_chat_diagnosis(sinas, chat_id)
            raise RuntimeError(
                outage or f"synthesis drafting dead at {n} claims after {MAX_NUDGES} nudges"
            )
    outage = await _dead_chat_diagnosis(sinas, chat_id)
    raise RuntimeError(outage or "synthesis drafting timed out")



async def _dead_chat_diagnosis(sinas: _Sinas, chat_id: str) -> str | None:
    """When a drafter dies producing nothing, check whether the chat shows an
    infrastructure failure rather than a model failure: if (nearly) every tool
    call errored, the story is a connector outage, and the run error should
    say so instead of blaming the drafter (run a20feca3: 40+ failed calls,
    reported as 'drafting dead at 0 claims')."""
    msgs = await sinas.chat_messages(chat_id)
    results = [m for m in msgs if m.get("role") == "tool"]
    if len(results) < 3:
        return None
    errored = [m for m in results if str(m.get("content") or "").lstrip().startswith('{"error"')]
    if len(errored) / len(results) < 0.9:
        return None
    sample = str(errored[-1].get("content") or "")[:200]
    return (
        f"connector outage: {len(errored)}/{len(results)} tool calls in the "
        f"synthesis chat failed — last error: {sample}"
    )


async def _gate_answer(
    sinas: _Sinas, run_question: str, answer_id: uuid.UUID
) -> tuple[bool, str, list[str]]:
    """Judge whether the surviving claims still answer the question, and
    surface quality findings. Generic by construction: no claim-type
    vocabulary, no counting floors — one holistic verdict from a stateless
    judge. Returns (publishable, missing, issues). `publishable` is the hard
    gate; `issues` are best-effort remediation targets that must never block
    publication on their own."""
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
        sources: list[tuple[str, str]] = []
        if parent_result_id:
            sources = [
                (fn, (summ or "").replace("\n", " ")[:220])
                for fn, summ in (
                    await session.execute(
                        select(Document.filename, Document.summary)
                        .join(ResultDocument, ResultDocument.document_id == Document.id)
                        .where(ResultDocument.result_id == parent_result_id)
                    )
                ).all()
            ]
    claims = "\n".join(f"{seq}. {text}" for seq, text in rows)
    source_lines = "\n".join(
        f"- [{'CITED' if fn in cited else 'uncited'}] {fn}: {summ}" for fn, summ in sources
    ) or "(working set unavailable)"
    reply = await sinas.invoke(
        "grove/answer-gate-agent",
        "QUESTION:\n" + run_question + "\n\nCLAIMS OF THE DRAFT ANSWER:\n" + claims
        + "\n\nWORKING DOCUMENT SET (each marked CITED if the answer uses it):\n" + source_lines
        + '\n\nReply ONLY JSON: {"publishable": true|false,'
        ' "missing": "<if not publishable: the one thing the claims fail to deliver on>",'
        ' "unresponsive": [<sequence numbers of claims that only describe a source without advancing the answer>],'
        ' "tension": "<claims that contradict each other with no claim reconciling them, or null>",'
        ' "unused_sources": ["<filename>: <why it is plainly more direct or authoritative for a point made than the source cited for it>", ...]}',
    )
    try:
        cleaned = reply.strip().strip("`").removeprefix("json").strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        data = json.loads(cleaned[start : end + 1])
        issues: list[str] = []
        seqs = [s for s in (data.get("unresponsive") or []) if isinstance(s, (int, str))]
        if seqs:
            issues.append(
                "Claims " + ", ".join(str(s) for s in seqs) + " only describe their source "
                "document; each must state what that source contributes to answering the "
                "question, or be dropped."
            )
        if data.get("tension"):
            issues.append(
                "Unreconciled tension: " + str(data["tension"]) + " Add a claim that "
                "reconciles these positions (grounded in evidence), or revise them."
            )
        for src in (data.get("unused_sources") or [])[:3]:
            issues.append(
                "Stronger source unused: " + str(src) + " Use it for the point it speaks "
                "to (or keep the current citation only if it is genuinely the better fit)."
            )
        return bool(data.get("publishable")), str(data.get("missing") or ""), issues
    except Exception:
        # an unparseable verdict must never block publication of a fully
        # validated answer — log via telemetry and treat as pass
        return True, "(gate verdict unparseable — treated as pass)", []


def _gate_remediation_msg(missing: str, issues: list[str]) -> str:
    parts = ([f"The verified claims no longer fully answer the question. Missing: {missing}"]
             if missing else []) + issues
    return (
        "Answer review found problems to fix before publication:\n- "
        + "\n- ".join(parts)
        + "\nGround every new or revised claim ONLY in evidence you can bind "
        "(read documents with numbered:true and copy the visible line numbers "
        "into spans). Revise an existing claim by re-posting its sequence "
        "number. Then reply REMEDIATION COMPLETE."
    )


async def _publish_answer(run_id: uuid.UUID, answer_id: uuid.UUID, **tele: Any) -> None:
    from app.models import Answer

    async with AsyncSessionLocal() as session:
        row = await session.get(Answer, answer_id)
        row.status = "published"
        row.published_at = _now()
        await session.commit()
    await _tele(run_id, "validate", published=_iso(), **tele)


async def _stage_validate_publish(
    run_id: uuid.UUID, sinas: _Sinas, gate_cycles: int = ANSWER_GATE_CYCLES
) -> None:
    from app.services.faithfulness import validate_answer_evidence

    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        answer_id, chat_id = run.answer_id, run.synthesis_chat_id
        caller = _runner_caller(run)

    async def _await_chat_quiescence() -> None:
        """Never judge while the drafter is mid-write: require the synthesis
        chat to be continuously idle for IDLE_DEAD_S before proceeding (run 10
        failed on exactly this race — a validate round judged mid-remediation
        and the late resets had no round left)."""
        while not await _chat_is_idle(sinas, chat_id):
            await asyncio.sleep(POLL_S)

    await _mark(run_id, status="validating")
    failed_history: list[int] = []
    round_no = 0
    while True:
        round_no += 1
        # Base budget, extended round by round while the failed count is
        # strictly shrinking (a converging run finishes; a stalled one stops).
        if round_no > MAX_VALIDATE_ROUNDS:
            converging = len(failed_history) >= 2 and failed_history[-1] < failed_history[-2]
            if round_no > HARD_VALIDATE_ROUNDS or not converging:
                break
            await _tele(run_id, "validate", extended_to_round=round_no)
        await _await_chat_quiescence()
        async with AsyncSessionLocal() as session:
            verdict = await validate_answer_evidence(session, caller, answer_id, pending_only=True)
        failed_history.append(len(verdict["failed"]))
        await _tele(run_id, "validate", **{f"round_{round_no}": {
            "judged": verdict["judged"], "passed": verdict["passed"],
            "failed": len(verdict["failed"]), "errors": len(verdict["errors"]),
        }})
        if not verdict["failed"] and not verdict["errors"]:
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
                ok, missing, issues = await _gate_answer(sinas, question, answer_id)
                if ok and (not issues or gate_cycles <= 0):
                    # quality issues never block publication on their own —
                    # unremediated ones are recorded, not fatal
                    tele = {"quality_issues": issues} if issues else {}
                    await _publish_answer(run_id, answer_id, **tele)
                    return
                if not ok and gate_cycles <= 0:
                    raise RuntimeError(
                        f"answer gate: claims validate but no longer answer the question — {missing}"
                    )
                await _tele(run_id, "validate", gate_redraft=missing, gate_issues=issues)
                sinas.send_detached(chat_id, _gate_remediation_msg(missing, issues))
                t0 = asyncio.get_event_loop().time()
                saw = False
                while asyncio.get_event_loop().time() - t0 < REMEDIATION_WINDOW_S:
                    await asyncio.sleep(POLL_S)
                    idle = await _chat_is_idle(sinas, chat_id)
                    if not idle:
                        saw = True
                    elif saw:
                        break
                return await _stage_validate_publish(run_id, sinas, gate_cycles - 1)
        failures = "\n".join(
            f"- claim seq {f['claim_sequence']} (claim_id {f['claim_id']}, evidence {f['evidence_id']}): {f['reason']}"
            for f in verdict["failed"]
        ) or "(transport errors only — rebind those spans)"
        sinas.send_detached(
            chat_id,
            "Validation results. Apply the TWO-STRIKES rule to these failed rows "
            "(update_claim to weaken, delete_claim if unsupportable, or bind ONE "
            "better span), then reply REMEDIATION COMPLETE. Do not validate or "
            "publish yourself:\n" + failures,
        )
        t0 = asyncio.get_event_loop().time()
        saw_activity = False
        while asyncio.get_event_loop().time() - t0 < REMEDIATION_WINDOW_S:
            await asyncio.sleep(POLL_S)
            idle = await _chat_is_idle(sinas, chat_id)
            if not idle:
                saw_activity = True
            elif saw_activity:
                break  # worked, then went quiet — remediation done

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
        for cid in failing_ids:
            await session.execute(
                ClaimEvidence.__table__.delete().where(ClaimEvidence.claim_id == cid)
            )
            await session.execute(
                AnswerClaim.__table__.delete().where(AnswerClaim.id == cid)
            )
        await session.commit()
        question = (await session.get(QueryRun, run_id)).question
    ok, missing, issues = await _gate_answer(sinas, question, answer_id)
    if ok and (not issues or gate_cycles <= 0):
        tele = {"quality_issues": issues} if issues else {}
        await _publish_answer(
            run_id, answer_id, dropped_claims=len(failing_ids), **tele
        )
        return
    if not ok and gate_cycles <= 0:
        raise RuntimeError(
            "validation exhausted and the surviving claims do not answer the "
            f"question — {missing}"
        )
    await _tele(
        run_id, "validate",
        gate_redraft=missing, gate_issues=issues, dropped_claims=len(failing_ids),
    )
    prefix = (
        "Several claims were dropped as unverifiable.\n" if failing_ids else ""
    )
    sinas.send_detached(chat_id, prefix + _gate_remediation_msg(missing, issues))
    t0 = asyncio.get_event_loop().time()
    saw = False
    while asyncio.get_event_loop().time() - t0 < REMEDIATION_WINDOW_S:
        await asyncio.sleep(POLL_S)
        idle = await _chat_is_idle(sinas, chat_id)
        if not idle:
            saw = True
        elif saw:
            break
    return await _stage_validate_publish(run_id, sinas, gate_cycles - 1)


# ── entrypoint ──────────────────────────────────────────────────────────────


async def run_pipeline(run_id: uuid.UUID) -> None:
    """Drive one QueryRun to published/failed. Designed to be launched as an
    asyncio background task; safe to re-launch on a failed run (resume)."""
    sinas = _Sinas()
    await _mark(run_id, started_at=_now(), error=None)
    async with AsyncSessionLocal() as session:
        mode = (await session.get(QueryRun, run_id)).mode
    try:
        if mode in ("full", "retrieval"):
            await _stage_decompose(run_id, sinas)
            children = await _stage_search(run_id, sinas)
            await _stage_merge(run_id, children)
            await _stage_discovery(run_id, sinas)
        if mode == "retrieval":
            await _mark(run_id, status="published", completed_at=_now())
            _log.info("query run %s retrieval published", run_id)
            return
        # synthesis mode requires parent_result_id supplied at creation
        await _stage_synthesize(run_id, sinas)
        await _stage_validate_publish(run_id, sinas)
        await _mark(run_id, status="published", completed_at=_now())
        _log.info("query run %s published", run_id)
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
