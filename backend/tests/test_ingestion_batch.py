"""Provider-batch wave client: parking, settle-flush, multi-wave flows."""

import asyncio
import uuid

import pytest

from app.services import ingestion_batch
from app.services.ingestion_batch import BatchWaveClient


@pytest.mark.asyncio
async def test_waves_flush_and_second_calls_form_next_wave(monkeypatch):
    monkeypatch.setattr(ingestion_batch, "SETTLE_SECONDS", 0.05)
    client = BatchWaveClient(run_id=None)
    waves: list[list[str]] = []

    async def fake_submit(agent, items):
        waves.append([m for m, _ in items])
        for m, fut in items:
            fut.set_result(f"reply:{m}")

    monkeypatch.setattr(client, "_submit_and_resolve", fake_submit)

    async def doc_one_call(n):
        return await client.invoke("grove/doc-metadata-agent", f"doc{n}")

    async def doc_two_calls(n):
        first = await client.invoke("grove/doc-metadata-agent", f"doc{n}")
        second = await client.invoke("grove/doc-metadata-agent", f"doc{n}-props")
        return first, second

    tasks = [
        asyncio.ensure_future(doc_one_call(1)),
        asyncio.ensure_future(doc_one_call(2)),
        asyncio.ensure_future(doc_two_calls(3)),
    ]
    await client.drive(tasks)

    assert [await t for t in tasks[:2]] == ["reply:doc1", "reply:doc2"]
    assert await tasks[2] == ("reply:doc3", "reply:doc3-props")
    # doc3's follow-up call landed in a later wave, not the first
    assert len(waves) >= 2
    assert "doc3-props" not in waves[0]
    assert any("doc3-props" in w for w in waves[1:])


@pytest.mark.asyncio
async def test_wave_failure_isolates_to_its_futures(monkeypatch):
    monkeypatch.setattr(ingestion_batch, "SETTLE_SECONDS", 0.05)
    client = BatchWaveClient(run_id=None)

    async def fake_submit(agent, items):
        raise RuntimeError("provider rejected batch")

    monkeypatch.setattr(client, "_submit_and_resolve", fake_submit)

    async def doc(n):
        return await client.invoke("grove/doc-metadata-agent", f"doc{n}")

    tasks = [asyncio.ensure_future(doc(1))]
    await client.drive(tasks)
    with pytest.raises(RuntimeError, match="provider rejected"):
        await tasks[0]


def test_run_create_accepts_batch_flag():
    from app.schemas.ingestion import RunCreateIn

    body = RunCreateIn(filter={"document_ids": [str(uuid.uuid4())]}, batch=True)
    assert body.batch is True
    assert RunCreateIn(filter={}).batch is False
