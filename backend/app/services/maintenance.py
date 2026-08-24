"""Periodic corpus maintenance: the by-hand upkeep, made automatic.

Races and ordering during ingestion leave repairable debt behind: entity
references that could not resolve when first seen (an alias learned later
resolves them), documents whose case entity was never minted, materialized
annotations computed before the graph edges they walk existed, and
wall-of-text documents ingested before line-density normalization. Each of
these was fixed by a hand-run script at least once in Aug 2026 — a full
weekend of it around Gate 2. This module is that upkeep as one idempotent
pass, run on a timer by the backend (SGR_MAINTENANCE_INTERVAL_SECONDS,
0 disables) or by hand:

    python -m app.services.maintenance

Every step is deterministic — no model calls, no spend.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select, text

from app.db import AsyncSessionLocal

log = logging.getLogger("sgr.maintenance")

# Wall-of-text thresholds mirror the upload-time normalizer's intent: a
# document this large with this few lines cannot be quoted by line spans.
_WALL_MIN_CHARS = 20_000
_WALL_MAX_LINES = 30


async def _normalize_new_walls(session) -> int:
    """Normalize current versions that predate (or slipped past) the
    upload-time line-density normalization. New version, repointed head;
    published evidence keeps its own pinned version."""
    from app.services.toc import normalize_line_density

    rows = (await session.execute(text("""
        SELECT d.id, dv.version, dv.content_md
        FROM document d JOIN document_version dv ON dv.id = d.current_version_id
        WHERE length(dv.content_md) > :chars
          AND (length(dv.content_md) - length(replace(dv.content_md, E'\n', ''))) + 1 < :lines
    """), {"chars": _WALL_MIN_CHARS, "lines": _WALL_MAX_LINES})).all()
    changed = 0
    for doc_id, version, content in rows:
        fixed = normalize_line_density(content)
        if fixed == content:
            continue
        await session.execute(text("""
            INSERT INTO document_version (document_id, version, content_md, content_tsvector)
            VALUES (:d, :v, :c, to_tsvector('simple', :c))"""),
            {"d": doc_id, "v": version + 1, "c": fixed})
        await session.execute(text("""
            UPDATE document SET current_version_id =
              (SELECT id FROM document_version WHERE document_id = :d AND version = :v)
            WHERE id = :d"""), {"d": doc_id, "v": version + 1})
        changed += 1
    await session.commit()
    return changed


async def _full_text_entity_ids(session) -> list[uuid.UUID]:
    """Every entity a document currently stands for — the rematerialization
    subject set."""
    rows = (await session.execute(text("""
        SELECT DISTINCT r.target_id
        FROM relationship r
        JOIN relationship_definition rd ON rd.id = r.relationship_definition_id
        WHERE rd.name LIKE 'is_full_text_of%'"""))).all()
    return [r[0] for r in rows]


async def run_maintenance() -> dict[str, Any]:
    """One idempotent upkeep pass. Order matters: resolutions and minting
    change the graph that rematerialization then walks."""
    from app.services.entity_keys import shared_index
    from app.services.key_replay import (backfill_full_text_entities,
                                         rematerialize, replay_unresolved)

    stats: dict[str, Any] = {}
    async with AsyncSessionLocal() as session:
        ki = await shared_index(session)
        stats["replay"] = await replay_unresolved(session, key_index=ki)
        stats["full_text_backfill"] = await backfill_full_text_entities(
            session, key_index=ki)
        stats["walls_normalized"] = await _normalize_new_walls(session)
        subjects = await _full_text_entity_ids(session)
        stats["rematerialized_values"] = await rematerialize(session, subjects)
        stats["remat_subjects"] = len(subjects)
    log.info("maintenance pass: %s", stats)
    return stats


async def maintenance_loop(interval_seconds: int) -> None:
    """Backend-resident timer. First pass after one full interval — boot is
    not the moment to rescan the corpus. A failing pass logs and waits for
    the next tick; maintenance must never take the API down."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await run_maintenance()
        except Exception:  # noqa: BLE001 — upkeep never propagates
            log.exception("maintenance pass failed; retrying next interval")


if __name__ == "__main__":
    print(asyncio.run(run_maintenance()))
