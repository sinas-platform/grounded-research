"""What the corpus holds, in round numbers — computed out of band, read as a lookup.

The planner is grounded in a description of the corpus: what kinds of thing it
holds, roughly how many of each, and a few well-used examples of each kind so
the planner can see what a value of that kind looks like. That description was
COMPUTED ON EVERY RUN, from the two largest tables in the database:

    count every mention of every entity, rank all entities within their type
    by that count, keep five names per type and one exact count per type.

Six concurrent runs each spent over sixteen minutes there before planning
started, and the bulk ingestion writing to the same database lost two thirds
of its throughput to them. The work grows with the corpus, so the cost grows
every hour, and none of it is per-question: the same answer is computed for
every run until the next document lands.

So it is computed once, out of band, and stored. Three things follow.

AN ORDER OF MAGNITUDE, NOT A COUNT. What the count is FOR is judging how much
a filter will cut — a type with half a million members and a type with two
thousand call for different plans, and 512,004 rather than 500,000 changes
nothing about either. An exact count is also the expensive half of the query
and the half that can only be got by reading every row. So the stored figure
is the largest round number at or below the estimate, on the 1-2-5 ladder:
1, 2, 5, 10, 20, 50, 100 ... A bare power of ten was the other candidate and
is too coarse — it reports 900,000 and 110,000 as the same 100,000, a ninefold
error where the whole point is a ratio. The 1-2-5 ladder is never wrong by
more than a factor of 2.5 within a bucket, which is finer than any plan needs,
and it is visibly round, so nobody downstream mistakes it for a measurement.
It is also STABLE: a load of ten thousand new entities usually leaves every
stored figure untouched, so the planner's view of the corpus does not churn.

AN ESTIMATE, NOT AN AGGREGATE. The refresh reads a bounded sample of pages
from each table (`TABLESAMPLE SYSTEM`, a page budget rather than a row budget,
because pages are what that sampler picks and what the read actually costs)
and scales the result up. Sampling the MENTIONS to find the most-mentioned
entities is not a compromise but the natural method: an entity mentioned ten
thousand times is overwhelmingly likely to appear in any sample, and one
mentioned twice is not, which is precisely the ranking wanted. A table smaller
than the page budget is read whole, so a small deployment gets exact figures
and no sampling error at all.

IT MUST NEVER HOLD THE DATABASE. Every statement runs in its own transaction
with `statement_timeout` and `lock_timeout` set LOCAL to it, so no snapshot is
held open across statements (an open snapshot during a bulk load is what stops
vacuum, which is the real way a background reader hurts a writer). The reads
take ACCESS SHARE only and block no insert; the only write is to this module's
own table. A refresh that runs into its timeout dies and leaves the previous
profile in place — a stale profile is worth more than a wedged load.

Triggered from the periodic maintenance pass (services/maintenance), which is
how every other piece of out-of-band upkeep in this backend is triggered, or
by hand:

    python -m app.services.corpus_profile
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.config import get_settings

log = logging.getLogger("sgr.corpus_profile")

#: Heap pages read from each sampled table. 2,000 pages is ~16 MB, which is
#: some tens of milliseconds of buffered read and small enough that a
#: concurrent bulk load does not notice the cache pressure. A table smaller
#: than this is read whole.
SAMPLE_PAGE_BUDGET = 2_000
#: A fixed seed makes `TABLESAMPLE` pick the same pages from one refresh to
#: the next, so the example names change when the corpus changes and not
#: because the sampler rolled differently. The page set widens on its own as
#: the table grows.
_SAMPLE_SEED = 0.42
#: Example canonical forms kept per entity type.
EXAMPLES_PER_TYPE = 5
#: Per-statement ceilings, LOCAL to each statement's own transaction.
_STATEMENT_TIMEOUT_MS = 30_000
_LOCK_TIMEOUT_MS = 5_000

_PAGE_BYTES = 8192


# ─────────────────────────────────────────────────────────────
# Magnitudes
# ─────────────────────────────────────────────────────────────
def magnitude(n: float) -> int:
    """The largest 1-2-5 round number at or below `n`; 0 for nothing.

    Pure. See the module docstring for why this ladder and not powers of ten.
    """
    if n < 1:
        return 0
    step = 1
    while step * 10 <= n:
        step *= 10
    for mult in (5, 2, 1):
        if step * mult <= n:
            return step * mult
    return step  # unreachable: step <= n held on entry to the loop


def size_phrase(mag: int) -> str:
    """How a stored magnitude is said to the planner."""
    return "none observed" if mag <= 0 else f"~{mag:,}"


# ─────────────────────────────────────────────────────────────
# Reading — a lookup, never a computation
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TypeProfile:
    name: str
    magnitude: int
    examples: tuple[str, ...]


def render_entity_types(
    type_names: list[str], profile: dict[str, TypeProfile]
) -> str:
    """The ENTITY TYPES block of the planner's corpus map.

    Pure, and the whole degradation story is here. THE TYPE NAMES DO NOT COME
    FROM THE PROFILE: they come from the configuration table that declares
    them, which is a few dozen rows and free to read. So an unbuilt or stale
    profile costs the planner the sizes and the examples and nothing else — it
    still knows what kinds of thing the corpus holds, which is what the round-1
    prompt asks it to name a probe against. Losing the type list as well would
    have made an unbuilt profile unable to plan at all.

    Profiled types sort by magnitude descending, the order the planner saw
    before this existed; unprofiled ones sort last, by name.
    """
    header = "ENTITY TYPES (name, approximate size, most-mentioned examples):"
    if not profile:
        return "\n".join(
            ["ENTITY TYPES (name) — sizes and examples unavailable, the corpus "
             "profile has not been built:"]
            + [f"- {n}" for n in sorted(type_names)]
        )
    ranked = sorted(
        type_names,
        key=lambda n: (-(profile[n].magnitude if n in profile else -1), n),
    )
    lines = [header]
    for name in ranked:
        p = profile.get(name)
        if p is None:
            lines.append(f"- {name}")
            continue
        ex = ", ".join(p.examples)
        lines.append(f"- {name} ({size_phrase(p.magnitude)})"
                     + (f": {ex}" if ex else ""))
    return "\n".join(lines)


async def read_profile(session) -> tuple[dict[str, TypeProfile], datetime | None]:
    """The stored profile keyed by type name, and when it was computed.

    Two small reads and no aggregation of anything. Staleness is judged on the
    profile AS A WHOLE, from the newest row, because one refresh writes every
    row in one transaction: a single old row means the type was dropped from
    the sample, not that the snapshot is old.
    """
    rows = (await session.execute(text("""
        SELECT t.name, p.entity_count_magnitude, p.examples, p.refreshed_at
        FROM corpus_profile p JOIN entity_type t ON t.id = p.entity_type_id
    """))).all()
    if not rows:
        return {}, None
    newest = max(r[3] for r in rows)
    out = {
        str(name): TypeProfile(
            name=str(name),
            magnitude=int(mag or 0),
            examples=tuple(str(x) for x in (ex or []) if x),
        )
        for name, mag, ex, _ in rows
    }
    return out, newest


async def entity_type_block(session) -> tuple[str, str | None]:
    """The planner's view of the corpus's entity types, and what is wrong
    with it. A lookup.

    Never computes the profile. An absent one, or one older than the
    deployment's tolerance, yields the type names alone — see
    `render_entity_types` — and the second value says so, in a sentence the
    caller can put in front of someone.

    Said every time, as an error. This used to be one warning per process,
    on the theory that a planner running every few minutes against an
    unbuilt profile would bury the log. What happened instead: the profile
    was empty for the life of a 127,000-document corpus, the one warning
    scrolled past at boot, and every question anyone asked was planned by a
    model told the names of the entity types and nothing else. A planner
    working without its grounding is an error condition on every question it
    plans, and this is called once per corpus-map build, which is cached, so
    the volume is bounded by the map's TTL and not by the question rate.
    """
    names = [str(r[0]) for r in (await session.execute(
        text("SELECT name FROM entity_type"))).all()]
    profile, refreshed_at = await read_profile(session)
    max_age = get_settings().sgr_corpus_profile_max_age_seconds
    problem: str | None = None
    if not profile:
        problem = ("corpus profile is empty: the planner gets entity type "
                   "names with no sizes and no examples. Build it with "
                   "`python -m app.services.corpus_profile`, or let the "
                   "maintenance pass do it.")
    elif max_age > 0 and refreshed_at is not None and _age_s(refreshed_at) > max_age:
        problem = (f"corpus profile last refreshed {refreshed_at.isoformat()}, "
                   f"older than the {max_age}s tolerance: discarded, so the "
                   "planner gets entity type names with no sizes and no "
                   "examples rather than a stale corpus.")
        profile = {}
    if problem:
        log.error(problem)
    return render_entity_types(names, profile), problem


def _age_s(when: datetime) -> float:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


# ─────────────────────────────────────────────────────────────
# Refreshing — out of band, bounded, gentle
# ─────────────────────────────────────────────────────────────
def sample_percent(pages: int, budget: int = SAMPLE_PAGE_BUDGET) -> float:
    """The `TABLESAMPLE SYSTEM` fraction that reads about `budget` pages.

    Pure. A table at or under the budget is read whole (100), so a small
    corpus is counted exactly and carries no sampling error. Floored at 0.01
    because that is the smallest fraction the sampler accepts.
    """
    if pages <= 0 or pages <= budget:
        return 100.0
    return max(0.01, min(100.0, round(budget / pages * 100.0, 4)))


async def _guarded(session, sql: str, params: dict | None = None) -> list:
    """One statement, in its own transaction, under a timeout.

    The timeouts are LOCAL, so they die with the transaction and never follow
    the connection back into the pool. Committing after each statement is the
    point of the helper: it ends the snapshot, and a background reader holding
    a snapshot open is what stops vacuum from keeping up with a bulk load.
    """
    await session.execute(
        text("SELECT set_config('statement_timeout', :v, true)"),
        {"v": str(_STATEMENT_TIMEOUT_MS)})
    await session.execute(
        text("SELECT set_config('lock_timeout', :v, true)"),
        {"v": str(_LOCK_TIMEOUT_MS)})
    rows = (await session.execute(text(sql), params or {})).all()
    await session.commit()
    return rows


async def _pages(session, table: str) -> int:
    """Current heap size of `table` in pages.

    From the file size, not from `reltuples`: the file size is exact and free
    and owes nothing to when ANALYZE last ran, which on a table being bulk
    loaded is a question with no good answer.
    """
    rows = await _guarded(
        session, "SELECT pg_relation_size(to_regclass(:t))", {"t": table})
    size = rows[0][0] if rows and rows[0][0] is not None else 0
    return int(size) // _PAGE_BYTES


_COUNTS_SQL = """
SELECT entity_type_id, count(*)::bigint
FROM entity TABLESAMPLE SYSTEM (CAST(:pct AS float8))
     REPEATABLE (CAST(:seed AS float8))
