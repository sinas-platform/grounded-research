"""The result a run retrieves belongs to the run's owner, never to a guess.

`store_result` used to pick its owner by reading the most recently created
result's owner from the table. Every run had been an admin's, so nobody saw
the consequence: the first non-admin run stored a result owned by whoever
ran before, the answer inherited that owner, and the asker was refused their
own answer's evidence with "answer not found".
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app import retrieval_first as rf
from app.services import query_runner as qr


@pytest.mark.asyncio
async def test_the_retrieval_stage_stores_the_result_as_the_run_owner(
    monkeypatch,
) -> None:
    run = SimpleNamespace(
        question="Q?",
        effort="medium",
        parent_result_id=None,
        owner_id=uuid.uuid4(),
        roles=["reviewers"],
    )
    stored: dict = {}

    class _Session:
        async def get(self, _model, _ident):
            return run

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    async def _noop(*_a, **_k):
        return None

    async def _returning(value):
        return value

    async def _store_result(question, ranked, briefing, plan=None, **kw):
        stored.update(kw)
        return str(uuid.uuid4())

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(qr, "_mark", _noop)
    monkeypatch.setattr(qr, "_tele", _noop)
    monkeypatch.setattr(qr, "_check_cancel", _noop)
    monkeypatch.setattr(rf, "plan_question", lambda *a, **k: _returning({}))
    monkeypatch.setattr(rf, "retrieve_and_rank", lambda *a, **k: _returning([]))
    monkeypatch.setattr(rf, "build_briefing", lambda *a, **k: _returning([]))
    monkeypatch.setattr(rf, "store_result", _store_result)

    await qr._stage_retrieve_first(uuid.uuid4())

    assert stored == {"owner_id": run.owner_id, "roles": ["reviewers"]}


@pytest.mark.asyncio
async def test_store_result_writes_the_given_owner_and_never_guesses_one(
    monkeypatch,
) -> None:
    owner = uuid.uuid4()
    executed: list[tuple[str, dict]] = []

    class _Session:
        async def execute(self, stmt, params=None):
            executed.append((str(stmt), dict(params or {})))
            return SimpleNamespace(scalar=lambda: None)

        async def commit(self):
            return None

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    import app.db

    monkeypatch.setattr(app.db, "AsyncSessionLocal", _session_local)

    await rf.store_result("Q?", [], [], None, owner_id=owner, roles=["reviewers"])

    inserts = [(sql, p) for sql, p in executed if "INSERT INTO result " in sql]
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params["o"] == str(owner)
    assert params["roles"] == ["reviewers"]
    # No read of anyone else's owner: the only statement is the insert.
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_store_result_requires_an_owner() -> None:
    with pytest.raises(TypeError):
        await rf.store_result("Q?", [], [], None)  # type: ignore[call-arg]
