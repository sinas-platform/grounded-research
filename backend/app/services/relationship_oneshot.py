"""One-shot relationship extraction: one tool-less LLM call per document.

The retired agentic relationship stage read the document through tool
calls — get_document, read_document_content, find_entity_by_name per edge
end, one record call per edge. Measured on the cost sampling runs that
was ~12 model calls and ~$0.11 per document; the tool-loop fan-out, not
the extraction itself, was where the money went.

This module is its replacement, using the one-shot pattern already used
for front matter and grounding:

  1. ONE tool-less completion (CHEAP_LLM, temp 0, forced JSON) gets the
     document text, the relationship definitions with their guidance, and
     the document's active entity mentions. It returns edges by NAME.
  2. The SERVER does everything the agent used to do with tools:
     name → entity id via the document's resolved mentions, end-type
     validation against the definition, dedupe against existing rows, and
     the same routing the API applies — creation_mode "review" or low
     confidence → RelationshipProposal, unmapped-but-cited target →
     UnresolvedRelationship (the cite is never dropped), otherwise →
     Relationship.

Runs after grounding + resolution in the one-shot pipeline, so mentions
carry entity ids and rejected names are already invisible. Only extracts
what the document states; discovery stays with the discovery agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import Document, DocumentClass, Entity, EntityMention
from app.models.config import EntityType, RelationshipDefinition
from app.models.runtime import (
    DocumentVersion,
    Relationship,
    RelationshipProposal,
    UnresolvedRelationship,
)
from app.services.ingestion_oneshot import _clip, _parse_json_reply
from app.services.query_runner import _Sinas

log = logging.getLogger(__name__)

RELATIONSHIP_AGENT = "sgr/relationship-oneshot-agent"
PROPOSAL_THRESHOLD = 0.8  # below this an open-mode edge becomes a proposal
_MAX_MENTION_NAMES = 150
_SOURCE_DOCUMENT = "DOCUMENT"
# DB phases of the wide-concurrent batch path run through this gate:
# thousands of parked wave members are fine, but only a bounded number
# of tasks may hold connections / multiplex multi-query transactions at
# once (15 Aug: 876/1120 lost to pool starvation at 1120-wide prep).
_PREP_GATE = asyncio.Semaphore(32)

# Long documents are covered per chunk, like NER (citations sit deep in
# court decisions, not in the head). Windows are double the NER chunk:
# relationship statements are sparse, so fewer, larger calls win.
_CHUNK_CHARS = 60_000
_CHUNK_OVERLAP = 2_000


def _chunks(content: str) -> list[str]:
    if len(content) <= _CHUNK_CHARS + _CHUNK_OVERLAP:
        return [content]
    out = []
    start = 0
    while start < len(content):
        out.append(content[start : start + _CHUNK_CHARS])
        start += _CHUNK_CHARS - _CHUNK_OVERLAP
    return out


# ── definition loading ─────────────────────────────────────────────────────


async def load_definitions(session: AsyncSession) -> list[dict[str, Any]]:
    """Relationship definitions with their end-type NAMES resolved, closed
    definitions excluded (the API refuses runtime writes for those)."""
    defs = (
        (await session.execute(select(RelationshipDefinition))).scalars().all()
    )
    class_names = {
        c.id: c.name
        for c in (await session.execute(select(DocumentClass))).scalars()
    }
    type_names = {
        t.id: t.name for t in (await session.execute(select(EntityType))).scalars()
    }

    def _end(ref_type: str, ref_id: uuid.UUID) -> str:
        if ref_type == "document_class":
            return f"document({class_names.get(ref_id, '?')})"
        return f"entity({type_names.get(ref_id, '?')})"

    out = []
    for d in defs:
        if d.creation_mode == "closed":
            continue
        out.append(
            {
                "id": d.id,
                "name": d.name,
                "creation_mode": d.creation_mode,
                "source_ref_type": d.source_ref_type,
                "source_ref_id": d.source_ref_id,
                "target_ref_type": d.target_ref_type,
                "target_ref_id": d.target_ref_id,
                "source_desc": _end(d.source_ref_type, d.source_ref_id),
                "target_desc": _end(d.target_ref_type, d.target_ref_id),
                "guidance": (d.extraction_guidance or d.description or "").strip(),
            }
        )
    return out


# ── prompt ─────────────────────────────────────────────────────────────────

_PROMPT = """Extract the relationships this document explicitly states. Reply with ONLY a JSON object, no prose.