WHERE merged_into_id IS NULL
GROUP BY entity_type_id
"""

# The most-mentioned entities of each type, from a sample of the mentions.
# `sampled` collapses the sampled mentions to one row per entity before
# anything is joined or sorted, so the window below orders tens of thousands
# of rows rather than tens of millions. Ties break on the name so that two
# equally-mentioned entities do not swap places between refreshes.
_EXAMPLES_SQL = """
WITH sampled AS (
  SELECT entity_id, count(*) AS n
  FROM entity_mention TABLESAMPLE SYSTEM (CAST(:pct AS float8))
       REPEATABLE (CAST(:seed AS float8))
  WHERE entity_id IS NOT NULL AND status = 'active'
  GROUP BY entity_id
), ranked AS (
  SELECT e.entity_type_id AS tid, e.canonical_form AS form,
         row_number() OVER (PARTITION BY e.entity_type_id
                            ORDER BY s.n DESC, e.canonical_form) AS rn
  FROM sampled s JOIN entity e ON e.id = s.entity_id
  WHERE e.merged_into_id IS NULL
)
SELECT tid, form FROM ranked WHERE rn <= :k ORDER BY tid, rn
"""

_UPSERT_SQL = """
INSERT INTO corpus_profile
  (entity_type_id, entity_count_magnitude, examples, refreshed_at)
