"""Ingestion writes — called by Grove agents during the ingestion pipeline.

The post-upload function in the Sinas package POSTs to /ingest/post-upload to
register a new document; from there agents call set_document_class,
set_document_summary, set_property_value, etc.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.config import get_settings
from app.db import get_session
from app.models import (
    Document,
    DocumentVersion,
    Dossier,
    Entity,
    EntityAlias,
    EntityMention,
    EntityProposal,
    EntityType,
    PropertyValue,
    Relationship,
    RelationshipDefinition,
    RelationshipProposal,
    UnresolvedEntityMention,
    UnresolvedRelationship,
)
from app.schemas.runtime import (
    PropertyValueIn,
    PropertyValueOut,
    RelationshipIn,
    RelationshipOut,
    RelationshipProposalIn,
    RelationshipProposalOut,
    UnresolvedRelationshipIn,
    UnresolvedRelationshipOut,
)

router = APIRouter(prefix="/ingest", tags=["ingestion"])

logger = logging.getLogger(__name__)


def _guard_mode(setting: str) -> str:
    """Read a precision guard's enforcement policy (reject | warn | off) from
    settings. The policy is a deployment choice, never derived from any
    particular schema; the rule each guard enforces is read from config at
    the call site.
    """
    return getattr(get_settings(), setting, "reject")


def _guard_decide(guard: str, mode: str, detail: str) -> bool:
    """Log a precision-guard hit and report whether the write must be skipped.

    `mode` is the deployment policy (reject | warn). reject → skip + log and
    return True; warn → log and return False so the write proceeds. The log
    line is structured (stable key=value fields) so rejections and warnings
    are countable downstream.
    """
    logger.warning(
        "precision_guard guard=%s mode=%s action=%s %s",
        guard,
        mode,
        "reject" if mode == "reject" else "warn",
        detail,
    )
    return mode == "reject"


# ─────────────────────────── post-upload registration ───────────────────────────
class PostUploadIn(BaseModel):
    collection_file_id: str
    filename: str
    # content_md is optional: only text uploads carry it. Binary formats
    # (.docx, .pdf, etc.) are registered without text and rely on a later
    # extraction step to populate the version's content.
    content_md: str | None = None
    metadata: dict | None = None
    # If true, the document skips the auto-pipeline (classifier + extractors
    # don't fire). Used for the discovery upload flow: drop docs in, scan
    # them with discovery / FM-suggest, design the schema, then promote
    # via an IngestionRun with staged_only=true.
    staged: bool = False


class PostUploadOut(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    version: int
    staged: bool


@router.post(
    "/post-upload",
    response_model=PostUploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_upload(
    payload: PostUploadIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Called by the Sinas post-upload function to register a new document.

    Idempotent on `collection_file_id`: re-uploads create new versions, not
    duplicate documents.
    """
    existing = (
        await session.execute(
            select(Document).where(Document.collection_file_id == payload.collection_file_id)
        )
    ).scalar_one_or_none()

    if existing is None:
        doc = Document(
            filename=payload.filename,
            collection_file_id=payload.collection_file_id,
            owner_id=caller.user_id,
            roles=caller.roles or [],
            staged=payload.staged,
        )
        session.add(doc)
        await session.flush()
        version = 1
    else:
        doc = existing
        # Re-uploading a doc keeps its current staged state. To unstage,
        # use the promote IngestionRun path; to re-stage, edit directly.
        latest = (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        version = (latest.version + 1) if latest else 1

    # Postgres TEXT can't store NUL bytes — strip them defensively even for
    # nominally-text uploads where one snuck through. Also defend against
    # the U+0000 escape that some markdown converters emit for binary residue.
    content_md = (
        payload.content_md.replace("\x00", "").replace("\\u0000", "")
        if payload.content_md
        else None
    )
    dv = DocumentVersion(document_id=doc.id, version=version, content_md=content_md)
    session.add(dv)
    await session.flush()

    doc.current_version_id = dv.id
    await session.commit()
    return PostUploadOut(
        document_id=doc.id,
        document_version_id=dv.id,
        version=version,
        staged=doc.staged,
    )


# ─────────────────────────── classifier ───────────────────────────
class SetClassIn(BaseModel):
    document_class_id: uuid.UUID
    confidence: float | None = None
    reason: str | None = None


@router.post(
    "/documents/{doc_id}/class",
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def set_document_class(
    doc_id: uuid.UUID,
    payload: SetClassIn,
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    doc.document_class_id = payload.document_class_id
    doc.classification_confidence = payload.confidence
    # Classifier-agent success is the canonical moment a doc enters the
    # pipeline. Unstage here so the no-worker runner doesn't need a
    # separate reconciliation step for it.
    if doc.staged:
        doc.staged = False
    await session.commit()
    return {"ok": True}


# ─────────────────────────── summarizer ───────────────────────────
class SetSummaryIn(BaseModel):
    summary: str
    toc: dict | None = None


@router.post(
    "/documents/{doc_id}/summary",
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def set_document_summary(
    doc_id: uuid.UUID,
    payload: SetSummaryIn,
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    doc.summary = payload.summary
    doc.toc = payload.toc
    await session.commit()
    return {"ok": True}


# ─────────────────────────── properties ───────────────────────────
@router.post(
    "/property-values",
    response_model=PropertyValueOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def set_property_value(
    payload: PropertyValueIn,
    session: AsyncSession = Depends(get_session),
):
    raw = payload.model_dump()
    if isinstance(raw["value"], (str, int, float, bool)) or raw["value"] is None:
        raw["value"] = {"_": raw["value"]}
    elif isinstance(raw["value"], list):
        raw["value"] = {"_": raw["value"]}
    if raw.get("source_span") is not None:
        raw["source_span"] = raw["source_span"]
    row = PropertyValue(**raw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ─────────────────────────── entities ───────────────────────────
class EntityIn(BaseModel):
    """Agent-facing 'find-or-create entity' payload.

    The handler always tries an alias/canonical-form match first. On no match,
    the EntityType's `creation_mode` decides:
      - open    → insert an Entity row (legacy behavior)
      - review  → insert an EntityProposal (needs human approval)
      - closed  → don't create; if evidence (document_id + span) was supplied,
                  park it as an UnresolvedEntityMention for review

    The response `kind` tells the caller which table the id is in so it knows
    whether it can immediately record an EntityMention or must wait for review.
    """

    entity_type_id: uuid.UUID
    canonical_form: str
    extra_metadata: dict | None = None
    # Optional evidence — required only for `closed` mode to park an
    # unresolved mention; useful for `review` mode for context on approval.
    document_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    span: dict | None = None
    confidence: float | None = None
    proposing_agent: str | None = None
    reasoning: str | None = None


class EntityResolution(BaseModel):
    # "rejected" (id is None) is returned when a precision guard skips the write.
    kind: Literal["entity", "proposal", "unresolved", "blocked", "rejected"]
    id: uuid.UUID | None = None
    canonical_form: str
    creation_mode: Literal["open", "review", "closed"]


async def _alias_match(
    session: AsyncSession, entity_type_id: uuid.UUID, canonical_form: str
) -> Entity | None:
    # Exact match on canonical_form first.
    hit = (
        await session.execute(
            select(Entity).where(
                Entity.entity_type_id == entity_type_id,
                Entity.canonical_form == canonical_form,
            )
        )
    ).scalar_one_or_none()
    if hit is not None:
        return hit
    # Then alias.
    aliased = (
        await session.execute(
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(
                Entity.entity_type_id == entity_type_id,
                EntityAlias.alias == canonical_form,
            )
        )
    ).scalar_one_or_none()
    return aliased


async def _content_md(
    session: AsyncSession,
    document_id: uuid.UUID,
    document_version_id: uuid.UUID | None,
) -> str | None:
    """Load the body a mention should appear in: the named version if given,
    else the document's current version. None when there is no text (e.g. a
    binary document registered without extracted content)."""
    if document_version_id is not None:
        dv = await session.get(DocumentVersion, document_version_id)
        return dv.content_md if dv else None
    doc = await session.get(Document, document_id)
    if doc is None or doc.current_version_id is None:
        return None
    dv = await session.get(DocumentVersion, doc.current_version_id)
    return dv.content_md if dv else None


def _span_in_body(span: dict, content_md: str) -> tuple[bool, str]:
    """Check a mention's span points at real text in `content_md`.

    Verifies the recorded offsets rather than substring-searching a canonical
    form: canonical forms legitimately differ from the surface text (aliases,
    abbreviations, normalised names), so a substring check would false-reject.
    Prefers char offsets, then a line range; a span carrying neither is
    unverifiable and allowed. Returns (ok, reason)."""
    char_from, char_to = span.get("char_from"), span.get("char_to")
    if char_from is not None and char_to is not None:
        n = len(content_md)
        if not (0 <= char_from < char_to <= n):
            return False, f"char span [{char_from},{char_to}] out of bounds (len={n})"
        if not content_md[char_from:char_to].strip():
            return False, f"char span [{char_from},{char_to}] is whitespace only"
        return True, "char span ok"
    line_from, line_to = span.get("line_from"), span.get("line_to")
    if line_from is not None and line_to is not None:
        n_lines = content_md.count("\n") + 1
        if not (1 <= line_from <= line_to <= n_lines):
            return False, f"line span [{line_from},{line_to}] out of bounds (lines={n_lines})"
        return True, "line span ok"
    return True, "no offsets, unverifiable"


@router.post(
    "/entities",
    response_model=EntityResolution,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def propose_new_entity(
    payload: EntityIn, session: AsyncSession = Depends(get_session)
):
    et = await session.get(EntityType, payload.entity_type_id)
    if et is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity type not found")

    matched = await _alias_match(session, et.id, payload.canonical_form)
    if matched is not None:
        return EntityResolution(
            kind="entity",
            id=matched.id,
            canonical_form=matched.canonical_form,
            creation_mode=et.creation_mode,  # type: ignore[arg-type]
        )

    # Guard 3 — when evidence is supplied, verify its span points at real body
    # text before creating anything from it.
    mode = _guard_mode("grove_guard_mention_in_body")
    if mode != "off" and payload.document_id is not None and payload.span is not None:
        body = await _content_md(session, payload.document_id, payload.document_version_id)
        if body:
            ok, reason = _span_in_body(payload.span, body)
            if not ok and _guard_decide(
                "mention_in_body", mode, f"document={payload.document_id} {reason}"
            ):
                return EntityResolution(
                    kind="rejected",
                    id=None,
                    canonical_form=payload.canonical_form,
                    creation_mode=et.creation_mode,  # type: ignore[arg-type]
                )

    if et.creation_mode == "open":
        row = Entity(
            entity_type_id=et.id,
            canonical_form=payload.canonical_form,
            extra_metadata=payload.extra_metadata,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return EntityResolution(
            kind="entity",
            id=row.id,
            canonical_form=row.canonical_form,
            creation_mode="open",
        )

    if et.creation_mode == "review":
        prop = EntityProposal(
            entity_type_id=et.id,
            canonical_form=payload.canonical_form,
            extra_metadata=payload.extra_metadata,
            proposing_agent=payload.proposing_agent,
            reasoning=payload.reasoning,
            evidence_document_id=payload.document_id,
            evidence_span=payload.span,
            confidence=payload.confidence,
            status="pending",
        )
        session.add(prop)
        await session.commit()
        await session.refresh(prop)
        return EntityResolution(
            kind="proposal",
            id=prop.id,
            canonical_form=prop.canonical_form,
            creation_mode="review",
        )

    # closed
    if payload.document_id is not None and payload.span is not None:
        mention = UnresolvedEntityMention(
            entity_type_id=et.id,
            mention_text=payload.canonical_form,
            document_id=payload.document_id,
            document_version_id=payload.document_version_id,
            span=payload.span,
            confidence=payload.confidence,
            proposing_agent=payload.proposing_agent,
            reasoning=payload.reasoning,
            status="unresolved",
        )
        session.add(mention)
        await session.commit()
        await session.refresh(mention)
        return EntityResolution(
            kind="unresolved",
            id=mention.id,
            canonical_form=payload.canonical_form,
            creation_mode="closed",
        )
    return EntityResolution(
        kind="blocked",
        id=None,
        canonical_form=payload.canonical_form,
        creation_mode="closed",
    )


class EntityMentionInWithBody(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    entity_id: uuid.UUID
    span: dict
    confidence: float | None = None


@router.post(
    "/entity-mentions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def record_entity_mention(
    payload: EntityMentionInWithBody, session: AsyncSession = Depends(get_session)
):
    # Guard 3 — verify the mention's span points at real text in the body.
    mode = _guard_mode("grove_guard_mention_in_body")
    if mode != "off":
        body = await _content_md(
            session, payload.document_id, payload.document_version_id
        )
        if body:  # unverifiable without body text (e.g. binary docs) → allow
            ok, reason = _span_in_body(payload.span, body)
            if not ok and _guard_decide(
                "mention_in_body", mode, f"document={payload.document_id} {reason}"
            ):
                return {"id": None, "rejected": True, "reason": reason}
    row = EntityMention(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id, "rejected": False}


class FindEntityIn(BaseModel):
    name: str
    document_id: uuid.UUID


class EntityMatch(BaseModel):
    entity_id: uuid.UUID
    canonical_form: str
    mention_id: uuid.UUID
    confidence: float | None = None


@router.post(
    "/find-entity",
    response_model=list[EntityMatch],
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def find_entity_by_name(
    payload: FindEntityIn, session: AsyncSession = Depends(get_session)
):
    """Resolve an entity name to entity IDs among the entities already mentioned
    in a document.

    The relationship extractor calls this to turn the names it reads in the text
    into the source_id / target_id that record_relationship needs. Matching is
    case-insensitive and partial (substring) against the entity's canonical form
    and its aliases, so a short query like "Commission" returns candidates the
    agent can pick from.
    """
    pattern = f"%{payload.name.strip()}%"
    stmt = (
        select(
            EntityMention.id,
            Entity.id,
            Entity.canonical_form,
            EntityMention.confidence,
        )
        .join(Entity, EntityMention.entity_id == Entity.id)
        .outerjoin(EntityAlias, EntityAlias.entity_id == Entity.id)
        .where(EntityMention.document_id == payload.document_id)
        .where(
            or_(
                Entity.canonical_form.ilike(pattern),
                EntityAlias.alias.ilike(pattern),
            )
        )
        .distinct()
        .limit(20)
    )
    rows = (await session.execute(stmt)).all()
    return [
        EntityMatch(
            mention_id=mention_id,
            entity_id=entity_id,
            canonical_form=canonical_form,
            confidence=confidence,
        )
        for mention_id, entity_id, canonical_form, confidence in rows
    ]


# ─────────────────────────── relationships ───────────────────────────
async def _check_relationship_mode(
    session: AsyncSession, relationship_definition_id: uuid.UUID
) -> RelationshipDefinition:
    rdef = await session.get(RelationshipDefinition, relationship_definition_id)
    if rdef is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "relationship definition not found")
    if rdef.creation_mode == "closed":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"relationship '{rdef.name}' is locked — runtime extraction is not allowed for this definition",
        )
    return rdef


# Maps a relationship definition's ref_type to the (model, type-column) that
# identifies a node's type. The keys are the ref_types Grove supports; the
# expected type id always comes from the definition, so this stays agnostic to
# any particular schema.
_REF_TYPE_NODE = {
    "entity_type": (Entity, "entity_type_id"),
    "document_class": (Document, "document_class_id"),
    "dossier_class": (Dossier, "dossier_class_id"),
}


async def _ref_type_mismatch(
    session: AsyncSession, ref_type: str, ref_id: uuid.UUID, node_id: uuid.UUID
) -> str | None:
    """Return a reason string if `node_id`'s declared type differs from the
    `ref_id` the definition expects, else None. The (model, column) is chosen
    by `ref_type`, which the definition declares; an unknown ref_type cannot be
    checked from config and is left unenforced (returns None)."""
    resolver = _REF_TYPE_NODE.get(ref_type)
    if resolver is None:
        return None
    model, column = resolver
    node = await session.get(model, node_id)
    if node is None:
        return f"{ref_type} node {node_id} not found"
    actual = getattr(node, column)
    if actual != ref_id:
        return f"{ref_type} expected {ref_id} got {actual}"
    return None


class RelationshipResolution(BaseModel):
    # "rejected" (id is None) is returned when a precision guard skips the write.
    kind: Literal["relationship", "proposal", "rejected"]
    id: uuid.UUID | None = None
    creation_mode: Literal["open", "review", "closed"]


@router.post(
    "/relationships",
    response_model=RelationshipResolution,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def record_relationship(
    payload: RelationshipIn, session: AsyncSession = Depends(get_session)
):
    rdef = await _check_relationship_mode(session, payload.relationship_definition_id)

    # Guard 1 — reject a self-referential edge (source == target). Entity
    # resolution can hand back the same node for both ends (name matching is
    # substring-based), which would otherwise persist a self-loop.
    mode = _guard_mode("grove_guard_self_reference")
    if mode != "off" and payload.source_id == payload.target_id:
        if _guard_decide(
            "self_reference", mode, f"definition={rdef.name} node={payload.source_id}"
        ):
            return RelationshipResolution(
                kind="rejected", id=None, creation_mode=rdef.creation_mode  # type: ignore[arg-type]
            )

    # Guard 2 — validate each endpoint against the type the definition declares
    # (source_ref_type / target_ref_type + the matching *_ref_id). The rule is
    # read entirely from the definition, so whatever a given schema declares is
    # what gets enforced.
    mode = _guard_mode("grove_guard_relationship_type")
    if mode != "off":
        for end, ref_type, ref_id, node_id in (
            ("source", rdef.source_ref_type, rdef.source_ref_id, payload.source_id),
            ("target", rdef.target_ref_type, rdef.target_ref_id, payload.target_id),
        ):
            reason = await _ref_type_mismatch(session, ref_type, ref_id, node_id)
            if reason is not None and _guard_decide(
                "relationship_type", mode, f"definition={rdef.name} {end} {reason}"
            ):
                return RelationshipResolution(
                    kind="rejected", id=None, creation_mode=rdef.creation_mode  # type: ignore[arg-type]
                )

    data = payload.model_dump()
    span = data.pop("evidence_span", None)

    if rdef.creation_mode == "review":
        # Translate the direct payload into a proposal. Drop `last_verified_at`
        # (not relevant pre-approval) and map evidence fields verbatim.
        prop = RelationshipProposal(
            relationship_definition_id=data["relationship_definition_id"],
            source_id=data["source_id"],
            target_id=data["target_id"],
            suggested_state_id=data.get("current_state_id"),
            evidence_document_id=data.get("evidence_document_id"),
            evidence_span=span,
            confidence=data.get("confidence"),
            status="pending",
        )
        session.add(prop)
        await session.commit()
        await session.refresh(prop)
        return RelationshipResolution(kind="proposal", id=prop.id, creation_mode="review")

    row = Relationship(
        **data, evidence_span=span, last_verified_at=datetime.now(timezone.utc)
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return RelationshipResolution(kind="relationship", id=row.id, creation_mode="open")


@router.post(
    "/relationship-proposals",
    response_model=RelationshipProposalOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def propose_relationship(
    payload: RelationshipProposalIn, session: AsyncSession = Depends(get_session)
):
    await _check_relationship_mode(session, payload.relationship_definition_id)
    row = RelationshipProposal(**payload.model_dump(), status="pending")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.post(
    "/unresolved-relationships",
    response_model=UnresolvedRelationshipOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("grove.ingestion.write:own"))],
)
async def record_unresolved_relationship(
    payload: UnresolvedRelationshipIn, session: AsyncSession = Depends(get_session)
):
    """Park a relationship whose target node isn't in the graph yet (e.g. a
    cited ECLI we haven't ingested), keyed by `target_key`. A resolver promotes
    it to a real Relationship once the target lands — see UnresolvedRelationship.
    """
    await _check_relationship_mode(session, payload.relationship_definition_id)
    data = payload.model_dump()
    span = data.pop("evidence_span", None)
    row = UnresolvedRelationship(**data, evidence_span=span, status="unresolved")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
