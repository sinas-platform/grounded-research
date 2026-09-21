"""The planner reads what the corpus holds; it does not compute it.

The entity half of the corpus map was a full-corpus aggregation on the hot
path of every question: count every mention of every entity, rank all of them
within their type, keep five names and an exact count per type. Six concurrent
runs each spent over sixteen minutes there, and the bulk ingestion writing to
the same database lost two thirds of its throughput to them.

What is checked here is the whole of that arrangement: the figures are round,
the read is a lookup, an unbuilt or stale profile costs the planner the sizes
and the examples and nothing more, and the refresh writes a row for every
declared type from what it sampled.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import corpus_profile as cp


# ─────────────────────────────────────────────────────────────
# Magnitudes
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n,expected", [
    (0, 0), (0.4, 0), (1, 1), (1.9, 1), (2, 2), (4, 2), (5, 5), (9, 5),
    (10, 10), (19, 10), (99, 50), (100, 100), (885_000, 500_000),
    (110_000, 100_000), (900_000, 500_000), (2_400_000, 2_000_000),
])
def test_the_figure_is_the_round_number_below_it(n, expected):
    assert cp.magnitude(n) == expected


def test_no_bucket_is_wider_than_a_factor_of_two_and_a_half():
    """The ladder's purpose: the planner judges how much a filter will cut, so
    the error that matters is the RATIO. A power-of-ten ladder puts 110,000 and
    900,000 in one bucket, which is a ninefold error where a ratio is the whole
    question."""
    for n in (1, 3, 7, 40, 999, 123_456, 7_654_321):
        assert cp.magnitude(n) <= n
        assert n / cp.magnitude(n) < 2.5
    assert cp.magnitude(110_000) != cp.magnitude(900_000)


def test_a_modest_load_does_not_move_the_figure():
    """Stability is the other half of rounding: the planner's view of the
    corpus must not churn because ten thousand entities arrived."""
    assert cp.magnitude(520_000) == cp.magnitude(530_000) == 500_000


def test_nothing_observed_is_said_as_such():
    assert cp.size_phrase(0) == "none observed"
    assert cp.size_phrase(500_000) == "~500,000"


# ─────────────────────────────────────────────────────────────
# The planner's block, in the three cases
# ─────────────────────────────────────────────────────────────
def _profile(**kw) -> dict[str, cp.TypeProfile]:
    return {n: cp.TypeProfile(n, m, tuple(e)) for n, (m, e) in kw.items()}


def test_a_populated_profile_gives_sizes_and_examples():
    block = cp.render_entity_types(
        ["Alpha", "Beta"],
        _profile(Alpha=(500_000, ["aa", "ab"]), Beta=(2_000, ["ba"])))
    assert block.splitlines() == [
        "ENTITY TYPES (name, approximate size, most-mentioned examples):",
        "- Alpha (~500,000): aa, ab",
        "- Beta (~2,000): ba",
    ]


def test_the_largest_type_is_named_first():
    block = cp.render_entity_types(
        ["Small", "Large"],
        _profile(Small=(100, ["s"]), Large=(50_000, ["l"])))
    assert block.index("- Large") < block.index("- Small")


def test_an_empty_profile_still_names_every_type():
    """The degradation that matters. Type NAMES come from the configuration
    table that declares them, not from the profile, so an unbuilt profile
    costs the sizes and the examples and leaves the planner able to name a
    probe against a type — which is what round 1 asks it for."""
    block = cp.render_entity_types(["Beta", "Alpha"], {})
    assert block.splitlines() == [
        "ENTITY TYPES (name) — sizes and examples unavailable, the corpus "
        "profile has not been built:",
        "- Alpha",
        "- Beta",
    ]
    assert "~" not in block


def test_a_type_the_refresh_never_saw_is_named_without_a_size():
    block = cp.render_entity_types(
        ["Known", "Fresh"], _profile(Known=(300, ["k"])))
    assert "- Known (~300): k" in block
    assert "- Fresh" in block
    assert block.index("- Known") < block.index("- Fresh")


# ─────────────────────────────────────────────────────────────
# The read is a lookup
# ─────────────────────────────────────────────────────────────
class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Answers the two reads `entity_type_block` makes, and records the SQL.

    Nothing here aggregates, and the assertions below check that nothing asked
    it to: the shape of the old query is what must never reappear.
    """

    def __init__(self, types, profile_rows):
        self.types = types
        self.profile_rows = profile_rows
        self.sql: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sql.append(sql)
        if "FROM corpus_profile" in sql:
            return _Rows(self.profile_rows)
        if "FROM entity_type" in sql:
            return _Rows([(n,) for n in self.types])
        raise AssertionError(f"unexpected read on the planning path: {sql}")

    async def commit(self):
        pass


def _row(name, mag, examples, age_s=0.0):
    return (name, mag, examples,
            datetime.now(timezone.utc) - timedelta(seconds=age_s))


