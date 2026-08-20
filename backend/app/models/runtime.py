from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._common import OwnedMixin, TimestampMixin, uuid_pk


# ─────────────────────────────────────────────────────────────
# Documents and versions
# ─────────────────────────────────────────────────────────────
class Document(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "document"

    id: Mapped[uuid.UUID] = uuid_pk()
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    toc: Mapped[dict | None] = mapped_column(JSONB)
    document_class_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_class.id", ondelete="SET NULL"), index=True
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    collection_file_id: Mapped[str | None] = mapped_column(String(500), index=True)
    # Staged docs skip the auto-ingestion pipeline (classifier + extractors
    # don't fire on upload). Discovery and front-matter scans still see them;
    # retrieval does not. Flips false when a manual IngestionRun completes.
    staged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="DocumentVersion.document_id",
    )


class DocumentVersion(Base, TimestampMixin):
    __tablename__ = "document_version"
    __table_args__ = (
        Index("ix_document_version_doc_v", "document_id", "version", unique=True),
        Index("ix_document_version_tsv", "content_tsvector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_md: Mapped[str | None] = mapped_column(Text)
    content_tsvector: Mapped[str | None] = mapped_column(TSVECTOR)

    document: Mapped[Document] = relationship(
        back_populates="versions", foreign_keys=[document_id]
    )


# ─────────────────────────────────────────────────────────────
# Property values
# ─────────────────────────────────────────────────────────────
class PropertyValue(Base, TimestampMixin):
    __tablename__ = "property_value"

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_class_property.id", ondelete="CASCADE"),
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_span: Mapped[dict | None] = mapped_column(JSONB)
    method: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)  # auto|manual
    confidence: Mapped[float | None] = mapped_column(Float)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


# ─────────────────────────────────────────────────────────────
# Entities
# ─────────────────────────────────────────────────────────────
class Entity(Base, TimestampMixin):
    __tablename__ = "entity"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_type.id", ondelete="CASCADE"), index=True
    )
    canonical_form: Mapped[str] = mapped_column(String(500), nullable=False)
    extra_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    # package-declared identity (e.g. a case number, an act+year signature);
    # unique per type among live (unmerged) entities
    natural_key: Mapped[str | None] = mapped_column(String(300))
    # Name identity: entity_resolver.normalize(canonical_form). Unique per
    # type among live entities, so a second "CMA" cannot be created at all —
    # 98.9% of entities carry no natural_key, which left name-identified
    # entities with no constraint whatsoever and made duplicates a matter of
    # timing rather than correctness.
    normalized_form: Mapped[str | None] = mapped_column(String(500))
    # merge tombstone: set when this entity was merged into another
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="SET NULL")
    )


class EntityAlias(Base, TimestampMixin):
    __tablename__ = "entity_alias"

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(String(500), nullable=False)
    context_rules: Mapped[dict | None] = mapped_column(JSONB)


class EntityMention(Base, TimestampMixin):
    """Ground truth of extraction: the text as written, typed, with the
    document-local resolved form. Linking to a canonical entity is a
    re-computable annotation performed by services/entity_resolver."""

    __tablename__ = "entity_mention"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="CASCADE"), index=True
    )
    span: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    # ground truth
    surface_form: Mapped[str | None] = mapped_column(String(500))
    resolved_form: Mapped[str | None] = mapped_column(String(500))
    entity_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_type.id", ondelete="SET NULL")
    )
    # link annotation: natural_key | alias | adjudicated | created | legacy | manual
    link_method: Mapped[str | None] = mapped_column(String(20))
    link_confidence: Mapped[float | None] = mapped_column(Float)
    link_evidence: Mapped[dict | None] = mapped_column(JSONB)
    # grounding annotation: active | rejected_ungrounded. Soft drop: an
    # ungrounded name stays for audit but no consumer query sees it.
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="active", default="active"
    )


class EntityProposal(Base, TimestampMixin):
    """Proposed Entity awaiting human approval.

    Created when the entity-extractor agent reports a mention against an
    EntityType whose `creation_mode == 'review'` and no alias match exists.
    Approval promotes the proposal to a real `Entity`.
    """

    __tablename__ = "entity_proposal"
    __table_args__ = (
        Index("ix_entity_proposal_pending", "status", postgresql_where="status = 'pending'"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_type.id", ondelete="CASCADE"), index=True
    )
    canonical_form: Mapped[str] = mapped_column(String(500), nullable=False)
    extra_metadata: Mapped[dict | None] = mapped_column(JSONB)
    proposing_agent: Mapped[str | None] = mapped_column(String(200))
    reasoning: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL")
    )
    evidence_span: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    promoted_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="SET NULL")
    )


class UnresolvedEntityMention(Base, TimestampMixin):
    """A mention the extractor reported that didn't match anything when the
    EntityType is `closed`. A reviewer can later match it to an existing
    entity, promote it to a new entity, or dismiss it.
    """

    __tablename__ = "unresolved_entity_mention"
    __table_args__ = (
        Index(
            "ix_unresolved_entity_mention_open",
            "entity_type_id",
            "mention_text",
            postgresql_where="status = 'unresolved'",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    entity_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_type.id", ondelete="CASCADE"), index=True
    )
    mention_text: Mapped[str] = mapped_column(String(500), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    span: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    proposing_agent: Mapped[str | None] = mapped_column(String(200))
    reasoning: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default="unresolved", nullable=False, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="SET NULL")
    )


