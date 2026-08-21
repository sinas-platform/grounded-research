from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel, OwnedOut, Span, TimestampedOut


# ─────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────
class DocumentOut(OwnedOut):
    filename: str
    summary: str | None = None
    toc: dict[str, Any] | None = None
    document_class_id: uuid.UUID | None = None
    classification_confidence: float | None = None
    collection_file_id: str | None = None
    staged: bool = False


class DocumentPatch(BaseModel):
    summary: str | None = None
    toc: dict[str, Any] | None = None
    document_class_id: uuid.UUID | None = None
    classification_confidence: float | None = None


class DocumentVersionOut(TimestampedOut):
    document_id: uuid.UUID
    version: int
    content_md: str | None = None


class PropertyValueIn(BaseModel):
    property_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    value: dict[str, Any] | list[Any] | str | int | float | bool | None
    source_span: Span | None = None
    method: Literal["auto", "manual"] = "auto"
    confidence: float | None = None
    schema_version: int = 1
    locked: bool = False
    reason: str | None = None


class PropertyValueOut(TimestampedOut):
    property_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    value: Any
    source_span: dict[str, Any] | None = None
    method: str
    confidence: float | None = None
    schema_version: int
    locked: bool
    reason: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _unwrap_value(cls, v: Any) -> Any:
        # Scalars and lists are wrapped as {"_": x} on write so they fit a JSONB
        # dict column; unwrap on read so callers see the natural value.
        if isinstance(v, dict) and set(v.keys()) == {"_"}:
            return v["_"]
        return v