@pytest.mark.asyncio
async def test_a_populated_cache_is_read_and_not_recomputed(caplog):
    s = _FakeSession(["Alpha", "Beta"],
                     [_row("Alpha", 500_000, ["aa", "ab"]),
                      _row("Beta", 2_000, ["ba"])])
    with caplog.at_level(logging.WARNING):
        block, problem = await cp.entity_type_block(s)
    assert "- Alpha (~500,000): aa, ab" in block
    assert "- Beta (~2,000): ba" in block
    assert problem is None
    assert caplog.records == []
    joined = " ".join(s.sql)
    for banned in ("entity_mention", "row_number", "count(*)", "array_agg"):
        assert banned not in joined, (
            f"{banned!r} on the planning path: the aggregation this replaced "
            "has come back")


@pytest.mark.asyncio
async def test_an_empty_cache_costs_the_examples_and_says_so_every_time(caplog):
    """Every time, as an error, and returned to the caller. Once per process
    at WARNING was not noticed for the life of a 127,000-document corpus."""
    s = _FakeSession(["Alpha", "Beta"], [])
    with caplog.at_level(logging.ERROR):
        first, p1 = await cp.entity_type_block(s)
        second, p2 = await cp.entity_type_block(s)
    assert first == second
    assert "- Alpha" in first and "- Beta" in first
    assert "~" not in first
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2, "said on every build of the map, not once"
    assert "empty" in errors[0].message
    assert p1 == p2 and "empty" in p1, "the problem is handed back, not only logged"


@pytest.mark.asyncio
async def test_a_stale_cache_is_discarded_rather_than_believed(caplog):
    from app.config import get_settings

    tolerance = get_settings().sgr_corpus_profile_max_age_seconds
    s = _FakeSession(["Alpha"],
                     [_row("Alpha", 500_000, ["aa"], age_s=tolerance + 60)])
    with caplog.at_level(logging.ERROR):
        block, problem = await cp.entity_type_block(s)
    assert "- Alpha" in block
    assert "500,000" not in block and "aa" not in block
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "older than" in errors[0].message
    assert problem and "older than" in problem


@pytest.mark.asyncio
async def test_a_profile_inside_the_tolerance_is_believed():
    from app.config import get_settings

    tolerance = get_settings().sgr_corpus_profile_max_age_seconds
    s = _FakeSession(["Alpha"],
                     [_row("Alpha", 500_000, ["aa"], age_s=tolerance - 60)])
    block, problem = await cp.entity_type_block(s)
    assert "~500,000" in block and problem is None


def test_the_map_asks_the_profile_for_its_entity_types():
    """The wiring: `_build_corpus_map_uncached` must go through the lookup."""
    import inspect

    from app import retrieval_first

    src = inspect.getsource(retrieval_first._build_corpus_map_uncached)
    assert "entity_type_block" in src
    assert "entity_mention" not in src, (
        "the map reached into the mention table again")


def test_a_plan_made_without_the_profile_says_so_on_the_run():
    """The wiring for being loud: the problem the block hands back travels
    into the plan's `warnings` and from there onto the run row's telemetry,
    where the API shows it. A log line alone went unread for the life of a
    corpus."""
    import inspect

    from app import retrieval_first
    from app.services import query_runner

    plan_src = inspect.getsource(retrieval_first.plan_question)
    assert '"warnings"' in plan_src and "map_problem" in plan_src
    run_src = inspect.getsource(query_runner)
    assert 'await _tele(run_id, "retrieval", warnings=' in run_src


# ─────────────────────────────────────────────────────────────
# The refresh
# ─────────────────────────────────────────────────────────────
def test_the_refresh_writes_a_row_for_every_declared_type():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    rows = cp.profile_rows(
        [a, b, c],
        sampled_counts={a: 5_000, b: 21},
        scale=100.0,                      # a 1% sample
        examples={a: ["aa", "ab", "ac", "ad", "ae", "af"], b: ["ba"]},
        now=now,
    )
    by_id = {r["tid"]: r for r in rows}
    assert set(by_id) == {str(a), str(b), str(c)}
    assert by_id[str(a)]["mag"] == 500_000          # 5,000 x 100, rounded down
    assert by_id[str(b)]["mag"] == 2_000            # 21 x 100 -> 2,100 -> 2,000
    assert by_id[str(c)]["mag"] == 0, (
        "a type the sample never saw still gets a row: no row would be "
        "indistinguishable from a type the refresh has not reached")
    assert json.loads(by_id[str(a)]["ex"]) == ["aa", "ab", "ac", "ad", "ae"], (
        "capped at EXAMPLES_PER_TYPE")
    assert json.loads(by_id[str(c)]["ex"]) == []
    assert all(r["now"] == now for r in rows)


def test_the_refresh_never_holds_the_database():
    """Three promises, read off the source because none of them can be
    observed without a database this test must not have: every statement runs
    under a timeout, the timeouts are LOCAL so they die with the transaction
    rather than following the connection into the pool, and each statement
    commits so no snapshot is held across them — an open snapshot is what
    stops vacuum keeping up with a bulk load."""
    import inspect

    src = inspect.getsource(cp._guarded)
    assert "statement_timeout" in src and "lock_timeout" in src
    assert "true" in src, "set_config's third argument is is_local"
    assert "commit" in src
    assert cp._STATEMENT_TIMEOUT_MS > 0 and cp._LOCK_TIMEOUT_MS > 0