# ─────────────────────────────────────────────────────────────
# Annotation values (materialized derived fields)
# ─────────────────────────────────────────────────────────────
class AnnotationValue(Base, TimestampMixin):
    """Materialized value of an annotation for one subject. Written by
    services/annotations.materialize(); absent = the path reached nothing."""

    __tablename__ = "annotation_value"
    __table_args__ = (
        UniqueConstraint("annotation_definition_id", "subject_id", name="uq_annotation_subject"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    annotation_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotation_definition.id", ondelete="CASCADE"),
        index=True,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)


# ─────────────────────────────────────────────────────────────
# Relationships (instances) and proposals
# ─────────────────────────────────────────────────────────────
class Relationship(Base, TimestampMixin):
    __tablename__ = "relationship"

    id: Mapped[uuid.UUID] = uuid_pk()
    relationship_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relationship_definition.id", ondelete="CASCADE"),
        index=True,
    )
    # Polymorphic refs — type comes from the definition.
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    current_state_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("relationship_state.id", ondelete="SET NULL")
    )
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL")
    )
    evidence_span: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class RelationshipProposal(Base, TimestampMixin):
    __tablename__ = "relationship_proposal"
    __table_args__ = (Index("ix_rel_proposal_pending", "status", postgresql_where="status = 'pending'"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    relationship_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relationship_definition.id", ondelete="CASCADE"),
        index=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    suggested_state_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("relationship_state.id", ondelete="SET NULL")
    )
    proposing_agent: Mapped[str | None] = mapped_column(String(200))
    reasoning: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL")
    )
    evidence_span: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class UnresolvedRelationship(Base, TimestampMixin):
    """A relationship whose source node is known but whose target isn't in the
    graph yet — e.g. an article that cites an ECLI we haven't ingested.

    Held as a candidate keyed by `target_key` (the raw external reference). A
    resolver promotes it to a real `Relationship` once a node carrying that key
    is ingested, so we never have to re-scan every existing document on each new
    ingest — resolution is a single keyed lookup at the moment the target lands.
    """

    __tablename__ = "unresolved_relationship"
    __table_args__ = (
        Index(
            "ix_unresolved_rel_open",
            "target_key",
            postgresql_where="status = 'unresolved'",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    relationship_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relationship_definition.id", ondelete="CASCADE"),
        index=True,
    )
    # The known end of the edge — type comes from the definition's source ref.
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # The unresolved end: a raw external key plus what kind it is, so the
    # resolver knows which document property to match against (e.g. "ecli").
    target_key: Mapped[str] = mapped_column(String(500), nullable=False)
    target_key_kind: Mapped[str | None] = mapped_column(String(64))
    suggested_state_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("relationship_state.id", ondelete="SET NULL")
    )
    proposing_agent: Mapped[str | None] = mapped_column(String(200))
    reasoning: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL")
    )
    evidence_span: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    # unresolved → resolved (target ingested, promoted) | dismissed (won't resolve)
    status: Mapped[str] = mapped_column(
        String(20), default="unresolved", nullable=False, index=True
    )
    resolved_relationship_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("relationship.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ─────────────────────────────────────────────────────────────
# Results, traces, answers, claim evidence
# ─────────────────────────────────────────────────────────────
class Result(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "result"
    __table_args__ = (
        Index("ix_result_published", "status", postgresql_where="status = 'published'"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    query: Mapped[str] = mapped_column(Text, nullable=False)
    invoked_skill_names: Mapped[list[str] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    parent_result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("result.id", ondelete="SET NULL")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The current draft filter (GroveFilter shape). Mutated via the
    # /retrieval/results/{id}/filter/* endpoints. Frozen on publish.
    filter: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Optimistic concurrency token. Mutations require matching version and
    # 409 on mismatch. See ADR 2026-05-14-stateful-filter-on-result.
    filter_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class ResultDocument(Base, TimestampMixin):
    __tablename__ = "result_document"

    id: Mapped[uuid.UUID] = uuid_pk()
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("result.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    rank: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    added_by_agent: Mapped[str | None] = mapped_column(String(200))


class ResultTrace(Base):
    __tablename__ = "result_trace"

    id: Mapped[uuid.UUID] = uuid_pk()
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("result.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    agent: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    parameters: Mapped[dict | None] = mapped_column(JSONB)
    outcome: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Answer(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "answer"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("result.id", ondelete="SET NULL"), index=True
    )
    source_dossier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier.id", ondelete="SET NULL"), index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnswerClaim(Base, TimestampMixin):
    __tablename__ = "answer_claim"

    id: Mapped[uuid.UUID] = uuid_pk()
    answer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("answer.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str | None] = mapped_column(String(100))


class ClaimEvidence(Base, TimestampMixin):
    __tablename__ = "claim_evidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("answer_claim.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    span: Mapped[dict] = mapped_column(JSONB, nullable=False)
    stance: Mapped[str] = mapped_column(String(20), default="supports", nullable=False)
    relevance: Mapped[float | None] = mapped_column(Float)
    validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    validation_reasoning: Mapped[str | None] = mapped_column(Text)


# ─────────────────────────────────────────────────────────────
# Dossiers (optional)
# ─────────────────────────────────────────────────────────────
class Dossier(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "dossier"

    id: Mapped[uuid.UUID] = uuid_pk()
    dossier_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier_class.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str | None] = mapped_column(String(50))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DossierPropertyValue(Base, TimestampMixin):
    __tablename__ = "dossier_property_value"

    id: Mapped[uuid.UUID] = uuid_pk()
    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier_class_property.id", ondelete="CASCADE"), index=True
    )
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier.id", ondelete="CASCADE"), index=True
    )
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_span: Mapped[dict | None] = mapped_column(JSONB)
    method: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class DossierDocument(Base, TimestampMixin):
    __tablename__ = "dossier_document"
    __table_args__ = (Index("ix_dossier_doc_unique", "dossier_id", "document_id", unique=True),)

    id: Mapped[uuid.UUID] = uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str | None] = mapped_column(String(100))
    added_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