# ─────────────────────────────────────────────────────────────
# Entities and relationships
# ─────────────────────────────────────────────────────────────
class EntityMentionIn(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    entity_id: uuid.UUID
    span: Span
    confidence: float | None = None


class EntityMentionOut(TimestampedOut, EntityMentionIn):
    pass


class EntityMentionWithEntityOut(TimestampedOut):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    entity_id: uuid.UUID
    span: dict[str, Any]
    confidence: float | None = None
    entity_canonical_form: str
    entity_type_id: uuid.UUID
    entity_type_name: str | None = None


class RelationshipIn(BaseModel):
    relationship_definition_id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    current_state_id: uuid.UUID | None = None
    evidence_document_id: uuid.UUID | None = None
    evidence_span: Span | None = None
    confidence: float | None = None
    notes: str | None = None


class RelationshipOut(TimestampedOut, RelationshipIn):
    last_verified_at: datetime | None = None


class RelationshipProposalIn(BaseModel):
    relationship_definition_id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    suggested_state_id: uuid.UUID | None = None
    proposing_agent: str | None = None
    reasoning: str | None = None
    evidence_document_id: uuid.UUID | None = None
    evidence_span: Span | None = None
    confidence: float | None = None


class RelationshipProposalOut(TimestampedOut, RelationshipProposalIn):
    status: Literal["pending", "approved", "rejected"]
    reviewed_at: datetime | None = None
    reviewed_by: uuid.UUID | None = None


class UnresolvedRelationshipIn(BaseModel):
    relationship_definition_id: uuid.UUID
    source_id: uuid.UUID
    # Raw reference to the not-yet-ingested target (e.g. an ECLI string) plus
    # what kind of key it is, so the resolver knows which property to match.
    target_key: str
    target_key_kind: str | None = None
    suggested_state_id: uuid.UUID | None = None
    proposing_agent: str | None = None
    reasoning: str | None = None
    evidence_document_id: uuid.UUID | None = None
    evidence_span: Span | None = None
    confidence: float | None = None


class UnresolvedRelationshipOut(TimestampedOut, UnresolvedRelationshipIn):
    status: Literal["unresolved", "resolved", "dismissed"]
    resolved_relationship_id: uuid.UUID | None = None
    resolved_at: datetime | None = None


class EntityProposalIn(BaseModel):
    entity_type_id: uuid.UUID
    canonical_form: str
    extra_metadata: dict | None = None
    proposing_agent: str | None = None
    reasoning: str | None = None
    evidence_document_id: uuid.UUID | None = None
    evidence_span: Span | None = None
    confidence: float | None = None


class EntityProposalOut(TimestampedOut, EntityProposalIn):
    status: Literal["pending", "approved", "rejected"]
    reviewed_at: datetime | None = None
    reviewed_by: uuid.UUID | None = None
    promoted_entity_id: uuid.UUID | None = None


class UnresolvedEntityMentionIn(BaseModel):
    entity_type_id: uuid.UUID
    mention_text: str
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    span: dict
    confidence: float | None = None
    proposing_agent: str | None = None
    reasoning: str | None = None


class UnresolvedEntityMentionOut(TimestampedOut, UnresolvedEntityMentionIn):
    status: Literal["unresolved", "resolved", "dismissed"]
    resolved_entity_id: uuid.UUID | None = None
    resolved_at: datetime | None = None


# ─────────────────────────────────────────────────────────────
# Results, traces, answers
# ─────────────────────────────────────────────────────────────
class ResultOut(OwnedOut):
    query: str
    invoked_skill_names: list[str] | None = None
    status: str
    parent_result_id: uuid.UUID | None = None
    published_at: datetime | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    filter_version: int = 0


class ResultDocumentOut(TimestampedOut):
    result_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    rank: int | None = None
    reason: str | None = None
    added_by_agent: str | None = None
    # Identifying fields, joined from the document so a reader (the synthesis
    # agent, the UI) doesn't need a get_document call per row just to learn
    # what each attached document is. Same shape as MatchingDocumentOut.
    filename: str | None = None
    document_class_name: str | None = None
    summary: str | None = None
    # Present only when the read asked for it (?annotate=): derived graph
    # fields for the case entity this document is the full text of —
    # {"subject_entity_id": ..., "values": {name: value | None}}.
    annotations: dict | None = None


class ResultDocumentCompactOut(BaseModel):
    """Minimal projection of an attached document, for size-sensitive readers
    (an agent paging through a large result): identity and ordering only."""

    document_id: uuid.UUID
    filename: str | None = None
    document_class_name: str | None = None
    rank: int | None = None
    annotations: dict | None = None


class AnswerOut(OwnedOut):
    source_result_id: uuid.UUID | None = None
    source_dossier_id: uuid.UUID | None = None
    question: str
    status: str
    published_at: datetime | None = None


class ClaimOut(TimestampedOut):
    answer_id: uuid.UUID
    sequence: int
    # Consecutive position for display (1..n). Validation drops leave gaps in
    # `sequence` (a stable identifier); readers should number by this field.
    display_number: int | None = None
    claim_text: str
    claim_type: str | None = None
    # Why the claim rests on the source it cites. `validation_reasoning` on
    # the evidence rows answers the opposite question — whether the passage
    # carries the sentence — so a reader needs both to follow the argument.
    rationale: str | None = None


class ClaimEvidenceIn(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    span: Span
    stance: Literal["supports", "contradicts", "qualifies"] = "supports"
    relevance: float | None = None


class ClaimEvidenceOut(TimestampedOut):
    claim_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    span: dict[str, Any]
    stance: str
    relevance: float | None = None
    validated: bool
    validation_reasoning: str | None = None
    # Present only when the read asked for it (?annotate=): derived graph
    # fields for the case entity the evidence document is the full text of
    # — same shape as on result-documents reads.
    annotations: dict | None = None


class ClaimWithEvidenceOut(ClaimOut):
    """A claim with its evidence rows nested — lets a caller (the
    faithfulness validator) enumerate an answer's full evidence set in one
    call instead of one get_claim_evidence call per claim."""

    evidence: list[ClaimEvidenceOut] = []


# ─────────────────────────────────────────────────────────────
# Dossiers
# ─────────────────────────────────────────────────────────────
class DossierIn(BaseModel):
    dossier_class_id: uuid.UUID
    name: str
    status: str | None = None


class DossierOut(OwnedOut):
    dossier_class_id: uuid.UUID
    name: str
    status: str | None = None
    closed_at: datetime | None = None


class DossierDocumentIn(BaseModel):
    document_id: uuid.UUID
    role: str | None = None


class DossierDocumentOut(TimestampedOut, DossierDocumentIn):
    dossier_id: uuid.UUID
    added_by: uuid.UUID | None = None


# ─────────────────────────────────────────────────────────────
# Retrieval — filter object is persisted on Result.filter and mutated via
# /retrieval/results/{id}/filter/* endpoints. See ADR
# docs/adrs/2026-05-14-stateful-filter-on-result.md.
# ─────────────────────────────────────────────────────────────
class FieldFilter(BaseModel):
    field: str
    values: list[Any] | None = None
    op: Literal["eq", "in", "gte", "lte", "gt", "lt", "neq"] = "in"
    value: Any | None = None
    join: Literal["and", "or"] = "and"


class RegexFilter(BaseModel):
    field: str
    pattern: str


class EntityFilter(BaseModel):
    """Document must have an EntityMention to one of `entity_ids`.

    Slot keying: `entity_type_id` is the slot key — there is at most one
    EntityFilter per entity_type_id, and `None` is a valid slot for ungrouped
    use. Multiple EntityFilters (different slots) AND together; within a slot,
    `entity_ids` are OR'd. Mirrors FieldFilter's per-field convention.
    """

    entity_type_id: uuid.UUID | None = None
    entity_ids: list[uuid.UUID] = Field(default_factory=list)


class SgrFilter(BaseModel):
    document_class_id: uuid.UUID | None = None
    field_filters: list[FieldFilter] = Field(default_factory=list)
    regex_filters: list[RegexFilter] = Field(default_factory=list)
    entity_filters: list[EntityFilter] = Field(default_factory=list)
    text_search: str | None = Field(
        default=None,
        description=(
            "Full-text search in web-search syntax: terms are AND-ed by "
            "default, `OR` unions terms, `-term` excludes, and \"quoted "
            "phrases\" match adjacent words."
        ),
    )
    dossier_id: uuid.UUID | None = None
    dossier_class_id: uuid.UUID | None = None
    explicit_excludes: list[uuid.UUID] = Field(default_factory=list)
    explicit_includes: list[uuid.UUID] = Field(default_factory=list)


class IntrospectIn(BaseModel):
    filter: SgrFilter | None = None
    fields: list[str] | None = None
    # 10 values per facet is enough to pick the next narrowing filter; the
    # response is re-read by the agent on every turn, so the default leans
    # small. Callers that genuinely need the long tail can raise it.
    top_k: int = Field(default=10, ge=1, le=100)


class IntrospectFieldDistribution(ORMModel):
    field: str
    values: list[dict[str, Any]]
    total_documents: int


class IntrospectOut(BaseModel):
    candidate_count: int
    distributions: list[IntrospectFieldDistribution]


class MatchingDocumentsIn(BaseModel):
    filter: SgrFilter | None = None
    limit: int = Field(default=50, ge=1, le=200)


class MatchingDocumentOut(BaseModel):
    """One matched document, with just enough to identify it without a
    follow-up get_document call. filename is often a numeric id, so the
    class name and summary preview are what actually tell the caller what
    the document is."""

    id: uuid.UUID
    filename: str
    document_class_id: uuid.UUID | None = None
    document_class_name: str | None = None
    summary: str | None = None


class MatchingDocumentsOut(BaseModel):
    # document_ids stays for callers that only need the ids; documents adds
    # the identifying fields alongside. Both describe the same matches in the
    # same order.
    document_ids: list[uuid.UUID]
    documents: list[MatchingDocumentOut] = []