VALUES (CAST(:tid AS uuid), :mag, CAST(:ex AS jsonb), :now)
ON CONFLICT (entity_type_id) DO UPDATE SET
  entity_count_magnitude = EXCLUDED.entity_count_magnitude,
  examples = EXCLUDED.examples,
  refreshed_at = EXCLUDED.refreshed_at
"""


def profile_rows(
    type_ids: list[uuid.UUID],
    sampled_counts: dict[uuid.UUID, int],
    scale: float,
    examples: dict[uuid.UUID, list[str]],
    now: datetime,
) -> list[dict]:
    """The rows a refresh writes, from what it sampled. Pure.

    Every declared type gets a row, including one the sample never saw: a type
    with no row is indistinguishable from a type the refresh has not reached
    yet, and `magnitude` 0 says "none observed" — which is what a sample that
    missed it means and what an exhaustive read that missed it means too.
    """
    out = []
    for tid in type_ids:
        est = sampled_counts.get(tid, 0) * scale
        out.append({
            "tid": str(tid),
            "mag": magnitude(est),
            "ex": json.dumps(examples.get(tid, [])[:EXAMPLES_PER_TYPE]),
            "now": now,
        })
    return out


async def refresh_corpus_profile(force: bool = False) -> dict[str, Any]:
    """Recompute the stored profile. Safe to run beside a bulk load.

    Skips when the stored profile is younger than the deployment's interval,
    so the maintenance cadence and this one are independent: a backend told to
    do upkeep every five minutes does not resample the corpus every five
    minutes. `force` overrides that, and is what the CLI passes.
    """
    from app.db import AsyncSessionLocal

    settings = get_settings()
    stats: dict[str, Any] = {}
    async with AsyncSessionLocal() as session:
        if not force and settings.sgr_corpus_profile_interval_seconds > 0:
            _, refreshed_at = await read_profile(session)
            if (refreshed_at is not None
                    and _age_s(refreshed_at)
                    < settings.sgr_corpus_profile_interval_seconds):
                return {"skipped": "fresh", "refreshed_at": refreshed_at}

        types = await _guarded(session, "SELECT id, name FROM entity_type")
        type_ids = [r[0] for r in types]
        if not type_ids:
            return {"skipped": "no entity types"}

        entity_pct = sample_percent(await _pages(session, "entity"))
        mention_pct = sample_percent(await _pages(session, "entity_mention"))
        stats["entity_sample_pct"] = entity_pct
        stats["mention_sample_pct"] = mention_pct

        counted = await _guarded(session, _COUNTS_SQL,
                                 {"pct": entity_pct, "seed": _SAMPLE_SEED})
        sampled_counts = {r[0]: int(r[1]) for r in counted}
        exampled = await _guarded(
            session, _EXAMPLES_SQL,
            {"pct": mention_pct, "seed": _SAMPLE_SEED, "k": EXAMPLES_PER_TYPE})
        examples: dict[uuid.UUID, list[str]] = {}
        for tid, form in exampled:
            examples.setdefault(tid, []).append(str(form))

        rows = profile_rows(type_ids, sampled_counts, 100.0 / entity_pct,
                            examples, datetime.now(timezone.utc))
        await _guarded_write(session, rows)

    stats["types"] = len(rows)
    stats["types_with_examples"] = sum(1 for r in rows if r["ex"] != "[]")
    log.info("corpus profile refreshed: %s", stats)
    _invalidate_reader_cache()
    return stats


async def _guarded_write(session, rows: list[dict]) -> None:
    """The upsert, in one guarded transaction. Touches only this table, so it
    cannot block anything the ingestion is writing."""
    if not rows:
        return
    await session.execute(
        text("SELECT set_config('statement_timeout', :v, true)"),
        {"v": str(_STATEMENT_TIMEOUT_MS)})
    await session.execute(
        text("SELECT set_config('lock_timeout', :v, true)"),
        {"v": str(_LOCK_TIMEOUT_MS)})
    await session.execute(text(_UPSERT_SQL), rows)
    await session.commit()


def _invalidate_reader_cache() -> None:
    """Drop the in-process corpus map so a refresh in this process shows up at
    once. Best effort: the maintenance pass may be a separate process, where
    there is nothing to drop and the map's own TTL is the bound."""
    try:
        from app.retrieval_first import invalidate_corpus_map

        invalidate_corpus_map()
    except Exception:  # noqa: BLE001 — a cache that will expire anyway
        log.debug("could not drop the in-process corpus map", exc_info=True)


if __name__ == "__main__":
    print(asyncio.run(refresh_corpus_profile(force=True)))
