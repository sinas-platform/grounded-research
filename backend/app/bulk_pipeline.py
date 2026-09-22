"""Bulk ingestion pipeline — batch ETL, not service behavior.

The weekend of 15-16 Aug proved that driving bulk corpus loads through the
API process's event loop breaks in compounding ways (pool exhaustion, loop
starvation, persist stampedes, serial reply fetching). This module is the
batch-shaped alternative, built from the pieces that never failed:

  round 1  build front-matter prompts, submit provider batches (<=400)
  round 2  fetch replies (bounded-concurrent), derive props/chunk prompts
           from round-1 branching, submit those
  persist  replay all stored replies through the NORMAL oneshot pipeline
           in bounded groups (the recover_from_chats pattern: groups of
           25, concurrency 8 — zero failures across 584 docs this weekend)
  resolve  ground + resolve in bounded groups (direct function calls)
  rels     build relationship prompts from resolved mentions, batch,
           persist via the standard extractor with stored replies

Every stage derives its worklist FROM THE DATA (extraction needed == no
summary; relationships needed == no coverage), so any stage is resumable
and re-runnable by construction. Batch ids are checkpointed to the job dir
before polling so a crashed job resumes by polling, never by resubmitting.

Run standalone (never inside uvicorn):
    cd backend && ../.venv/bin/python -m app.bulk_pipeline \
        --ids-file /tmp/ids.txt --stages extract,resolve,relationships \
        --job-dir ~/sgr-bulk-jobs/p5-probe

The API trigger (app/api/v1/bulk.py) spawns exactly this as a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import os
import re
import socket
import time
import uuid
from collections import defaultdict
from pathlib import Path

import httpx
from sqlalchemy import select

log = logging.getLogger("bulk")

SUBMIT_MAX = 400          # inputs per Sinas batch POST (~15MB body ceiling)
FETCH_CONCURRENCY = int(os.environ.get("BULK_FETCH_CONCURRENCY", "16"))    # parallel chat-reply fetches (serial fetch cost
                          # ~1h on a 2,854-reply wave, 16 Aug)
PERSIST_GROUP = 25        # docs per persist group
PERSIST_CONCURRENCY = int(os.environ.get("BULK_PERSIST_CONCURRENCY", "8"))   # concurrent doc pipelines inside a group
MIDDLE_GROUP = 50         # docs per ground/resolve group
MIDDLE_CONCURRENCY = int(os.environ.get("BULK_MIDDLE_CONCURRENCY", "8"))
POLL_SECONDS = 20

CHUNK_RE = re.compile(r"CHUNK (\d+)/(\d+) OF DOCUMENT ([^\n:]+):")
FILENAME_RE = re.compile(r"^FILENAME: (.+)$", re.M)


# ── dependency outages: a wait, not a failure ───────────────────────────────
# A sleeping laptop or a dropped network takes BOTH dependencies with it: the
# provider host stops resolving and the pooled database connections die.
# Three restarts in two days (14 Sep overnight, 16 Sep morning, 16 Sep
# evening) were one shape — an in-flight round spent its six submit attempts
# inside the outage, raised, and a human had to notice and restart it. The
# supervising wrapper waits for the database AND the provider before it
# starts or resumes anything; a round already running now does the same.
#
# An unreachable dependency is not a refusal, so it does not spend a retry
# budget: the round pauses until the dependency answers again, or until the
# cap below runs out. The cap defaults to 12h because the worst observed
# outage was an overnight sleep (14 Sep) and a morning commute picks up where
# it left off — long enough to sit through a night, short enough that a
# genuinely dead dependency still ends the process and hands the job back to
# the wrapper, whose own wait-then-resume path is the tested one.
OUTAGE_WAIT_SECONDS = float(os.environ.get("BULK_OUTAGE_WAIT_SECONDS",
                                           str(12 * 3600)))
OUTAGE_BACKOFF_START = 5.0     # first re-probe, seconds
OUTAGE_BACKOFF_MAX = 120.0     # steady-state re-probe: ~2 min costs nothing
                               # across a 12h wait and returns work promptly


class SubmitRejected(RuntimeError):
    """The platform refused the submission itself — a bad request, not an
    unreachable dependency and not a rate-limit window. No wait and no retry
    changes the answer, so it ends the round on the first try."""


# Client statuses a retry can still fix; every other 4xx is a refusal.
_RETRYABLE = frozenset({408, 425, 429})


class DependencyDown(RuntimeError):
    """A dependency is unreachable — the round waits, it has not failed.

    Raised where a submission, poll, fetch or database call could not reach
    the thing it needed: the host would not resolve, the connection was
    refused or reset, or a pooled database connection was lost. Also raised
    (chained from the last failure) when a wait outlasts its cap.
    """

    def __init__(self, dependency: str, detail: str) -> None:
        super().__init__(f"{dependency} unreachable: {detail}")
        self.dependency = dependency
        self.detail = detail


# Errno numbers that mean "the transport never got there". Read from the
# local errno table, which is authoritative for exceptions raised in THIS
# process.
_TRANSPORT_ERRNOS = frozenset(
    v for v in (getattr(errno, n, None) for n in (
        "ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "ENETDOWN",
        "ENETUNREACH", "ENETRESET", "EHOSTDOWN", "EHOSTUNREACH",
        "ETIMEDOUT", "EPIPE", "ENOTCONN", "ESHUTDOWN",
    )) if v is not None
)

# The same failures as TEXT, for the one case where the cause cannot arrive
# as an exception: the platform runs the provider call in its own process
# (Linux) and relays the failure to us as a string inside an HTTP error body.
# Errno numbers are not portable across those two hosts (ECONNREFUSED is 61
# here, 111 on glibc), so the text table carries both spellings — locally
# derived first, then the glibc/BSD wordings we cannot produce from here.
_TRANSPORT_TEXTS = frozenset(
    [os.strerror(e).lower() for e in _TRANSPORT_ERRNOS]
    + [
        "connection refused", "connection reset by peer",
        "network is unreachable", "no route to host", "network is down",
        "connection timed out", "broken pipe", "host is down",
        # getaddrinfo (EAI_*), glibc and BSD wordings
        "no address associated with hostname", "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided, or not known",
        # asyncpg/SQLAlchemy connection loss, relayed as text
        "connection was closed in the middle of operation",
        "server closed the connection unexpectedly",
    ]
)

_ERRNO_IN_TEXT = re.compile(r"\[errno (-?\d+)\]", re.I)

# asyncpg exception names that mean the connection is gone, matched by name
# so this module keeps its import-free load (every app import here is
# function-local by design).
_ASYNCPG_LOST = frozenset({
    "ConnectionDoesNotExistError", "ConnectionFailureError",
    "ConnectionRejectionError", "CannotConnectNowError",
    "ClientCannotConnectError", "PostgresConnectionError",
    "AdminShutdownError", "CrashShutdownError", "InterfaceError",
})

# One log line per outage, however many callers hit it at once: dependency ->
# monotonic start. Single event loop, so no lock.
_OPEN_OUTAGES: dict[str, float] = {}


def _dur(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _text_is_transport(text: str) -> bool:
    """Does this error TEXT name a transport failure rather than a refusal?

    Used only where the cause crossed a process boundary as a string. An
    errno in the text is read as a number first: getaddrinfo failures surface
    as socket.gaierror, whose codes are EAI_* — negative on glibc
    (`[Errno -2]`, `[Errno -5]`), so any negative errno in a socket error is
    a name-resolution failure whatever host produced it.
    """
    if not text:
        return False
    for raw in _ERRNO_IN_TEXT.findall(text):
        n = int(raw)
        if n < 0 or n in _TRANSPORT_ERRNOS:
            return True
    low = text.lower()
    return any(t in low for t in _TRANSPORT_TEXTS)


def _iter_causes(exc: BaseException):
    """Walk an exception and everything it was raised from.

    Includes SQLAlchemy's `.orig`: the driver error it wrapped is the one
    that says whether the connection is gone, and it is reachable there even
    when the wrapper was built rather than raised from it.
    """
    seen: set[int] = set()
    queue: list = [exc]
    while queue:
        cur = queue.pop(0)
        if not isinstance(cur, BaseException) or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        queue += [cur.__cause__, cur.__context__, getattr(cur, "orig", None)]


def _is_database_frame(e: BaseException) -> bool:
    return (type(e).__module__ or "").split(".")[0] in ("sqlalchemy", "asyncpg")


def _is_lost_connection(e: BaseException) -> bool:
    """Evidence that a database connection is gone, not that a statement was
    bad. SQLAlchemy's OperationalError also carries server-side refusals
    (deadlocks, cancellations), so the flag or the wrapped cause has to say
    so — the wrapper class alone never does."""
    mod = type(e).__module__ or ""
    name = type(e).__name__
    if mod.startswith("asyncpg") and name in _ASYNCPG_LOST:
        return True
    if mod.startswith("sqlalchemy"):
        if getattr(e, "connection_invalidated", False):
            return True
        if name == "DisconnectionError":
            return True
    if isinstance(e, ConnectionError | socket.gaierror):
        return True
    if isinstance(e, OSError) and e.errno in _TRANSPORT_ERRNOS:
        return True
    return False


def _is_provider_transport(e: BaseException, *, pre_send_only: bool) -> bool:
    """Did this failure happen on the wire to the platform?

    `pre_send_only` is the no-double-submission guard. A POST that failed
    while connecting never reached the platform, so retrying it cannot
    duplicate anything; a POST that failed while reading the response may
    already have been accepted, so it is left to the ordinary retry budget
    exactly as before rather than being folded into an outage wait.
    """
    if isinstance(e, httpx.TransportError):
        if pre_send_only:
            return isinstance(e, httpx.ConnectError | httpx.ConnectTimeout
                              | httpx.ProxyError | httpx.PoolTimeout)
        return True
    if isinstance(e, socket.gaierror):
        return True
    if isinstance(e, ConnectionRefusedError):
        return True
    if pre_send_only:
        return False
    if isinstance(e, ConnectionError):
        return True
    return isinstance(e, OSError) and e.errno in _TRANSPORT_ERRNOS


def classify_outage(exc: BaseException, *,
                    pre_send_only: bool = False) -> str | None:
    """Name the unreachable dependency behind `exc`, or None when this is a
    real failure that must still fail fast.

    Matched on the exception CHAIN and on class first, errno second, text
    only where the failure crossed a process boundary and could not arrive
    any other way.
    """
    frames = list(_iter_causes(exc))
    for e in frames:
        if isinstance(e, DependencyDown):
            return e.dependency
    if any(_is_database_frame(e) for e in frames):
        # A database error is an outage only with evidence of connection
        # loss somewhere in its chain; a bad statement stays a failure.
        if any(_is_lost_connection(e) for e in frames):
            return "database"
        return None
    for e in frames:
        if _is_provider_transport(e, pre_send_only=pre_send_only):
            return "provider"
    return None


def response_outage(status_code: int, body: str) -> str | None:
    """Tell an unreachable provider from a genuine refusal in a platform
    error response.

    The platform runs the provider call itself and wraps a transport failure
    of its own as a 400 with the cause in `detail`:

        {"detail": "Provider batch submission failed:
                    [Errno -5] No address associated with hostname"}

    That 400 is not a bad request — nothing was accepted and nothing reached
    the provider, so the submission can be repeated safely once the name
    resolves again. A 400 whose detail names no transport cause is a real
    refusal and still fails fast.
    """
    if status_code in (502, 503, 504):
        return "provider"       # a gateway saying it cannot reach upstream
    detail = body
    try:
        payload = json.loads(body)
    except Exception:  # noqa: BLE001 — a non-JSON body is matched as text
        payload = None
    if isinstance(payload, dict) and payload.get("detail") is not None:
        d = payload["detail"]
        detail = d if isinstance(d, str) else json.dumps(d)
    return "provider" if _text_is_transport(detail) else None


def _outage_opened(dep: str, label: str, detail: str, limit: float) -> None:
    if dep in _OPEN_OUTAGES:
        log.debug("%s: %s still unreachable (%s)", label, dep, detail)
        return
    _OPEN_OUTAGES[dep] = time.monotonic()
    log.warning(
        "%s UNREACHABLE — round PAUSED, not failed. Waiting for it to come "
        "back (first hit: %s — %s); re-probing every %gs up to %s "
        "(BULK_OUTAGE_WAIT_SECONDS). Nothing is resubmitted on resume.",
        dep.upper(), label, detail, OUTAGE_BACKOFF_MAX, _dur(limit))


def _outage_closed(dep: str, label: str) -> None:
    started = _OPEN_OUTAGES.pop(dep, None)
    if started is None:
        log.debug("%s: %s answered again", label, dep)
        return
    log.info("%s reachable again after %s — round resumes (%s)",
             dep, _dur(time.monotonic() - started), label)


async def await_dependency(label: str, op, *, pre_send_only: bool = False,
                           cap: float | None = None):
    """Run `op()`, pausing for as long as a dependency is unreachable.

    Real failures propagate on the first try. An unreachable dependency never
    consumes the caller's retry budget: this waits with bounded backoff and
    returns `op()`'s result once it answers. If the outage outlasts the cap,
    raises DependencyDown chained from the last failure.
    """
    limit = OUTAGE_WAIT_SECONDS if cap is None else cap
    started: float | None = None
    dependency = ""
    delay = OUTAGE_BACKOFF_START
    while True:
        try:
            result = await op()
        except Exception as exc:  # noqa: BLE001 — classified, then re-raised
            kind = classify_outage(exc, pre_send_only=pre_send_only)
            if kind is None:
                raise
            now = time.monotonic()
            if started is None:
                started, dependency = now, kind
                _outage_opened(kind, label, str(exc)[:200], limit)
            waited = now - started
            if waited >= limit:
                _OPEN_OUTAGES.pop(dependency, None)
                log.error("%s still unreachable after %s — giving up on %s; "
                          "the job resumes from its checkpoint when restarted",
                          dependency, _dur(waited), label)
                raise DependencyDown(
                    dependency,
                    f"still unreachable after {_dur(waited)} "
                    f"({label}): {str(exc)[:150]}") from exc
            log.debug("%s: %s unreachable for %s; re-probing in %gs",
                      label, kind, _dur(waited), delay)
            await asyncio.sleep(min(delay, max(limit - waited, 0.0)))
            delay = min(delay * 2, OUTAGE_BACKOFF_MAX)
            continue
        if started is not None:
            _outage_closed(dependency, label)
        return result


# ── submission pacing: don't cause the burst in the first place ─────────────
# Second finding, 16 Sep: the [Errno -5] bursts are not only a sleeping
# laptop. Every container resolves the provider host correctly and a single
# one-prompt submission succeeds immediately — but three workers each firing
# ~10 chunk submissions back to back make the platform resolve the provider
# host many times at once, and a burst of simultaneous lookups intermittently
# fails inside the container. Waiting (above) survives the symptom; pacing
# removes the cause.
#
# One submission in flight per process, and at least a five-second gap after
# each one finishes. A ten-chunk round therefore costs at most ~45s more
# against a provider turnaround measured in minutes to hours — under a
# percent — and the platform's uploads never resolve in a thundering herd.
# The pacing is per-process by construction: three workers still submit three
# times as often, which is ~1 submission/1.7s rather than a burst of ten.
SUBMIT_CONCURRENCY = int(os.environ.get("BULK_SUBMIT_CONCURRENCY", "1"))
SUBMIT_STAGGER_SECONDS = float(
    os.environ.get("BULK_SUBMIT_STAGGER_SECONDS", "5"))

_submit_gate: tuple[object, asyncio.Semaphore] | None = None
_last_submit_at: float = 0.0


def _submit_gate_for_loop() -> asyncio.Semaphore:
    global _submit_gate
    loop = asyncio.get_running_loop()
    if _submit_gate is None or _submit_gate[0] is not loop:
        _submit_gate = (loop, asyncio.Semaphore(max(SUBMIT_CONCURRENCY, 1)))
    return _submit_gate[1]


async def paced_submit(op):
    """Run one submission, bounded and spaced process-wide.

    The gap is measured from the END of the previous submission: a batch POST
    carries up to SUBMIT_MAX prompts and the platform's uploads run while it
    is in flight, so spacing from its completion is what actually separates
    two bursts of name resolution.
    """
    global _last_submit_at
    async with _submit_gate_for_loop():
        gap = SUBMIT_STAGGER_SECONDS - (time.monotonic() - _last_submit_at)
        if gap > 0:
            log.debug("submission pacing: waiting %.1fs before the next POST",
                      gap)
            await asyncio.sleep(gap)
        try:
            return await op()
        finally:
            _last_submit_at = time.monotonic()


def group_outage(errors: list[str], group_size: int) -> bool:
    """Is a group of per-document failures an outage, or just failures?

    `oneshot_ingest` isolates a document's failure into its report, so a
    database that went away arrives as text rather than as an exception. The
    whole group failing with transport errors is the database; one document
    failing that way is one failed document and must stay one.
    """
    return (bool(errors) and len(errors) == group_size
            and all(_text_is_transport(e) for e in errors))


async def db_op(label: str, fn, *, cap: float | None = None):
    """Run `fn(session)` in a fresh session, pausing through database
    outages. The whole call is retried: a session that raised rolled back,
    so a retry re-runs work that was never committed."""
    from app.db import AsyncSessionLocal

    async def once():
        async with AsyncSessionLocal() as session:
            return await fn(session)

    return await await_dependency(label, once, cap=cap)

# ── process-pool gazetteer scanning ─────────────────────────────────────────
# Prompt prep is CPU-bound on gazetteer_scan (~250ms/doc at 63K entries) and
# the GIL makes threads useless for it. A small process pool with the
# gazetteer loaded once per worker turns a 35-minute prep (8K docs) into ~7.
_SCAN_WORKERS = int(os.environ.get("BULK_SCAN_WORKERS", "4"))
_worker_gazetteer = None


def _pool_init(gazetteer):
    global _worker_gazetteer
    _worker_gazetteer = gazetteer


def _pool_scan(content: str):
    from app.services.ingestion_oneshot import gazetteer_scan

    return gazetteer_scan(content, _worker_gazetteer)


async def scan_many(contents: list[str], gazetteer) -> list[dict]:
    """Gazetteer-scan many documents on a process pool. Falls back to
    in-process scanning if the pool can't start (e.g. spawn issues)."""
    import concurrent.futures as cf

    from app.services.ingestion_oneshot import gazetteer_scan

    if len(contents) < 50:
        return [gazetteer_scan(c, gazetteer) for c in contents]
    try:
        loop = asyncio.get_event_loop()
        with cf.ProcessPoolExecutor(
            max_workers=_SCAN_WORKERS,
            initializer=_pool_init, initargs=(gazetteer,),
        ) as pool:
            futs = [loop.run_in_executor(pool, _pool_scan, c)
                    for c in contents]
            return list(await asyncio.gather(*futs))
    except Exception as exc:  # noqa: BLE001 — never lose a job to the pool
        log.warning("scan pool failed (%s); falling back in-process",
                    str(exc)[:120])
        return [gazetteer_scan(c, gazetteer) for c in contents]


# ── stored-reply client (the proven fake) ───────────────────────────────────
class StoredReplyClient:
    """Serves batch replies keyed by the prompt's own markers. Non-chunk
    prompts are served in call order per filename."""

    def __init__(self, front: dict[str, list[str]], chunks: dict):
        self.front = {k: list(v) for k, v in front.items()}
        self.chunks = chunks
        self.misses: list[str] = []

    async def invoke(self, agent: str, message: str) -> str:
        m = CHUNK_RE.search(message)
        if m:
            key = (m.group(3).strip(), int(m.group(1)))
            reply = self.chunks.get(key)
            if reply is None:
                self.misses.append(f"chunk {key}")
                raise RuntimeError(f"no stored reply for chunk {key}")
            return reply
        fm = FILENAME_RE.search(message)
        fn = fm.group(1).strip() if fm else None
        queue = self.front.get(fn)
        if not queue:
            self.misses.append(f"front {fn}")
            raise RuntimeError(f"no stored reply left for {fn}")
        return queue.pop(0)


# ── sinas batch client (bounded, checkpointed, retrying) ────────────────────
class BatchClient:
    def __init__(self, job_dir: Path):
        from app.config import get_settings

        s = get_settings()
        self.base = s.sinas_url
        self.headers = {"Authorization": f"Bearer {s.sinas_api_key}"}
        self.job_dir = job_dir
        self.state_path = job_dir / "batches.json"
        self.state: dict = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists() else {}
        )

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1))

    async def _post_batch(self, agent: str, round_key: str,
                          chunk: list[str]) -> dict:
        """Submit one chunk.

        Raises DependencyDown when the platform answered that IT could not
        reach the provider (nothing was accepted, so repeating is safe),
        SubmitRejected on a refusal no retry can fix, and RuntimeError on the
        statuses worth retrying (429 rate-limit windows, 5xx).
        """
        async with httpx.AsyncClient(timeout=180.0) as c:
            r = await c.post(
                f"{self.base}/agents/{agent}/chats/batch",
                headers=self.headers,
                json={"inputs": [{"message": p} for p in chunk],
                      "execution_mode": "provider",
                      "trigger_id_prefix": f"sgr-bulk-{round_key}"},
            )
        if r.is_error:
            msg = f"submit rejected ({r.status_code}): {r.text[:200]}"
            dep = response_outage(r.status_code, r.text)
            if dep:
                raise DependencyDown(dep, msg)
            if 400 <= r.status_code < 500 and r.status_code not in _RETRYABLE:
                # A bad request is bad six times too. Failing here keeps a
                # malformed round out of the retry budget AND out of the
                # outage wait — neither mechanism may absorb it.
                raise SubmitRejected(msg)
            raise RuntimeError(msg)
        return r.json()

    async def _get_batch(self, batch_id: str) -> dict:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.get(f"{self.base}/batches/{batch_id}",
                            headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def _get_chat(self, chat_id: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.get(f"{self.base}/chats/{chat_id}",
                            headers=self.headers)
            r.raise_for_status()
            return r.json().get("messages") or []

    async def run_round(self, round_key: str, agent: str,
                        prompts: list[str]) -> list[str | None]:
        """Submit prompts (resuming prior submissions), await completion,
        return replies aligned with the prompt list."""
        rd = self.state.setdefault(round_key, {"chunks": []})
        # submit missing chunks
        for start in range(0, len(prompts), SUBMIT_MAX):
            idx = start // SUBMIT_MAX
            if idx < len(rd["chunks"]) and rd["chunks"][idx].get("batch_id"):
                continue  # resumed: already submitted
            chunk = prompts[start:start + SUBMIT_MAX]
            for attempt in (1, 2, 3, 4, 5, 6):
                try:
                    # The wait sits INSIDE the attempt: an unreachable
                    # provider pauses here and spends none of the six
                    # attempts, which exist for refusals with a retry (the
                    # Google 429 window below), not for outages. Every POST,
                    # first try or retry, goes through the pacer.
                    sub = await await_dependency(
                        f"{round_key} submit chunk {idx}",
                        lambda chunk=chunk: paced_submit(
                            lambda: self._post_batch(agent, round_key, chunk)),
                        pre_send_only=True,
                    )
                    while len(rd["chunks"]) <= idx:
                        rd["chunks"].append({})
                    rd["chunks"][idx] = {"batch_id": sub["batch_id"],
                                         "chat_ids": sub["chat_ids"],
                                         "n": len(chunk)}
                    self._save()
                    log.info("%s: submitted chunk %d (%d prompts, batch %s)",
                             round_key, idx, len(chunk), sub["batch_id"])
                    break
                except DependencyDown:
                    # The wait already outlasted its cap. Spending the six
                    # attempts on top of that buys nothing: the checkpoint
                    # holds, so a restart resumes without resubmitting.
                    raise
                except SubmitRejected:
                    raise       # a bad request: fail fast, don't retry it
                except Exception as exc:  # noqa: BLE001
                    if attempt == 6:
                        raise
                    # rate-limit windows (Google 429 on file uploads,
                    # 16 Aug 20:30) need minutes, not seconds
                    delay = min(60 * attempt, 300)
                    log.warning("%s submit attempt %d failed: %s — retry %ds",
                                round_key, attempt, str(exc)[:150], delay)
                    await asyncio.sleep(delay)
        # await all batches
        for ch in rd["chunks"]:
            while True:
                try:
                    st = await await_dependency(
                        f"{round_key} poll {ch['batch_id']}",
                        lambda ch=ch: self._get_batch(ch["batch_id"]),
                    )
                    if (st["completed"] + st["failed"] + st["cancelled"]
                            >= st["total"]) or st.get("status") in (
                            "completed", "failed", "cancelled"):
                        break
                except DependencyDown:
                    raise       # capped-out outage: end the round, resumably
                except Exception as exc:  # noqa: BLE001 — poll must not kill
                    log.warning("poll %s failed (%s); retrying",
                                ch["batch_id"], str(exc)[:120])
                await asyncio.sleep(POLL_SECONDS)
        # fetch replies, bounded-concurrent
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        replies: list[str | None] = [None] * len(prompts)

        async def fetch(i_global: int, chat_id: str) -> None:
            async with sem:
                for attempt in (1, 2, 3, 4, 5, 6):
                    try:
                        # A GET is idempotent, so the full outage set applies
                        # here — and an outage must not be spent on the three
                        # attempts below, which end by DROPPING the reply.
                        msgs = await await_dependency(
                            f"fetch {chat_id}",
                            lambda chat_id=chat_id: self._get_chat(chat_id),
                        )
                        replies[i_global] = next(
                            (m.get("content") or "" for m in reversed(msgs)
                             if m.get("role") == "assistant"), None)
                        return
                    except DependencyDown:
                        raise   # capped-out outage: fail the round loudly
                                # rather than silently lose every reply
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 3:
                            log.warning("fetch %s failed: %s", chat_id,
                                        str(exc)[:120])
                            return
                        await asyncio.sleep(5)

        tasks = []
        for ci, ch in enumerate(rd["chunks"]):
            base = ci * SUBMIT_MAX
            for j, chat_id in enumerate(ch["chat_ids"]):
                tasks.append(fetch(base + j, chat_id))
        await asyncio.gather(*tasks)
        got = sum(1 for x in replies if x)
        log.info("%s: %d/%d replies retrieved", round_key, got, len(prompts))
        return replies


# ── stages ──────────────────────────────────────────────────────────────────
async def _load_shared(session):
    from app.models import DocumentClass, EntityType
    from app.services import ingestion_oneshot as one_module
    from app.services.ingestion_oneshot import _load_gazetteer

    gazetteer = await _load_gazetteer(session)
    classes = [(c.id, c.name, c.description or "") for c in
               (await session.execute(select(DocumentClass))).scalars()]
    # Keyed beside the properties rather than added to the `classes` tuple:
    # four call sites unpack that tuple, and widening one several readers
    # unpack is how the naming check was silently killed twice this week.
    guidance_by_class = {
        c.id: c.summarization_guidance
        for c in (await session.execute(select(DocumentClass))).scalars()
    }
    entity_types = [
        {"id": t.id, "name": t.name,
         "guidance": (t.guidance or t.description or "").strip(),
         "creation_mode": t.creation_mode}
        for t in (await session.execute(select(EntityType))).scalars()]
    # Same reason the guidance is keyed separately: the `classes` tuple is
    # unpacked at four call sites. The rules are a deployment declaration
    # loaded once per run — see `ingestion_oneshot.load_class_rules`.
    class_rules = await one_module.load_class_rules(session)
    return gazetteer, classes, entity_types, guidance_by_class, class_rules


async def stage_extract(doc_ids: list[uuid.UUID], job_dir: Path) -> dict:
    """Round 1 (front prompts) -> round 2 (props+chunk prompts derived from
    round-1 replies) -> bounded replay-persist through the normal pipeline."""
    from app.models import Document, DocumentVersion
    from app.services import ingestion_oneshot as one
    from app.services.ingestion_runner import _wipe_extracted_artifacts

    client = BatchClient(job_dir)
    agent = one.DOC_METADATA_AGENT

    # worklist from data
    async def _worklist(session):
        (gazetteer, classes, entity_types, guidance_by_class,
         class_rules) = await _load_shared(session)
        work = []  # (id, filename, content)
        class_by_did: dict = {}
        for did in doc_ids:
            doc = await session.get(Document, did)
            if doc is None or (doc.summary or "").strip():
                continue
            version = (await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == did)
                .order_by(DocumentVersion.version.desc()).limit(1)
            )).scalars().first()
            if version is None or not (version.content_md or "").strip():
                continue
            work.append((did, doc.filename or "", version.content_md))
            # Kept beside `work` rather than inside it. Six places unpack that
            # tuple, and widening a tuple several readers unpack is how the
            # naming check was silently killed twice this week.
            class_by_did[did] = doc.document_class_id
        return (gazetteer, classes, entity_types, guidance_by_class,
                class_rules, work, class_by_did)

    (gazetteer, classes, entity_types, guidance_by_class, class_rules,
     work, class_by_did) = await db_op("extract worklist", _worklist)
    log.info("extract: %d docs need extraction", len(work))
    if not work:
        return {"extracted": 0, "skipped": len(doc_ids)}

    log.info("scanning %d docs on %d-process pool", len(work), _SCAN_WORKERS)
    scans = await scan_many([c for _, _, c in work], gazetteer)
    known_by_idx = {i: sc for i, sc in enumerate(scans)}

    # round 1: front-matter prompts (rule-hinted, same construction as
    # the live pipeline; class-props preloaded for rule hints)
    front_prompts: list[str] = []

    async def _class_props(session):
        from app.models import DocumentClassProperty

        by_class: dict = {}
        for cid, _, _ in classes:
            rows = (await session.execute(
                select(DocumentClassProperty).where(
                    DocumentClassProperty.document_class_id == cid,
                    DocumentClassProperty.manual.is_(False)))).scalars().all()
            # Same shape the one-shot sends, from the same helper: this is the
            # path bulk ingestion takes, so a prompt fix that lands only in
            # `ingestion_oneshot` never reaches the documents that arrive in
            # bulk, which is most of them.
            by_class[cid] = [one._prop_for_prompt(p) for p in rows]
        return by_class

    props_by_class = await db_op("extract class properties", _class_props)
    # Prompt construction below reads nothing from the database — it is out
    # of the session on purpose, so a sleeping laptop cannot lose a pooled
    # connection while the pipeline is only formatting strings.
    for wi, (did, fn, content) in enumerate(work):
        rule = one.classify_by_rules(fn, class_rules)
        hint = rule
        # A filename rule is one way to know the class, not the only one.
        # A document that arrives already classified -- source-declared, or
        # classified on an earlier pass -- has a class whether or not its
        # name matches a rule, and its class's properties and summary
        # guidance apply just the same. Deriving both from the rule alone
        # sent those documents a generic prompt and stored a summary
        # written to nobody's instructions. The live path in
        # `ingestion_oneshot` reads the document's own class here; this is
        # the same read, and this is the path most documents take.
        # Precedence follows the live path: a class the document already
        # has wins over one its filename suggests. Persistence keeps the
        # assigned class either way, so preferring the rule would extract
        # against one class's properties and store a summary written to its
        # guidance while the document remains another -- a summary for the
        # wrong class, which is worse than a generic one.
        cid = class_by_did.get(did)
        if cid is not None:
            # State it rather than only reading its config. Loading a
            # class's properties while still asking the model to pick
            # invites a different class back, whose properties are then
            # discarded and whose summary is not.
            fixed = next((n for c, n, _ in classes if c == cid), None)
            if fixed:
                hint = (fixed, 1.0, "already assigned")
        elif rule is not None:
            cid = next((c for c, n, _ in classes if n == rule[0]), None)
        class_props = props_by_class.get(cid) or None if cid else None
        known = known_by_idx[wi]
        front_prompts.append(one._front_matter_prompt(
            filename=fn, content=content,
            classes=[(n, d) for _, n, d in classes],
            entity_types=entity_types,
            known_entities=[c for c, _ in known.values()],
            class_hint=hint, properties=class_props,
            summary_guidance=guidance_by_class.get(cid) if cid else None))
    r1 = await client.run_round("extract-front", agent, front_prompts)

    # round 1.5: props follow-up prompts. Docs WITHOUT a filename-rule hint
    # get classified by round 1's reply; the live pipeline then makes a
    # second call for that class's properties. Mirror that branching here
    # or the replay starves on its second front call (smoke test, 16 Aug).
    props_prompts: list[str] = []
    props_owners: list[str] = []
    # No session here: this block reads nothing from the database (it was
    # holding a pooled connection open across pure string work).
    for wi, ((_did, fn, content), reply) in enumerate(zip(work, r1, strict=False)):
        if not reply:
            continue
        rule = one.classify_by_rules(fn, class_rules)
        try:
            data = one._parse_json_reply(reply)
        except Exception:  # noqa: BLE001 — replay will surface it per-doc
            continue
        cls_name = str(data.get("document_class") or "")
        cid = next((c for c, n, _ in classes if n == cls_name), None)
        hinted_matches = rule is not None and cls_name == rule[0]
        if cid is None or hinted_matches:
            continue
        cprops = props_by_class.get(cid) or []
        if not cprops:
            continue
        known = known_by_idx[wi]
        props_prompts.append(one._front_matter_prompt(
            filename=fn, content=content,
            classes=[(n, d) for _, n, d in classes],
            entity_types=entity_types,
            known_entities=[c for c, _ in known.values()],
            class_hint=(cls_name, 1.0, "already classified"),
            properties=cprops,
            summary_guidance=guidance_by_class.get(cid)))
        props_owners.append(fn)
    r15 = (await client.run_round("extract-props", agent, props_prompts)
           if props_prompts else [])

    # round 2: chunk prompts for long docs
    chunk_prompts: list[str] = []
    chunk_keys: list[tuple[str, int]] = []
    for wi, ((_did, fn, content), reply) in enumerate(zip(work, r1, strict=False)):
        if not reply:
            continue
        chunks = one._entity_chunks(content)
        if len(chunks) <= 1:
            continue
        known = known_by_idx[wi]
        known_names = ", ".join(sorted(c for c, _ in known.values())[:120]) or "(none)"
        for i, chunk in enumerate(chunks, start=1):
            chunk_prompts.append(one._ENTITY_CHUNK_PROMPT.format(
                types=", ".join(t["name"] for t in entity_types),
                type_guidance="\n".join(
                    f"- {t['name']}: {t['guidance']}" for t in entity_types),
                known=known_names, i=i, n=len(chunks), filename=fn,
                chunk=chunk))
            chunk_keys.append((fn, i))
    r2 = (await client.run_round("extract-chunks", agent, chunk_prompts)
          if chunk_prompts else [])

    # persist via the normal pipeline with stored replies, bounded groups
    front_map: dict[str, list[str]] = defaultdict(list)
    for (_did, fn, _), reply in zip(work, r1, strict=False):
        if reply:
            front_map[fn].append(reply)
    for fn, reply in zip(props_owners, r15, strict=False):
        if reply:
            front_map[fn].append(reply)  # served second, after the front reply
    chunk_map = {k: v for k, v in zip(chunk_keys, r2, strict=False) if v}

    ok = 0
    failed: dict[str, str] = {}
    for start in range(0, len(work), PERSIST_GROUP):
        group = work[start:start + PERSIST_GROUP]

        async def _persist_group(group=group):
            """One group, whole. Retried as a unit through a database
            outage: the wipe and the replay are both re-runnable (that is
            what makes every stage here resumable), and the replay client is
            rebuilt each time because it CONSUMES its stored replies."""
            async def _wipe(session, did):
                await _wipe_extracted_artifacts(session, did)
                await session.commit()

            for did, _, _ in group:
                await db_op(f"extract-wipe {did}",
                            lambda s, did=did: _wipe(s, did))
            replay = StoredReplyClient(front_map, chunk_map)
            reports = await one.oneshot_ingest(
                [did for did, _, _ in group], write=True,
                concurrency=PERSIST_CONCURRENCY, sinas=replay)
            # Nothing in this replay talks to the provider — the replies are
            # already stored — so a transport failure across the whole group
            # can only be the database.
            errs = [str(r.get("error") or "") for r in reports if r.get("error")]
            if group_outage(errs, len(group)):
                raise DependencyDown("database", errs[0][:200])
            return reports

        reports = await await_dependency(
            f"extract-persist {start // PERSIST_GROUP}", _persist_group)
        for (_did, fn, _), rep in zip(group, reports, strict=False):
            if rep.get("error"):
                failed[fn] = str(rep["error"])[:200]
            else:
                ok += 1
        log.info("extract-persist: %d/%d (ok %d, failed %d)",
                 min(start + PERSIST_GROUP, len(work)), len(work), ok,
                 len(failed))
    return {"extracted": ok, "failed": failed}


