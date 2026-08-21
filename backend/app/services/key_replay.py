"""Replay the unresolved-relationship queue against learned keys.

An unresolved row is a cite the extractor refused to drop: it knew the
definition, the source document, and what the document calls the target —
it just could not find the entity. Resolution has since learned to match
keys (entity_keys), so the queue can be replayed: each row whose key now
names exactly one live entity of the right type becomes a real
relationship, and the key becomes an alias so the next occurrence resolves
at ingestion instead of parking.

Replaying is idempotent and LLM-free. Rows that resolve are marked, rows
that do not stay queued for the next pass — later ingestions teach more
aliases, so the queue drains monotonically instead of refilling.

After the edges land, the materialized annotations of every touched entity
are recomputed, because the whole point of the edge is what it makes
derivable: a document with an is_full_text_of edge gets an issuing body and
an authority tier in the planning manifest; without one it shows nothing.

    python -m app.services.key_replay            # resolve + materialize
    python -m app.services.key_replay --dry-run  # count only
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import (
    Relationship,
    RelationshipDefinition,
    UnresolvedRelationship,
)
from app.services.entity_keys import KeyIndex, learn_aliases

_log = logging.getLogger("grove.key_replay")


async def replay_unresolved(
    session: AsyncSession, *, write: bool = True,
    key_index: KeyIndex | None = None,
) -> dict[str, Any]:
    """One pass over every unresolved row; returns what happened."""
    if key_index is None:
        key_index = await KeyIndex.load(session)

    defs = {d.id: d for d in (await session.execute(
        select(RelationshipDefinition))).scalars()}
    rows = list((await session.execute(
        select(UnresolvedRelationship)
        .where(UnresolvedRelationship.status == "unresolved")
    )).scalars())

    existing = set((await session.execute(
        select(Relationship.relationship_definition_id,
               Relationship.source_id, Relationship.target_id))).all())

    report = {"queued": len(rows), "resolved": 0, "duplicates": 0,
              "still_unresolved": 0, "aliases_learned": 0,
              "touched_entities": set()}
    for row in rows:
        d = defs.get(row.relationship_definition_id)
        target_type = (d.target_ref_id
                       if d is not None and d.target_ref_type == "entity_type"
                       else None)
        hit = key_index.resolve(row.target_key, target_type)
        if hit is None:
            report["still_unresolved"] += 1
            continue
        edge_key = (row.relationship_definition_id, row.source_id, hit)
        if edge_key in existing:
            # the edge exists already (a later document resolved it by
            # name); the row is settled either way, and the key is proven
            report["duplicates"] += 1
        else:
            existing.add(edge_key)
            report["resolved"] += 1
            if write:
                rel = Relationship(
                    relationship_definition_id=row.relationship_definition_id,
                    source_id=row.source_id,
                    target_id=hit,
                    evidence_document_id=row.evidence_document_id,
                    evidence_span=row.evidence_span,
                    confidence=row.confidence,
                    notes=f"resolved from queue by key '{row.target_key}'",
                )
                session.add(rel)
                await session.flush()
                row.resolved_relationship_id = rel.id
        if write:
            row.status = "resolved"
            row.resolved_at = datetime.now(timezone.utc)
            report["aliases_learned"] += await learn_aliases(
                session, hit, [row.target_key])
        key_index.learn(hit, row.target_key)
        report["touched_entities"].add(hit)

    if write:
        await session.commit()
    report["touched_entities"] = list(report["touched_entities"])
    return report


async def rematerialize(session: AsyncSession,
                        entity_ids: list[uuid.UUID]) -> int:
    """Recompute materialized annotations for these entities."""
    from app.services.annotations import materialize

    if not entity_ids:
        return 0
    written = 0
    for i in range(0, len(entity_ids), 500):
        written += await materialize(session, entity_ids[i:i + 500])
        await session.commit()
    return written


async def run(write: bool = True) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        report = await replay_unresolved(session, write=write)
        if write and report["touched_entities"]:
            report["annotations_written"] = await rematerialize(
                session, [uuid.UUID(str(e)) if not isinstance(e, uuid.UUID)
                          else e for e in report["touched_entities"]])
        report["touched_entities"] = len(report["touched_entities"])
    return report


if __name__ == "__main__":
    import argparse
    import asyncio
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = asyncio.run(run(write=not args.dry_run))
    print(json.dumps(out, indent=1, default=str))
