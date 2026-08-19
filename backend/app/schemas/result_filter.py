"""Pydantic schemas for Result-filter mutation ops.

See ADR `docs/adrs/2026-05-14-stateful-filter-on-result.md`. Each mutation
request carries `filter_version` (optimistic concurrency); each response
returns the new version, the new full filter, the matching candidate count,
and the trace row sequence written by the server.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.runtime import SgrFilter


# ───────────────────────────── common envelopes ─────────────────────────────


class _VersionedIn(BaseModel):
    """Base for any mutation: caller must supply the expected current version."""

    filter_version: int


class FilterMutationOut(BaseModel):
    """Uniform response shape across every filter mutation op."""

    filter_version: int  # the new version after this mutation
    filter: SgrFilter  # the new full filter state
    candidate_count: int  # docs matching after the mutation
    trace_sequence: int  # sequence number of the auto-written ResultTrace row


# ───────────────────────────── scopes ─────────────────────────────


class SetDocumentClassFilterIn(_VersionedIn):
    document_class_id: uuid.UUID


class SetDossierFilterIn(_VersionedIn):
    dossier_id: uuid.UUID


class SetDossierClassFilterIn(_VersionedIn):
    dossier_class_id: uuid.UUID


class ClearIn(_VersionedIn):
    """For ops that take no parameters beyond the version (clears)."""


# ───────────────────────────── field filters ─────────────────────────────


class SetFieldFilterIn(_VersionedIn):
    op: Literal["eq", "in", "gte", "lte", "gt", "lt", "neq"] = "in"
    values: list[Any] | None = None
    value: Any | None = None
    join: Literal["and", "or"] = "and"


class FieldFilterValuesIn(_VersionedIn):
    """Used by extend / shrink."""

    values: list[Any] = Field(default_factory=list)


# ───────────────────────────── entity filters ─────────────────────────────


class SetEntityFilterIn(_VersionedIn):
    """Slot-keyed by entity_type_id (None = ungrouped slot). entity_ids OR'd
    inside the slot; multiple slots AND across the filter."""

    entity_type_id: uuid.UUID | None = None
    entity_ids: list[uuid.UUID] = Field(default_factory=list)


class EntityFilterValuesIn(_VersionedIn):
    """Used by extend / shrink."""

    entity_type_id: uuid.UUID | None = None
    entity_ids: list[uuid.UUID] = Field(default_factory=list)


class RemoveEntityFilterIn(_VersionedIn):
    entity_type_id: uuid.UUID | None = None


# ───────────────────────────── regex filters ─────────────────────────────


class SetRegexFilterIn(_VersionedIn):
    pattern: str


# ───────────────────────────── text search ─────────────────────────────


class SetTextSearchIn(_VersionedIn):
    query: str


# ───────────────────────────── explicit includes/excludes ─────────────────────────────


class DocumentIdsIn(_VersionedIn):
    document_ids: list[uuid.UUID] = Field(default_factory=list)


# ───────────────────────────── whole-filter ─────────────────────────────


class ReplaceFilterIn(_VersionedIn):
    filter: SgrFilter


# ───────────────────────────── introspect variants ─────────────────────────────


class IntrospectByResultIn(BaseModel):
    """Introspect using the filter persisted on the Result. Optional overlay
    is treated as additional clauses applied on top of the stored filter
    (preview semantics) — overlay is *not* persisted."""

    fields: list[str] | None = None
    # Same default as IntrospectIn.top_k — see the rationale there.
    top_k: int = Field(default=10, ge=1, le=100)
    overlay: SgrFilter | None = None


# ───────────────────────────── draft-result document mutations ─────────────────────────────


class RemoveFilesIn(BaseModel):
    document_ids: list[uuid.UUID] = Field(default_factory=list)
    reason: str | None = None


class DocumentMutationOut(BaseModel):
    """Response shape for draft-doc mutations (add/remove/clear files).
    Doesn't increment filter_version since these don't touch the filter."""

    document_count: int  # current number of docs in the draft result
    trace_sequence: int