RELATIONSHIP DEFINITIONS (use exactly these names; source → target types are binding):
{definitions}

ENTITIES recorded for this document (use these exact names for edge ends):
{mentions}

Reply JSON schema:
{{
  "relationships": [
    {{
      "definition": "<definition name>",
      "source": "DOCUMENT" | "<entity name from the list>",
      "target": "DOCUMENT" | "<entity name from the list>",
      "quote": "<verbatim supporting text, max 200 chars>",
      "confidence": <0..1>,
      "target_reference": "<only when the target is a cited case/instrument NOT in the entity list: the raw reference, e.g. an ECLI or case number>"
    }}
  ]
}}

Rules:
- Only relationships the text EXPLICITLY states. No inference, no world knowledge.
- "DOCUMENT" means this document itself; use it only where the definition's
  source or target is a document type. For definitions BETWEEN entities,
  the document's own case is its case ENTITY from the list — use that
  name, and also record the document→case edge where a definition offers
  it.
- Edge ends must be COPIED CHARACTER-FOR-CHARACTER from the entity list —
  never retype, shorten or embellish a name. The one exception is a cited
  target missing from the list: keep the edge and put the raw reference in
  target_reference.
- Every edge needs a verbatim quote. No duplicates.

FILENAME: {filename}
DOCUMENT:
{content}"""


def build_prompt(
    *,
    filename: str,
    content: str,
    definitions: list[dict[str, Any]],
    mention_names: list[str],
) -> str:
    def_lines = "\n".join(
        f"- {d['name']}: {d['source_desc']} → {d['target_desc']}."
        + (f" {d['guidance']}" if d["guidance"] else "")
        for d in definitions
    )
    names = sorted(set(mention_names))[:_MAX_MENTION_NAMES]
    return _PROMPT.format(
        definitions=def_lines,
        mentions="\n".join(f"- {n}" for n in names) or "(none)",
        filename=filename,
        content=_clip(content),
    )


# ── mapping + writing ──────────────────────────────────────────────────────


def _normalize_name(name: str) -> str:
    """Fold the differences models introduce when echoing names: case,
    dash variants, collapsed whitespace, trailing punctuation."""
    n = (name or "").lower().replace("–", "-").replace("—", "-")
    n = " ".join(n.split())
    return n.strip(" .,;:")


class _NameIndex:
    """Name → entity id with the same tolerance the agentic path had via
    find_entity_by_name (partial, case-insensitive): exact, normalized,
    then unique containment either way."""

    _MIN_CONTAINMENT = 8  # short names ("Article 1") must match exactly

    def __init__(self, name_to_entity: dict[str, uuid.UUID]) -> None:
        self.exact = dict(name_to_entity)
        self.norm: dict[str, uuid.UUID] = {}
        for k, v in name_to_entity.items():
            self.norm.setdefault(_normalize_name(k), v)

    def resolve(self, raw: str) -> uuid.UUID | None:
        name = (raw or "").strip().lower()
        if not name:
            return None
        hit = self.exact.get(name)
        if hit is not None:
            return hit
        norm = _normalize_name(raw)
        hit = self.norm.get(norm)
        if hit is not None:
            return hit
        if len(norm) < self._MIN_CONTAINMENT:
            return None
        contained = {
            v for k, v in self.norm.items()
            if len(k) >= self._MIN_CONTAINMENT and (norm in k or k in norm)
        }
        if len(contained) == 1:
            return contained.pop()
        return None

    def knows(self, raw: str) -> bool:
        return self.resolve(raw) is not None


def _find_span(content: str, quote: str) -> dict:
    span: dict[str, Any] = {"quote": (quote or "")[:300], "method": "oneshot"}
    q = (quote or "").strip()
    if q:
        pos = content.find(q)
        if pos >= 0:
            span["char_start"] = pos
            span["char_end"] = pos + len(q)
    return span


async def _apply_edges(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    content: str,
    edges: list[dict],
    definitions: list[dict[str, Any]],
    name_to_entity: dict[str, uuid.UUID],
    entity_types: dict[uuid.UUID, uuid.UUID],
    doc_class_id: uuid.UUID | None,
    write: bool,
    key_index=None,
) -> dict[str, int]:
    """Deterministic write path — mirrors the routing of the ingestion API:
    open+confident → Relationship, review or low confidence → proposal,
    named-but-unmapped target → UnresolvedRelationship."""
    by_name = {d["name"]: d for d in definitions}
    report = {
        "relationships": 0,
        "proposals": 0,
        "unresolved": 0,
        "resolved_by_key": 0,
        "aliases_learned": 0,
        "skipped_unknown_definition": 0,
        "skipped_unmapped": 0,
        "skipped_type_mismatch": 0,
        "skipped_duplicate": 0,
    }

    existing = {
        (r.relationship_definition_id, r.source_id, r.target_id)
        for r in (
            await session.execute(
                select(Relationship).where(
                    Relationship.evidence_document_id == document_id
                )
            )
        ).scalars().all()
    }
    existing_unresolved = {
        (u.relationship_definition_id, u.source_id, u.target_key)
        for u in (
            await session.execute(
                select(UnresolvedRelationship).where(
                    UnresolvedRelationship.evidence_document_id == document_id,
                    UnresolvedRelationship.status == "unresolved",
                )
            )
        ).scalars().all()
    }

    index = _NameIndex(name_to_entity)

    # The document's own case entity, per entity type. Models write
    # "DOCUMENT" as an end even where the definition wants the case
    # ENTITY the document is the full text of — semantically correct,
    # and the reply itself declares the mapping through its
    # document→entity edges (is_full_text_of). Pre-scan those, then
    # substitute on entity-typed ends.
    self_entity: dict[uuid.UUID, uuid.UUID] = {}
    for e in edges if isinstance(edges, list) else []:
        if not isinstance(e, dict):
            continue
        d = by_name.get(str(e.get("definition") or "").strip())
        if (
            d is None
            or d["source_ref_type"] != "document_class"
            or d["target_ref_type"] != "entity_type"
            or str(e.get("source") or "").strip().upper() != _SOURCE_DOCUMENT
        ):
            continue
        eid = index.resolve(str(e.get("target") or ""))
        if eid is not None and entity_types.get(eid) == d["target_ref_id"]:
            self_entity.setdefault(d["target_ref_id"], eid)

    def _resolve_end(
        raw: str, ref_type: str, ref_id: uuid.UUID
    ) -> uuid.UUID | None:
        """An edge end: the document itself, or a mentioned entity whose
        type matches the definition's declared end type."""
        name = (raw or "").strip()
        if ref_type == "document_class":
            if name.upper() == _SOURCE_DOCUMENT:
                if doc_class_id is not None and doc_class_id != ref_id:
                    return None
                return document_id
            return None
        if name.upper() == _SOURCE_DOCUMENT:
            return self_entity.get(ref_id)
        eid = index.resolve(name)
        if eid is None:
            return None
        etype = entity_types.get(eid)
        if etype is not None and etype != ref_id:
            return None
        return eid

    for e in edges if isinstance(edges, list) else []:
        if not isinstance(e, dict):
            continue
        d = by_name.get(str(e.get("definition") or "").strip())
        if d is None:
            report["skipped_unknown_definition"] += 1
            continue
        confidence = float(e.get("confidence") or 0.7)
        quote = str(e.get("quote") or "")
        source_id = _resolve_end(
            str(e.get("source") or ""), d["source_ref_type"], d["source_ref_id"]
        )
        target_id = _resolve_end(
            str(e.get("target") or ""), d["target_ref_type"], d["target_ref_id"]
        )

        if source_id is None:
            # without a resolvable source there is nothing to anchor the
            # edge to — a raw-reference fallback only exists for targets
            raw = str(e.get("source") or "")
            report[
                "skipped_type_mismatch"
                if raw.upper() == _SOURCE_DOCUMENT or index.knows(raw)
                else "skipped_unmapped"
            ] += 1
            continue

        if target_id is not None:
            # The extractor supplied both a resolvable name and a reference.
            # That pairing is proof: the reference names this entity. Learned
            # here, the same reference resolves directly next time — this is
            # what keeps the unresolved queue from refilling on the next
            # 70k-document ingestion.
            reference = str(e.get("target_reference") or "").strip()
            if reference and write:
                from app.services.entity_keys import learn_aliases

                report["aliases_learned"] += await learn_aliases(
                    session, target_id, [reference])
                if key_index is not None:
                    key_index.learn(target_id, reference)

        if target_id is None:
            raw_target = str(e.get("target") or "").strip()
            reference = str(e.get("target_reference") or "").strip() or raw_target
            # A name the gazetteer does not know may still be a key an
            # earlier resolution taught us.
            if key_index is not None and reference:
                d_target_type = (d["target_ref_id"]
                                 if d["target_ref_type"] == "entity_type" else None)
                hit = key_index.resolve(reference, d_target_type)
                if hit is None and raw_target and raw_target != reference:
                    hit = key_index.resolve(raw_target, d_target_type)
                if hit is not None:
                    target_id = hit
                    report["resolved_by_key"] += 1
                    if write:
                        from app.services.entity_keys import learn_aliases

                        report["aliases_learned"] += await learn_aliases(
                            session, hit, [reference, raw_target])

        if target_id is None:
            raw_target = str(e.get("target") or "").strip()
            reference = str(e.get("target_reference") or "").strip() or raw_target
            if index.knows(raw_target) or not reference:
                # mapped name of the wrong type, or nothing to key on
                report[
                    "skipped_type_mismatch" if index.knows(raw_target)
                    else "skipped_unmapped"
                ] += 1
                continue
            key = (d["id"], source_id, reference[:500])
            if key in existing_unresolved:
                report["skipped_duplicate"] += 1
                continue
            existing_unresolved.add(key)
            if write:
                session.add(
                    UnresolvedRelationship(
                        relationship_definition_id=d["id"],
                        source_id=source_id,
                        target_key=reference[:500],
                        target_key_kind="reference",
                        proposing_agent="relationship-oneshot",
                        reasoning=quote[:500] or None,
                        evidence_document_id=document_id,
                        evidence_span=_find_span(content, quote),
                        confidence=confidence,
                        status="unresolved",
                    )
                )
            report["unresolved"] += 1
            continue

        if (d["id"], source_id, target_id) in existing:
            report["skipped_duplicate"] += 1
            continue
        existing.add((d["id"], source_id, target_id))

        if d["creation_mode"] == "review" or confidence < PROPOSAL_THRESHOLD:
            if write:
                session.add(
                    RelationshipProposal(
                        relationship_definition_id=d["id"],
                        source_id=source_id,
                        target_id=target_id,
                        proposing_agent="relationship-oneshot",
                        reasoning=quote[:500] or None,
                        evidence_document_id=document_id,
                        evidence_span=_find_span(content, quote),
                        confidence=confidence,
                        status="pending",
                    )
                )
            report["proposals"] += 1
        else:
            if write:
                session.add(
                    Relationship(
                        relationship_definition_id=d["id"],
                        source_id=source_id,
                        target_id=target_id,
                        evidence_document_id=document_id,
                        evidence_span=_find_span(content, quote),
                        confidence=confidence,
                    )
                )
            report["relationships"] += 1
    return report