async def stage_resolve(doc_ids: list[uuid.UUID], job_dir: Path) -> dict:
    """Batched middle: grounding round then adjudication round, both via
    provider batches (half price, no interactive latency — the 2-min/call
    interactive path measured 16 Aug made this stage a 7-18h wall)."""
    from sqlalchemy import select as _select

    from app.models import Entity, EntityType
    from app.services import entity_resolver as er
    from app.services import grounding_gate as gg

    client = BatchClient(job_dir)
    errors: dict[str, str] = {}

    # ── grounding round ──
    g_items: list[tuple[uuid.UUID, dict]] = []
    for did in doc_ids:
        try:
            col = await db_op(f"ground-collect {did}",
                              lambda s, did=did: gg.ground_collect(s, did))
            if col["prompt"]:
                g_items.append((did, col))
        except DependencyDown:
            # Past the cap. Recording this per document would write one
            # "error" for every document in the corpus and call it work.
            raise
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"ground-collect: {str(exc)[:150]}"
    log.info("middle: %d/%d docs need grounding", len(g_items), len(doc_ids))
    g_replies = await client.run_round(
        "ground", gg.GROUNDING_AGENT, [c["prompt"] for _, c in g_items]
    ) if g_items else []
    g_done = 0
    for (did, col), reply in zip(g_items, g_replies, strict=False):
        try:
            await db_op(
                f"ground-apply {did}",
                lambda s, col=col, reply=reply: gg.ground_apply(
                    s, col["mention_ids"], col.get("surfaces") or [], reply,
                    write=True))
            g_done += 1
        except DependencyDown:
            raise
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"ground-apply: {str(exc)[:150]}"
    log.info("middle: grounding applied %d/%d", g_done, len(g_items))

    # ── adjudication round (post-grounding mention state) ──
    async def _index(session):
        entities = (await session.execute(_select(Entity))).scalars().all()
        return (er._EntityIndex(entities),
                {t.id: t for t in
                 (await session.execute(_select(EntityType))).scalars()})

    index, types = await db_op("resolve entity index", _index)

    r_items: list[tuple[uuid.UUID, dict]] = []
    for did in doc_ids:
        try:
            col = await db_op(
                f"resolve-collect {did}",
                lambda s, did=did: er.resolve_collect(s, index, types, did))
            r_items.append((did, col))
        except DependencyDown:
            raise
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"resolve-collect: {str(exc)[:150]}"
    flat_prompts = [c["prompt"] for _, col in r_items for c in col["chunks"]]
    log.info("middle: %d adjudication prompts across %d docs",
             len(flat_prompts), len(r_items))
    r_replies = await client.run_round(
        "adjudicate", er.RESOLVER_AGENT, flat_prompts
    ) if flat_prompts else []
    it = iter(r_replies)
    resolved = 0
    for did, col in r_items:
        replies = [next(it) for _ in col["chunks"]]
        try:
            await db_op(
                f"resolve-apply {did}",
                lambda s, did=did, col=col, replies=replies: er.resolve_apply(
                    s, index, types, did, col["chunks"], replies,
                    col["creation"], write=True))
            resolved += 1
        except DependencyDown:
            raise
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"resolve-apply: {str(exc)[:150]}"
        if resolved % 250 == 0:
            log.info("middle: resolve applied %d/%d", resolved, len(r_items))
    return {"resolved": resolved, "grounded": g_done, "errors": errors}


