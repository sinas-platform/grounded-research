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

Every step is deterministic — no model calls, no spend — except the
extraction-retry step, which respawns the bulk pipeline over documents
whose extraction failed (capped per pass; the pipeline itself skips
anything already extracted, so a retry costs only the failed docs).
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
        WHERE rd.source_ref_type = 'document_class'
          AND rd.target_ref_type = 'entity_type'"""))).all()
    return [r[0] for r in rows]


async def _backfill_content_hashes(session) -> int:
    """sha256 for versions that predate the identity columns."""
    rows = (await session.execute(text("""
        SELECT id, content_md FROM document_version
        WHERE content_hash IS NULL AND content_md IS NOT NULL
        LIMIT 50000"""))).all()
    import hashlib

    for vid, content in rows:
        await session.execute(text(
            "UPDATE document_version SET content_hash = :h WHERE id = :i"),
            {"h": hashlib.sha256(content.encode()).hexdigest(), "i": vid})
    await session.commit()
    return len(rows)


async def _sweep_exact_duplicates(session) -> int:
    """Documents whose current content is byte-identical to an earlier
    document's: mark duplicate_of and stage them out of retrieval. Nothing
    is deleted — published evidence keeps its pinned versions, and the
    marking is reversible."""
    rows = (await session.execute(text("""
        WITH cur AS (
          SELECT d.id, d.created_at, dv.content_hash
          FROM document d JOIN document_version dv ON dv.id = d.current_version_id
          WHERE dv.content_hash IS NOT NULL AND d.duplicate_of_id IS NULL
        )
        SELECT a.id, b.id
        FROM cur a JOIN cur b ON a.content_hash = b.content_hash
          AND (b.created_at < a.created_at
               OR (b.created_at = a.created_at AND b.id < a.id))"""))).all()
    canonical: dict = {}
    for dup_id, earlier_id in rows:
        canonical.setdefault(dup_id, earlier_id)
    for dup_id, earlier_id in canonical.items():
        await session.execute(text("""
            UPDATE document SET duplicate_of_id = :c, staged = true
            WHERE id = :d"""), {"c": earlier_id, "d": dup_id})
    await session.commit()
    return len(canonical)


async def _purge_stale_annotations(session) -> int:
    """Annotation values whose subject entity was merged away — inert
    (nothing reads a merged entity), but debt that accumulates."""
    res = await session.execute(text("""
        DELETE FROM annotation_value av USING entity e
        WHERE e.id = av.subject_id AND e.merged_into_id IS NOT NULL"""))
    await session.commit()
    return res.rowcount or 0


# A pass never respawns more than this many docs: keeps the model spend of
# one tick bounded and visible even if something upstream mass-fails.
_RETRY_CAP = 200
# Leave freshly-registered docs to the pipeline their upload spawned; only
# docs this old with no extraction are considered failed rather than pending.
_RETRY_MIN_AGE_MINUTES = 60


async def _retry_failed_extractions(session) -> int:
    """Documents with content but no extraction — a failed or crashed
    extract stage (e.g. one malformed model reply in a 750-doc batch).
    Respawn the bulk pipeline over them; its worklist is derived from data,
    so the run is idempotent and only the failed docs cost anything."""
    rows = (await session.execute(text("""
        SELECT d.id
        FROM document d JOIN document_version dv ON dv.id = d.current_version_id
        WHERE COALESCE(TRIM(d.summary), '') = ''
          AND COALESCE(TRIM(dv.content_md), '') != ''
          AND d.staged = false
          AND d.duplicate_of_id IS NULL
          AND d.created_at < now() - make_interval(mins => :age_min)
        ORDER BY d.created_at
        LIMIT :cap"""), {"age_min": _RETRY_MIN_AGE_MINUTES,
                         "cap": _RETRY_CAP})).all()
    if not rows:
        return 0
    from app.api.v1.bulk import _spawn

    job_id = "maint-" + uuid.uuid4().hex[:8]
    _spawn(job_id, [str(r[0]) for r in rows], "extract,resolve,relationships")
    log.info("extraction retry: respawned %d docs as job %s", len(rows), job_id)
    return len(rows)


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
        stats["hashes_backfilled"] = await _backfill_content_hashes(session)
        stats["duplicates_marked"] = await _sweep_exact_duplicates(session)
        stats["stale_annotations_purged"] = await _purge_stale_annotations(session)
        stats["extraction_retries"] = await _retry_failed_extractions(session)
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
