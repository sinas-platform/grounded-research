"""build_corpus_map is cached.

It describes the shape of the corpus — entity types with example values,
document classes, properties — which changes only on ingestion. Rebuilding it
per question meant a full-corpus aggregation on the hot path: ~14s with the
entity_id index present, and before the query was reshaped it exceeded
temp_file_limit outright and killed the run.
"""

from __future__ import annotations

import asyncio

import pytest

from app import retrieval_first


@pytest.fixture(autouse=True)
def _clear_cache():
    retrieval_first.invalidate_corpus_map()
    yield
    retrieval_first.invalidate_corpus_map()


def test_second_call_does_not_rebuild(monkeypatch):
    calls = []

    async def fake_build():
        calls.append(1)
        return "MAP"

    monkeypatch.setattr(retrieval_first, "_build_corpus_map_uncached", fake_build)

    async def run():
        first = await retrieval_first.build_corpus_map()
        second = await retrieval_first.build_corpus_map()
        return first, second

    first, second = asyncio.run(run())
    assert first == second == "MAP"
    assert len(calls) == 1


def test_invalidate_forces_rebuild(monkeypatch):
    calls = []

    async def fake_build():
        calls.append(1)
        return "MAP%d" % len(calls)

    monkeypatch.setattr(retrieval_first, "_build_corpus_map_uncached", fake_build)

    async def run():
        a = await retrieval_first.build_corpus_map()
        retrieval_first.invalidate_corpus_map()
        b = await retrieval_first.build_corpus_map()
        return a, b

    a, b = asyncio.run(run())
    assert (a, b) == ("MAP1", "MAP2")
    assert len(calls) == 2


def test_concurrent_callers_build_once(monkeypatch):
    calls = []

    async def fake_build():
        calls.append(1)
        await asyncio.sleep(0.05)  # let the other waiters pile up on the lock
        return "MAP"

    monkeypatch.setattr(retrieval_first, "_build_corpus_map_uncached", fake_build)

    async def run():
        return await asyncio.gather(*(retrieval_first.build_corpus_map() for _ in range(5)))

    results = asyncio.run(run())
    assert results == ["MAP"] * 5
    assert len(calls) == 1
