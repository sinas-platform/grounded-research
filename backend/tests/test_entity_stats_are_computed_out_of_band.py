"""Two facts about an entity — how many documents mention it, and whether
anything ever recognised it — are refreshed into a table by the maintenance
pass, not counted on the planning path.

Both were computed per question, per entity, out of the mention table, in
four places. On 22.7 million mentions the recognised count alone ran 169
seconds for one ubiquitous entity; the document count ran for every entity
a resolver's pattern matched before its cut to six, and a seed written as
"Kestrel Holdings v Northmoor Authority (T-123/45 P)" is split on its
punctuation into fragments like `45 P)` that match thousands of entities.
Planning spent nine to twelve minutes between two model calls of 18 seconds.

What these pin:

- one statement recomputes the table whole from active mentions, upserting
  by entity, so a merge or a status change moves the count and a new entity
  gets a row on the next pass;
- the refresh is the first step of maintenance, and the table is built once
  at boot when it is empty, for the reason the corpus profile is;
- a boot failure never takes the backend down.

No database: the session is a recorder.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from app.services import generic_entities as ge
from app.services import maintenance

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


class _Result:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, upserted=0):
        self.upserted = upserted
        self.calls: list[tuple[str, dict]] = []
        self.committed = False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, dict(params or {})))
        if "INSERT INTO entity_stats" in sql:
            return _Result(rowcount=self.upserted)
        raise AssertionError(f"unexpected statement: {sql[:80]}")

    async def commit(self):
        self.committed = True


# ─────────────────────────────────────────────────────────────
# The refresh
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_refresh_is_one_upsert_over_active_mentions():
    s = _Session(upserted=892_691)
    out = await ge.refresh_entity_stats(s, when=NOW)
    (sql, params), = s.calls
    assert params["blind"] == list(ge.BLIND_LINK_METHODS)
    assert params["when"] == NOW
    assert out == {"entities": 892_691}
    assert s.committed


def test_the_statement_recomputes_both_facts_and_upserts_by_entity():
    sql = str(ge._REFRESH_ENTITY_STATS)
    assert "count(DISTINCT document_id)" in sql
    assert "bool_or(link_method IS NOT NULL" in sql
    assert "NOT (link_method = ANY(:blind))" in sql
    assert "status = 'active'" in sql
    assert "GROUP BY entity_id" in sql
    assert "ON CONFLICT (entity_id) DO UPDATE" in sql, (
        "a merge or a status change must move the count: the row is "
        "replaced, not kept")


# ─────────────────────────────────────────────────────────────
# The wiring
# ─────────────────────────────────────────────────────────────
def test_the_maintenance_pass_refreshes_first():
    src = inspect.getsource(maintenance.run_maintenance)
    assert "await refresh_entity_stats(" in src
    assert src.index("await refresh_entity_stats(") < src.index("await replay_unresolved("), (
        "the planner reads these figures on every question; nothing else "
        "in the pass is waited for before they are refreshed")


def test_the_table_is_built_once_at_boot_when_empty():
    src = inspect.getsource(maintenance.maintenance_loop)
    assert "ensure_entity_stats" in src
    assert src.index("ensure_entity_stats") < src.index("while True")


@pytest.mark.asyncio
async def test_a_deployment_that_has_rows_is_left_alone(monkeypatch):
    built = []

    async def _refresh(session, when):
        built.append(when)
        return {}

    class _Has:
        async def execute(self, *_a, **_k):
            return _Result([(1,)])

    @asynccontextmanager
    async def _session_local():
        yield _Has()

    monkeypatch.setattr(maintenance, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(ge, "refresh_entity_stats", _refresh)
    await maintenance.ensure_entity_stats()
    assert built == []


@pytest.mark.asyncio
async def test_an_empty_table_is_built_once(monkeypatch):
    built = []

    async def _refresh(session, when):
        built.append(when)
        return {"entities": 3}

    class _Empty:
        async def execute(self, *_a, **_k):
            return _Result([])

    @asynccontextmanager
    async def _session_local():
        yield _Empty()

    monkeypatch.setattr(maintenance, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(ge, "refresh_entity_stats", _refresh)
    await maintenance.ensure_entity_stats()
    assert len(built) == 1


@pytest.mark.asyncio
async def test_a_failure_at_boot_does_not_stop_the_backend(monkeypatch):
    @asynccontextmanager
    async def _broken():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(maintenance, "AsyncSessionLocal", _broken)
    await maintenance.ensure_entity_stats()
