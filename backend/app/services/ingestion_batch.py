"""Provider-batch execution for the one-shot ingestion pipeline.

The per-document one-shot logic runs UNCHANGED. A wave client parks every
`invoke()` call instead of firing it; when no new call has parked for a
settle interval (i.e. every in-flight document is waiting), the wave is
submitted as ONE Sinas provider batch (`execution_mode="provider"`, ~50%
cost, up to 24h turnaround) and the replies resolve the parked futures.
Documents then continue — follow-up property calls and long-document chunk
fan-outs simply form the next wave. A premature flush is harmless: the
stragglers park into the following wave.

Scale note: documents are processed in slices (SLICE cap) so a 10k-doc run
is a handful of provider batches, not thousands of asyncio tasks at once.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import IngestionRun

log = logging.getLogger(__name__)

SLICE = 2000          # documents driven concurrently per slice
# Sinas's MAX_BATCH_SIZE is 1000, but large sub-batches produce multi-10MB
# submit bodies, and Sinas dropped two such POSTs cold on 16 Aug (1,180
# banked failures from 2 requests). 400 keeps bodies ~15MB with prompt-heavy
# relationship waves.
SUBMIT_MAX = 400
SETTLE_SECONDS = 10.0  # wide enough to bridge CPU-serialized parking gaps (15 Aug)  # no new park for this long -> flush the wave
POLL_SECONDS = 30.0   # provider batch progress poll cadence


class BatchWaveClient:
    """Drop-in for the runner's Sinas client: same async invoke() shape,
    but calls are parked and executed in provider batches per wave."""

    def __init__(self, run_id: uuid.UUID | None = None):
        s = get_settings()
        self.base = s.sinas_url
        self.headers = {"Authorization": f"Bearer {s.sinas_api_key}"}
        self.run_id = run_id
        self._parked: list[tuple[str, str, asyncio.Future[str]]] = []
        self._unfinished = 0
        self._closed = False

    # ── the injected surface ────────────────────────────────────────────
    async def invoke(self, agent: str, message: str) -> str:
        if self._closed:
            raise RuntimeError("wave client is closed")
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._parked.append((agent, message, fut))
        return await fut

    # ── driver ──────────────────────────────────────────────────────────
    async def drive(self, doc_tasks: list[asyncio.Task]) -> None:
        """Flush waves until every document task finishes."""
        self._unfinished = len(doc_tasks)
        try:
            while any(not t.done() for t in doc_tasks):
                await self._settle(doc_tasks)
                if self._parked:
                    wave, self._parked = self._parked, []
                    await self._run_wave(wave)
        finally:
            self._closed = True
            for _, _, fut in self._parked:
                if not fut.done():
                    fut.set_exception(RuntimeError("run ended before flush"))

    async def _settle(self, doc_tasks: list[asyncio.Task]) -> None:
        """Wait until the parked set is stable (or all tasks finished)."""
        last = -1
        while any(not t.done() for t in doc_tasks):
            count = len(self._parked)
            if count > 0 and count == last:
                return
            last = count
            await asyncio.sleep(SETTLE_SECONDS)

    async def _run_wave(self, wave: list[tuple[str, str, asyncio.Future[str]]]) -> None:
        by_agent: dict[str, list[tuple[str, asyncio.Future[str]]]] = {}
        for agent, message, fut in wave:
            by_agent.setdefault(agent, []).append((message, fut))
        for agent, items in by_agent.items():
            # Sinas rejects submissions over its MAX_BATCH_SIZE (default 1000)
            # with a 400, so a settled wave larger than that is split here.
            # Sub-batches run CONCURRENTLY: they are independent server-side
            # batches, and serializing them would add a full provider
            # round-trip per thousand documents. A sub-batch failure fails
            # only its own futures (seen 15 Aug: one 1223-item wave 400'd
            # and took the whole slice with it).
            chunks = [
                items[i : i + SUBMIT_MAX]
                for i in range(0, len(items), SUBMIT_MAX)
            ]
            results = await asyncio.gather(
                *(self._submit_and_resolve(agent, chunk) for chunk in chunks),
                return_exceptions=True,
            )
            for chunk, outcome in zip(chunks, results):
                if isinstance(outcome, BaseException):
                    log.error(
                        "provider batch wave failed for %s: %s", agent, outcome
                    )
                    for _, fut in chunk:
                        if not fut.done():
                            fut.set_exception(
                                outcome
                                if isinstance(outcome, Exception)
                                else RuntimeError(str(outcome))
                            )

    async def _submit_and_resolve(
        self, agent: str, items: list[tuple[str, asyncio.Future[str]]]
    ) -> None:
        # One retry with backoff: Sinas has dropped submit connections cold
        # under load (16 Aug — mechanism still undiagnosed). A transient
        # drop must cost one retry, not a whole wave of banked failures.
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=180.0) as c:
                    r = await c.post(
                        f"{self.base}/agents/{agent}/chats/batch",
                        headers=self.headers,
                        json={
                            "inputs": [{"message": m} for m, _ in items],
                            "execution_mode": "provider",
                            "trigger_id_prefix": f"grove-ingest-{self.run_id or 'adhoc'}",
                        },
                    )
                if r.is_error:
                    # Surface the server's stated reason — a bare
                    # raise_for_status cost three diagnostic round-trips 15 Aug.
                    raise RuntimeError(
                        f"batch submit rejected ({r.status_code}): {r.text[:300]}"
                    )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 2:
                    raise
                log.warning("batch submit attempt 1 failed (%s); retrying in 30s",
                            str(exc)[:200])
                await asyncio.sleep(30)
        sub = r.json()
        batch_id, chat_ids = sub["batch_id"], sub["chat_ids"]
        log.info("provider batch %s submitted: %d inputs (%s)",
                 batch_id, len(items), agent)
        await self._record_batch_id(batch_id)

        while True:
            await asyncio.sleep(POLL_SECONDS)
            if await self._run_cancelled():
                async with httpx.AsyncClient(timeout=60.0) as c:
                    await c.post(f"{self.base}/batches/{batch_id}/cancel",
                                 headers=self.headers)
                raise RuntimeError("ingestion run cancelled")
            try:
                async with httpx.AsyncClient(timeout=60.0) as c:
                    r = await c.get(f"{self.base}/batches/{batch_id}",
                                    headers=self.headers)
                    r.raise_for_status()
                    st = r.json()
            except Exception as exc:  # noqa: BLE001 — transient poll failure
                # A missed poll (Sinas blip/restart) must not fail the wave;
                # the provider batch keeps running regardless.
                log.warning("batch %s poll failed (%s); retrying next cycle",
                            batch_id, str(exc)[:150])
                continue
            terminal = st["completed"] + st["failed"] + st["cancelled"]
            if terminal >= st["total"] or st.get("status") in (
                "completed", "failed", "cancelled"
            ):
                break

        async with httpx.AsyncClient(timeout=120.0) as c:
            for (message, fut), chat_id in zip(items, chat_ids):
                try:
                    r = await c.get(f"{self.base}/chats/{chat_id}",
                                    headers=self.headers)
                    r.raise_for_status()
                    msgs = r.json().get("messages") or []
                    reply = next(
                        (m.get("content") or "" for m in reversed(msgs)
                         if m.get("role") == "assistant"),
                        None,
                    )
                except Exception as exc:  # noqa: BLE001
                    if not fut.done():
                        fut.set_exception(exc)
                    continue
                if not fut.done():
                    if reply:
                        fut.set_result(reply)
                    else:
                        fut.set_exception(
                            RuntimeError(f"no assistant reply in chat {chat_id}")
                        )

    async def _record_batch_id(self, batch_id: str) -> None:
        if self.run_id is None:
            return
        async with AsyncSessionLocal() as session:
            run = await session.get(IngestionRun, self.run_id)
            if run is not None:
                ids = dict(run.sinas_batch_ids or {})
                ids.setdefault("provider_batches", [])
                ids["provider_batches"] = list(ids["provider_batches"]) + [batch_id]
                run.sinas_batch_ids = ids
                await session.commit()

    async def _run_cancelled(self) -> bool:
        if self.run_id is None:
            return False
        async with AsyncSessionLocal() as session:
            run = await session.get(IngestionRun, self.run_id)
            return run is None or run.status in ("cancelled", "failed")


async def batch_relationship_pass(
    run_id: uuid.UUID | None, doc_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Run relationship extraction over `doc_ids` through provider batches.

    Same wave mechanics as the extract pass: per-doc chunk calls park into
    settled waves (sub-batched under Sinas's MAX_BATCH_SIZE) and per-doc
    processing keeps every transaction short. Returns one report per doc.
    """
    from app.db import AsyncSessionLocal
    from app.services.relationship_oneshot import extract_document, load_definitions

    results: dict[uuid.UUID, dict[str, Any]] = {}
    for i in range(0, len(doc_ids), SLICE):
        chunk = doc_ids[i : i + SLICE]
        client = BatchWaveClient(run_id)

        async def drive_chunk(chunk: list[uuid.UUID] = chunk,
                              client: "BatchWaveClient" = client) -> None:
            async with AsyncSessionLocal() as session:
                definitions = await load_definitions(session)
            async def one(did: uuid.UUID) -> None:
                async with AsyncSessionLocal() as session:
                    try:
                        rep = await extract_document(
                            session, client, did,
                            definitions=definitions, write=True,
                        )
                    except Exception as exc:  # per-doc isolation
                        rep = {"document": str(did), "error": str(exc)[:300]}
                    results[did] = rep
            await asyncio.gather(*(one(d) for d in chunk))

        task = asyncio.ensure_future(drive_chunk())
        await client.drive([task])
        await task
    return results


async def batch_oneshot_ingest(
    run_id: uuid.UUID | None, doc_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Run the extract pass over `doc_ids` through provider batches.
    Returns one report per document (same shape as oneshot_ingest)."""
    from app.services.ingestion_oneshot import oneshot_ingest

    reports: list[dict[str, Any]] = []
    for i in range(0, len(doc_ids), SLICE):
        chunk = doc_ids[i : i + SLICE]
        client = BatchWaveClient(run_id)
        task = asyncio.ensure_future(
            oneshot_ingest(chunk, write=True, concurrency=len(chunk),
                           sinas=client)
        )
        await client.drive([task])
        reports.extend(await task)
    return reports
