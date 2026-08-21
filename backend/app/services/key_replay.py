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

from sqlalchemy import select, text
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


async def backfill_full_text_entities(
    session: AsyncSession, *, write: bool = True,
    key_index: KeyIndex | None = None,
) -> dict[str, Any]:
    """Mint the case entity a document embodies, from the document itself.

    For an unresolved is_full_text_of* row the target is not some other
    entity the corpus may or may not mention — it is the case THIS document
    is the full text of. The document is the authority on what that case is:
    its extracted title names it, the parked key identifies it. So the
    entity can be created deterministically — no model call, no guessing —
    with the title as its name and the key as natural key and alias.

    Collisions are outcomes, not errors: if the title normalizes onto an
    entity that already exists, the edge links to that entity instead, which
    is exactly what resolution was trying to do all along.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import Entity
    from app.services.entity_resolver import normalize

    if key_index is None:
        key_index = await KeyIndex.load(session)

    rows = list((await session.execute(
        select(UnresolvedRelationship, RelationshipDefinition)
        .join(RelationshipDefinition,
              RelationshipDefinition.id ==
              UnresolvedRelationship.relationship_definition_id)
        .where(UnresolvedRelationship.status == "unresolved")
        .where(RelationshipDefinition.name.like("is_full_text_of%"))
    )).all())

    report = {"queued": len(rows), "created": 0, "linked_existing": 0,
              "no_title": 0, "resolved_by_key": 0,
              "touched_entities": set()}
    for row, d in rows:
        if d.target_ref_type != "entity_type":
            continue
        # a key an earlier creation in this same pass may already have taught
        hit = key_index.resolve(row.target_key, d.target_ref_id)
        if hit is None:
            title = (await session.execute(text("""
                SELECT coalesce(pv.value->>'_', pv.value #>> '{}')
                FROM property_value pv
                JOIN document_class_property p ON p.id = pv.property_id
                WHERE pv.document_id = :doc AND p.name = 'title' LIMIT 1"""),
                {"doc": row.evidence_document_id})).scalar_one_or_none()
            if not title or not title.strip():
                report["no_title"] += 1
                continue
            title = title.strip()[:500]
            from app.services.entity_keys import key_norm

            ent = Entity(entity_type_id=d.target_ref_id,
                         canonical_form=title,
                         natural_key=key_norm(row.target_key)[:300] or None,
                         normalized_form=normalize(title)[:500] or None)
            if write:
                try:
                    async with session.begin_nested():
                        session.add(ent)
                        await session.flush()
                    hit = ent.id
                    report["created"] += 1
                except IntegrityError:
                    # the title (or key) already names an entity — link it
                    hit = (await session.execute(
                        select(Entity.id)
                        .where(Entity.normalized_form == normalize(title)[:500])
                        .where(Entity.merged_into_id.is_(None))
                        .limit(1))).scalar_one_or_none()
                    if hit is None:
                        report["no_title"] += 1
                        continue
                    report["linked_existing"] += 1
            else:
                report["created"] += 1
                continue
            key_index.names.append((key_norm(title), hit, d.target_ref_id))
            key_index._blob = None
        else:
            report["resolved_by_key"] += 1
        if write and hit is not None:
            rel = Relationship(
                relationship_definition_id=row.relationship_definition_id,
                source_id=row.source_id, target_id=hit,
                evidence_document_id=row.evidence_document_id,
                evidence_span=row.evidence_span, confidence=row.confidence,
                notes=f"entity minted from own document for key '{row.target_key}'",
            )
            session.add(rel)
            await session.flush()
            row.status = "resolved"
            row.resolved_relationship_id = rel.id
            row.resolved_at = datetime.now(timezone.utc)
            await learn_aliases(session, hit, [row.target_key])
            key_index.learn(hit, row.target_key)
            report["touched_entities"].add(hit)
    if write:
        await session.commit()
    report["touched_entities"] = list(report["touched_entities"])
    return report


async def backfill_from_properties(
    session: AsyncSession, *,
    definitions: list[str],
    key_properties: list[str],
    name_property: str,
    write: bool = True,
) -> dict[str, Any]:
    """Link every source-class document to the entity it embodies, from its
    own extracted properties — for documents the extractor never proposed an
    edge for, so there is no queue row to replay.

    Domain knowledge arrives as parameters: the CALLER names the relationship
    definitions and which properties carry the key and the name. The platform
    only does what it always does — resolve a key, or mint the entity from
    the document's own statement of what it is. A legal deployment passes
    case_number/celex/title; a clinical one would pass its trial-id and
    study-title properties.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import Entity
    from app.services.entity_keys import key_norm
    from app.services.entity_resolver import normalize

    key_index = await KeyIndex.load(session)
    report = {"scanned": 0, "created": 0, "linked_existing": 0,
              "resolved_by_key": 0, "no_key_or_name": 0,
              "touched_entities": set()}

    for def_name in definitions:
        d = (await session.execute(
            select(RelationshipDefinition)
            .where(RelationshipDefinition.name == def_name))).scalar_one_or_none()
        if d is None or d.target_ref_type != "entity_type":
            continue
        rows = (await session.execute(text("""
            SELECT doc.id,
              (SELECT coalesce(pv.value->>'_', pv.value #>> '{}')
               FROM property_value pv
               JOIN document_class_property p ON p.id=pv.property_id
               WHERE pv.document_id=doc.id AND p.name=:name_prop LIMIT 1),
              ARRAY(SELECT coalesce(pv.value->>'_', pv.value #>> '{}')
               FROM property_value pv
               JOIN document_class_property p ON p.id=pv.property_id
               WHERE pv.document_id=doc.id AND p.name = ANY(:key_props))
            FROM document doc
            WHERE doc.document_class_id = :cls
              AND NOT EXISTS (
                SELECT 1 FROM relationship r WHERE r.source_id=doc.id
                  AND r.relationship_definition_id=:def_id)"""),
            {"name_prop": name_property, "key_props": key_properties,
             "cls": d.source_ref_id, "def_id": d.id})).all()

        for doc_id, title, keys in rows:
            report["scanned"] += 1
            keys = [k for k in (keys or []) if k and k.strip()]
            title = (title or "").strip()[:500]
            hit = None
            for k in keys:
                hit = key_index.resolve(k, d.target_ref_id)
                if hit is not None:
                    report["resolved_by_key"] += 1
                    break
            if hit is None:
                if not title and not keys:
                    report["no_key_or_name"] += 1
                    continue
                name = title or keys[0][:500]
                nk = key_norm(keys[0])[:300] if keys else None
                ent = Entity(entity_type_id=d.target_ref_id,
                             canonical_form=name,
                             natural_key=nk or None,
                             normalized_form=normalize(name)[:500] or None)
                if not write:
                    report["created"] += 1
                    continue
                try:
                    async with session.begin_nested():
                        session.add(ent)
                        await session.flush()
                    hit = ent.id
                    report["created"] += 1
                except IntegrityError:
                    hit = (await session.execute(
                        select(Entity.id)
                        .where(Entity.normalized_form == normalize(name)[:500])
                        .where(Entity.merged_into_id.is_(None))
                        .limit(1))).scalar_one_or_none()
                    if hit is None:
                        report["no_key_or_name"] += 1
                        continue
                    report["linked_existing"] += 1
                key_index.names.append((key_norm(name), hit, d.target_ref_id))
                key_index._blob = None
            if write and hit is not None:
                session.add(Relationship(
                    relationship_definition_id=d.id,
                    source_id=doc_id, target_id=hit,
                    evidence_document_id=doc_id,
                    notes="linked from own document properties",
                ))
                await learn_aliases(session, hit, keys)
                for k in keys:
                    key_index.learn(hit, k)
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