# ── per-document driver ────────────────────────────────────────────────────


async def extract_document(
    session: AsyncSession,
    sinas: _Sinas,
    document_id: uuid.UUID,
    *,
    definitions: list[dict[str, Any]] | None = None,
    write: bool = True,
    key_index=None,
) -> dict[str, Any]:
    async with _PREP_GATE:
        return await _extract_document_gated(
            session, sinas, document_id,
            definitions=definitions, write=write, key_index=key_index,
        )


async def _extract_document_gated(
    session: AsyncSession,
    sinas: _Sinas,
    document_id: uuid.UUID,
    *,
    definitions: list[dict[str, Any]] | None = None,
    write: bool = True,
    key_index=None,
) -> dict[str, Any]:
    doc = await session.get(Document, document_id)
    report: dict[str, Any] = {
        "document": doc.filename if doc else str(document_id),
        "llm_calls": 0,
    }
    if doc is None:
        report["skipped"] = "document not found"
        return report

    version = (
        await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version.desc())
            .limit(1)
        )
    ).scalars().first()
    content = (version.content_md or "") if version else ""
    if not content.strip():
        report["skipped"] = "no extracted content"
        return report

    if definitions is None:
        definitions = await load_definitions(session)
    if not definitions:
        report["skipped"] = "no relationship definitions"
        return report

    mentions = (
        await session.execute(
            select(EntityMention)
            .where(EntityMention.document_id == document_id)
            .where(EntityMention.status == "active")
            .where(EntityMention.entity_id.is_not(None))
        )
    ).scalars().all()
    linked_ids = {m.entity_id for m in mentions}
    entities = {}
    if linked_ids:
        entities = {
            e.id: e
            for e in (
                await session.execute(
                    select(Entity).where(Entity.id.in_(linked_ids))
                )
            ).scalars().all()
        }
    # surface forms AND canonical forms both address the entity — the model
    # may echo either
    name_to_entity: dict[str, uuid.UUID] = {}
    for m in mentions:
        ent = entities.get(m.entity_id)
        if ent is None:
            continue
        for nm in (m.surface_form, ent.canonical_form):
            if nm:
                name_to_entity.setdefault(nm.strip().lower(), ent.id)
    entity_types = {
        eid: e.entity_type_id for eid, e in entities.items()
    }
    if not name_to_entity:
        report["skipped"] = "no linked mentions"
        return report

    # Offer only definitions this document can actually source: a
    # document-class-sourced definition must match THIS class (a decision
    # is not a book chapter), and an entity-sourced one needs at least one
    # linked mention of that type to anchor on. Target types are not
    # filtered — a missing target parks as unresolved, never dropped.
    present_types = set(entity_types.values())
    applicable = [
        d for d in definitions
        if (
            d["source_ref_id"] == doc.document_class_id
            if d["source_ref_type"] == "document_class"
            else d["source_ref_id"] in present_types
        )
        and (
            d["target_ref_id"] == doc.document_class_id
            if d["target_ref_type"] == "document_class"
            else True
        )
    ]
    if not applicable:
        report["skipped"] = "no applicable definitions"
        return report

    # one call per chunk (usually one chunk); citations sit deep in long
    # court decisions, so the head alone is not enough
    chunks = _chunks(content)
    prompts = [
        build_prompt(
            filename=doc.filename or "",
            content=chunk,
            definitions=applicable,
            mention_names=[e.canonical_form for e in entities.values()],
        )
        for chunk in chunks
    ]
    # No transaction across a parked wave: in provider-batch mode these
    # calls wait minutes, and the reads above opened a transaction. Ends a
    # read-only transaction in sync mode — harmless there. The prep gate is
    # released for the wait so parked tasks never block other docs' prep.
    await session.commit()
    _PREP_GATE.release()
    try:
        replies = await asyncio.gather(
            *(sinas.invoke(RELATIONSHIP_AGENT, p) for p in prompts)
        )
    finally:
        await _PREP_GATE.acquire()
    report["llm_calls"] = len(prompts)
    report["chunks"] = len(chunks)
    edges: list[dict] = []
    unparsed = 0
    for reply in replies:
        try:
            edges.extend(_parse_json_reply(reply).get("relationships") or [])
        except ValueError:
            unparsed += 1
    if unparsed:
        report["unparsed_chunks"] = unparsed
    if unparsed == len(replies):
        report["unparsed"] = True
        return report
    report["extracted"] = len(edges)

    counts = await _apply_edges(
        session,
        document_id=document_id,
        content=content,
        edges=edges,
        definitions=applicable,
        name_to_entity=name_to_entity,
        entity_types=entity_types,
        doc_class_id=doc.document_class_id,
        write=write,
        key_index=key_index,
    )
    report.update(counts)
    if write:
        await session.commit()
    return report


async def extract_relationships(
    document_ids: list[uuid.UUID] | None = None, *, write: bool = True
) -> list[dict[str, Any]]:
    """Extract relationships for the given documents (or every document
    with linked active mentions when omitted)."""
    sinas = _Sinas()
    async with AsyncSessionLocal() as session:
        definitions = await load_definitions(session)
        # The process-shared key index: keys learned from an early document
        # resolve references in later ones, and per-document callers (the
        # ingestion runner) do not pay a 446k-entity rebuild per call.
        from app.services.entity_keys import shared_index

        key_index = await shared_index(session)
        if document_ids is None:
            document_ids = list(
                (
                    await session.execute(
                        select(EntityMention.document_id)
                        .where(EntityMention.status == "active")
                        .where(EntityMention.entity_id.is_not(None))
                        .distinct()
                    )
                ).scalars().all()
            )
    reports = []
    for did in document_ids:
        async with AsyncSessionLocal() as session:
            try:
                reports.append(
                    await extract_document(
                        session, sinas, did, definitions=definitions,
                        write=write, key_index=key_index,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — per-doc isolation
                reports.append({"document": str(did), "error": str(exc)[:300]})
    return reports
