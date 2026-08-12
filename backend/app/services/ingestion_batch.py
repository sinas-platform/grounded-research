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
SETTLE_SECONDS = 1.5  # no new park for this long -> flush the wave
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
            try:
                await self._submit_and_resolve(agent, items)
            except Exception as exc:  # noqa: BLE001 — fail this wave's futures
                log.exception("provider batch wave failed for %s", agent)
                for _, fut in items:
                    if not fut.done():
                        fut.set_exception(exc)

    async def _submit_and_resolve(
        self, agent: str, items: list[tuple[str, asyncio.Future[str]]]
    ) -> None:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                f"{self.base}/agents/{agent}/chats/batch",
                headers=self.headers,
                json={
                    "inputs": [{"message": m} for m, _ in items],
                    "execution_mode": "provider",
                    "trigger_id_prefix": f"grove-ingest-{self.run_id or 'adhoc'}",
                },
            )
            r.raise_for_status()
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
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.get(f"{self.base}/batches/{batch_id}",
                                headers=self.headers)
                r.raise_for_status()
                st = r.json()
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