def test_the_refresh_never_reads_the_mention_table():
    """The two reads that used to be the full scan, then a sample of it.
    Neither touches the mention table now: the counts are a grouped count
    over the entity table, exact, and the examples are read off
    `entity_stats`, which the maintenance pass computes before this."""
    for sql in (cp._COUNTS_SQL, cp._EXAMPLES_SQL):
        assert "entity_mention" not in sql
        assert "merged_into_id IS NULL" in sql
    assert "entity_stats" in cp._EXAMPLES_SQL


def test_the_examples_are_the_most_written_about_not_the_most_matched():
    """Measured 18 September 2026: ranked by mentions, the planner was told
    the collection's companies were "Case, Parties, Only, Lang, This" — the
    gazetteer's blind hits. Ranked by documents in which something actually
    recognised the entity, and never a marked generic term."""
    sql = cp._EXAMPLES_SQL
    assert "ORDER BY st.recognised_documents DESC" in sql
    assert "st.recognised_documents > 0" in sql
    assert "? 'generic_term'" in sql
    assert "e.canonical_form) AS rn" in sql, "ties break on the name"


@pytest.mark.asyncio
async def test_a_fresh_profile_is_not_resampled(monkeypatch):
    """The refresh keeps its own cadence, so a backend told to do upkeep every
    five minutes does not resample the corpus every five minutes."""
    s = _FakeSession(["Alpha"], [_row("Alpha", 100, ["a"], age_s=5)])

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return s

        async def __aexit__(self, *a):
            return False

    import app.db

    monkeypatch.setattr(app.db, "AsyncSessionLocal", _Maker())
    out = await cp.refresh_corpus_profile()
    assert out["skipped"] == "fresh"
    assert not any("entity_stats" in q or "corpus_profile" in q and "INSERT" in q
                   for q in s.sql), "a fresh profile is not recomputed"


def test_the_maintenance_pass_is_what_triggers_it():
    """The trigger path: the same timer every other piece of out-of-band
    upkeep in this backend hangs off."""
    import inspect

    from app.services import maintenance

    src = inspect.getsource(maintenance.run_maintenance)
    assert "refresh_corpus_profile" in src
    assert "corpus_profile" in src


def test_a_failed_refresh_does_not_lose_the_pass():
    import inspect

    from app.services import maintenance

    src = inspect.getsource(maintenance.run_maintenance)
    call = src.rindex("refresh_corpus_profile()")
    assert "try:" in src[:call], "the refresh must not be able to kill the pass"
    assert "except" in src[call:]


@pytest.mark.asyncio
async def test_the_profile_is_built_once_when_a_deployment_has_none(monkeypatch):
    """A profile that only a timer builds is a profile a restart prevents.

    The planner is shown each entity type's size and example values so it can
    propose probes against what the corpus holds. That table is computed once
    and stored — and the only thing that built it waited a full maintenance
    interval first. This deployment restarts more often than its six-hour
    interval, so the first pass never arrived: measured on 127,000 documents,
    zero rows, every question ever asked planned by a model told the NAMES of
    the entity types and nothing else. Building it took six minutes.

    Worse on a service that ships: every release restarts the clock.
    """
    from app.services import maintenance

    built = []

    async def _refresh(force=False):
        built.append(force)
        return {"types": 10}

    class _Empty:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(first=lambda: None)

    @asynccontextmanager
    async def _session_local():
        yield _Empty()

    monkeypatch.setattr(maintenance, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(
        "app.services.corpus_profile.refresh_corpus_profile", _refresh)
    await maintenance.ensure_corpus_profile()
    assert built == [True], "an empty profile must be built, and forced"


@pytest.mark.asyncio
async def test_a_deployment_that_has_one_is_left_alone(monkeypatch):
    """Missing is not stale. A boot never costs minutes resampling a corpus
    that already has a profile — refreshing is the timer's job."""
    from app.services import maintenance

    built = []

    async def _refresh(force=False):
        built.append(force)
        return {}

    class _Has:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(first=lambda: ("an-entity-type-id",))

    @asynccontextmanager
    async def _session_local():
        yield _Has()

    monkeypatch.setattr(maintenance, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(
        "app.services.corpus_profile.refresh_corpus_profile", _refresh)
    await maintenance.ensure_corpus_profile()
    assert built == []


@pytest.mark.asyncio
async def test_a_failure_to_build_does_not_stop_the_backend(monkeypatch):
    """Grounding is better, not required. A database that cannot answer at
    boot must not take the API down with it."""
    from app.services import maintenance

    @asynccontextmanager
    async def _broken():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(maintenance, "AsyncSessionLocal", _broken)
    await maintenance.ensure_corpus_profile()