async def stage_relationships(doc_ids: list[uuid.UUID], job_dir: Path) -> dict:
    """Build relationship prompts from resolved mentions, batch, persist
    through the standard extractor with stored replies."""
    from app.services import relationship_oneshot as rel

    client = BatchClient(job_dir)

    definitions = await db_op("relationship definitions",
                              rel.load_definitions)

    # collect prompts per doc via a collecting client run in no-write mode
    class Collector:
        def __init__(self):
            self.prompts: list[str] = []

        async def invoke(self, agent: str, message: str) -> str:
            self.prompts.append(message)
            raise _Collected()

    class _Collected(Exception):
        pass

    async def _collect(session, did):
        # The collector is built INSIDE the attempt: a retry through a
        # database outage must not append a second copy of every prompt.
        col = Collector()
        try:
            await rel.extract_document(session, col, did,
                                       definitions=definitions, write=False)
        except Exception as exc:  # noqa: BLE001 — _Collected or prep issues
            if classify_outage(exc) is not None:
                raise
        return list(col.prompts)

    doc_prompts: list[tuple[uuid.UUID, list[str]]] = []
    for did in doc_ids:
        prompts = await db_op(f"rel-collect {did}",
                              lambda s, did=did: _collect(s, did))
        if prompts:
            doc_prompts.append((did, prompts))
    flat = [p for _, ps in doc_prompts for p in ps]
    log.info("relationships: %d docs -> %d prompts",
             len(doc_prompts), len(flat))
    if not flat:
        return {"rel_docs": 0}

    replies = await client.run_round("relationships", rel.RELATIONSHIP_AGENT
                                     if hasattr(rel, "RELATIONSHIP_AGENT")
                                     else "sgr/relationship-oneshot-agent",
                                     flat)

    # persist: same extractor, stored replies keyed by consumption order
    it = iter(replies)
    per_doc_replies = {did: [next(it) for _ in ps] for did, ps in doc_prompts}

    class Stored:
        def __init__(self, queue: list):
            self.queue = list(queue)

        async def invoke(self, agent: str, message: str) -> str:
            if not self.queue or self.queue[0] is None:
                raise RuntimeError("missing stored relationship reply")
            return self.queue.pop(0)

    ok = 0
    errors: dict[str, str] = {}
    sem = asyncio.Semaphore(PERSIST_CONCURRENCY)

    async def persist(did: uuid.UUID) -> None:
        nonlocal ok
        async with sem:
            try:
                # Stored() is rebuilt per attempt for the same reason the
                # replay client is: it consumes its queue as it serves it.
                await db_op(
                    f"rel-persist {did}",
                    lambda s, did=did: rel.extract_document(
                        s, Stored(per_doc_replies[did]), did,
                        definitions=definitions, write=True))
                ok += 1
            except DependencyDown:
                raise
            except Exception as exc:  # noqa: BLE001
                errors[str(did)] = str(exc)[:200]

    for start in range(0, len(doc_prompts), MIDDLE_GROUP):
        await asyncio.gather(*(persist(did) for did, _ in
                               doc_prompts[start:start + MIDDLE_GROUP]))
        log.info("rel-persist: %d/%d", min(start + MIDDLE_GROUP,
                 len(doc_prompts)), len(doc_prompts))
    return {"rel_docs": ok, "errors": errors}


# ── driver ──────────────────────────────────────────────────────────────────
async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True)
    ap.add_argument("--stages", default="extract,resolve,relationships")
    ap.add_argument("--job-dir", required=True)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    job_dir = Path(args.job_dir).expanduser()
    job_dir.mkdir(parents=True, exist_ok=True)
    doc_ids = [uuid.UUID(line.strip()) for line in
               Path(args.ids_file).read_text().splitlines() if line.strip()]
    stages = args.stages.split(",")
    report: dict = {"doc_count": len(doc_ids), "stages": {}}

    if "extract" in stages:
        report["stages"]["extract"] = await stage_extract(doc_ids, job_dir)
    if "resolve" in stages:
        report["stages"]["resolve"] = await stage_resolve(doc_ids, job_dir)
    if "relationships" in stages:
        report["stages"]["relationships"] = await stage_relationships(
            doc_ids, job_dir)

    (job_dir / "report.json").write_text(json.dumps(report, indent=1,
                                                    default=str))
    print(json.dumps({k: (v if not isinstance(v, dict) else
                          {kk: (vv if not isinstance(vv, dict) else len(vv))
                           for kk, vv in v.items()})
                      for k, v in report["stages"].items()}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
