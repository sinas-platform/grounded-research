from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel, TimestampedOut

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


def slugify(value: str) -> str:
    """Lowercase, non-alphanumeric → '_', collapse repeats, strip ends."""
    s = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    return s[:64] or "unnamed"


# ─────────────────────────────────────────────────────────────
# Document classes
# ─────────────────────────────────────────────────────────────
class DocumentClassUpdate(BaseModel):
    """Updates — slug is intentionally absent because it's immutable."""

    name: str
    description: str | None = None
    summarization_guidance: str | None = None
    classification_hints: str | None = None


class DocumentClassCreate(DocumentClassUpdate):
    """Creates — slug is required (or auto-derived from name on the server)."""

    slug: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not SLUG_RE.match(v):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9_]{0,63}$ (lowercase alnum and underscores)"
            )
        return v


class DocumentClassOut(TimestampedOut, DocumentClassUpdate):
    slug: str


class DocumentClassPropertyIn(BaseModel):
    name: str
    description: str | None = None
    schema: dict[str, Any] = Field(default_factory=dict)
    guidance: str | None = None
    manual: bool = False
    required: bool = False
    cardinality: Literal["one", "many"] = "one"
    schema_version: int = 1


class DocumentClassPropertyOut(TimestampedOut, DocumentClassPropertyIn):
    document_class_id: uuid.UUID


# ─────────────────────────────────────────────────────────────
# Entity types
# ─────────────────────────────────────────────────────────────
EntityCreationMode = Literal["open", "review", "closed"]


class EntityTypeIn(BaseModel):
    name: str
    description: str | None = None
    guidance: str | None = None
    creation_mode: EntityCreationMode = "open"


class EntityTypeOut(TimestampedOut, EntityTypeIn):
    pass


# ─────────────────────────────────────────────────────────────
# Relationship definitions
# ─────────────────────────────────────────────────────────────
RefType = Literal["document_class", "entity_type", "dossier_class"]


class RelationshipDefinitionIn(BaseModel):
    name: str
    description: str | None = None
    source_ref_type: RefType
    source_ref_id: uuid.UUID
    target_ref_type: RefType
    target_ref_id: uuid.UUID
    cardinality: Literal["one", "many"] = "many"
    extraction_guidance: str | None = None
    discovery_guidance: str | None = None
    creation_mode: EntityCreationMode = "open"


class RelationshipDefinitionOut(TimestampedOut, RelationshipDefinitionIn):
    pass


class RelationshipStateIn(BaseModel):
    name: str
    description: str | None = None
    counts_as_active: bool = True


class RelationshipStateOut(TimestampedOut, RelationshipStateIn):
    relationship_definition_id: uuid.UUID


# ─────────────────────────────────────────────────────────────
# Dossier classes
# ─────────────────────────────────────────────────────────────
class DossierClassUpdate(BaseModel):
    name: str
    description: str | None = None
    guidance: str | None = None
    summarization_guidance: str | None = None
    classification_hints: str | None = None


class DossierClassCreate(DossierClassUpdate):
    slug: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not SLUG_RE.match(v):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9_]{0,63}$ (lowercase alnum and underscores)"
            )
        return v


class DossierClassOut(TimestampedOut, DossierClassUpdate):
    slug: str


class DossierClassPropertyIn(DocumentClassPropertyIn):
    pass


class DossierClassPropertyOut(TimestampedOut, DossierClassPropertyIn):
    dossier_class_id: uuid.UUID


# ─────────────────────────────────────────────────────────────
# Playbooks (content + scope, both SGR-side)
# ─────────────────────────────────────────────────────────────
PlaybookKindLit = Literal["retrieval", "synthesis", "validation"]


class PlaybookIn(BaseModel):
    kind: PlaybookKindLit
    name: str
    description: str = ""
    content: str = ""


class PlaybookOut(TimestampedOut, PlaybookIn):
    pass


class PlaybookScopeIn(BaseModel):
    playbook_id: uuid.UUID
    document_class_id: uuid.UUID | None = None
    dossier_class_id: uuid.UUID | None = None


class PlaybookScopeOut(TimestampedOut, PlaybookScopeIn):
    pass


class PlaybookSummaryOut(ORMModel):
    id: uuid.UUID
    kind: PlaybookKindLit
    name: str
    description: str = ""
    applies_to_document_classes: list[uuid.UUID] = Field(default_factory=list)
    applies_to_dossier_classes: list[uuid.UUID] = Field(default_factory=list)
