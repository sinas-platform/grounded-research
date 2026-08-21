from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._common import TimestampMixin, uuid_pk


# ─────────────────────────────────────────────────────────────
# Document classes
# ─────────────────────────────────────────────────────────────
class DocumentClass(Base, TimestampMixin):
    __tablename__ = "document_class"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Immutable slug — used as a stable handle in permission strings
    # (`sgr.documents/<slug>.read:own`). Constrained to [a-z0-9_], max 64.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    summarization_guidance: Mapped[str | None] = mapped_column(Text)
    classification_hints: Mapped[str | None] = mapped_column(Text)
    managed_by: Mapped[str | None] = mapped_column(String(128), index=True)

    properties: Mapped[list["DocumentClassProperty"]] = relationship(
        back_populates="document_class", cascade="all, delete-orphan"
    )
    entity_types: Mapped[list["DocumentClassEntityType"]] = relationship(
        back_populates="document_class", cascade="all, delete-orphan"
    )


class DocumentClassProperty(Base, TimestampMixin):
    __tablename__ = "document_class_property"
    __table_args__ = (UniqueConstraint("document_class_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    document_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_class.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    guidance: Mapped[str | None] = mapped_column(Text)
    manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cardinality: Mapped[str] = mapped_column(String(10), default="one", nullable=False)  # one|many
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    document_class: Mapped[DocumentClass] = relationship(back_populates="properties")


# ─────────────────────────────────────────────────────────────
# Entity types
# ─────────────────────────────────────────────────────────────
class EntityType(Base, TimestampMixin):
    __tablename__ = "entity_type"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    guidance: Mapped[str | None] = mapped_column(Text)
    # open | review | closed (see app/alembic/versions/0011_entity_gating.py)
    creation_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="open", server_default="open"
    )
    managed_by: Mapped[str | None] = mapped_column(String(128), index=True)


class DocumentClassEntityType(Base):
    __tablename__ = "document_class_entity_type"

    document_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_class.id", ondelete="CASCADE"), primary_key=True
    )
    entity_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_type.id", ondelete="CASCADE"), primary_key=True
    )
    document_class: Mapped[DocumentClass] = relationship(back_populates="entity_types")


# ─────────────────────────────────────────────────────────────
# Relationship definitions and states
# ─────────────────────────────────────────────────────────────
class RelationshipDefinition(Base, TimestampMixin):
    __tablename__ = "relationship_definition"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    # source/target ref types: "document_class" | "entity_type" | "dossier_class"
    source_ref_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_ref_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cardinality: Mapped[str] = mapped_column(String(10), default="many", nullable=False)
    extraction_guidance: Mapped[str | None] = mapped_column(Text)
    discovery_guidance: Mapped[str | None] = mapped_column(Text)
    # open | review | closed (see 0013_relationship_gating.py)
    creation_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="open", server_default="open"
    )
    managed_by: Mapped[str | None] = mapped_column(String(128), index=True)

    states: Mapped[list["RelationshipState"]] = relationship(
        back_populates="definition", cascade="all, delete-orphan"
    )


class RelationshipState(Base, TimestampMixin):
    __tablename__ = "relationship_state"
    __table_args__ = (UniqueConstraint("relationship_definition_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    relationship_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relationship_definition.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    counts_as_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    definition: Mapped[RelationshipDefinition] = relationship(back_populates="states")


# ─────────────────────────────────────────────────────────────
# Annotation definitions (derived fields over the relationship graph)
# ─────────────────────────────────────────────────────────────
class AnnotationDefinition(Base, TimestampMixin):
    """Config-declared derived field: a relationship path plus a reducer.

    `path` uses the small path language in services/annotations.py; `reduce`
    is a reducer name or a {output_key: reducer} mapping, stored as given.
    `subject_ref_type` is derived at import from the path's first hop
    (document_class | entity_type | dossier_class) so consumers know what
    the subject ids refer to. Materialized definitions keep their computed
    values in annotation_value; the rest compute on request.
    """

    __tablename__ = "annotation_definition"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    reduce: Mapped[dict | str] = mapped_column(JSONB, nullable=False)
    materialize: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    subject_ref_type: Mapped[str] = mapped_column(String(40), nullable=False)
    managed_by: Mapped[str | None] = mapped_column(String(128), index=True)


# ─────────────────────────────────────────────────────────────
# Dossier configuration (optional — only used when configured)
# ─────────────────────────────────────────────────────────────
class DossierClass(Base, TimestampMixin):
    __tablename__ = "dossier_class"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Immutable slug — used as a stable handle in permission strings
    # (`sgr.dossiers/<slug>.read:own`). Constrained to [a-z0-9_], max 64.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    guidance: Mapped[str | None] = mapped_column(Text)
    summarization_guidance: Mapped[str | None] = mapped_column(Text)
    classification_hints: Mapped[str | None] = mapped_column(Text)
    managed_by: Mapped[str | None] = mapped_column(String(128), index=True)

    properties: Mapped[list["DossierClassProperty"]] = relationship(
        back_populates="dossier_class", cascade="all, delete-orphan"
    )
    document_classes: Mapped[list["DossierClassDocumentClass"]] = relationship(
        back_populates="dossier_class", cascade="all, delete-orphan"
    )


class DossierClassProperty(Base, TimestampMixin):
    __tablename__ = "dossier_class_property"
    __table_args__ = (UniqueConstraint("dossier_class_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    dossier_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier_class.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    guidance: Mapped[str | None] = mapped_column(Text)
    manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cardinality: Mapped[str] = mapped_column(String(10), default="one", nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    dossier_class: Mapped[DossierClass] = relationship(back_populates="properties")


class DossierClassDocumentClass(Base):
    __tablename__ = "dossier_class_document_class"

    dossier_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier_class.id", ondelete="CASCADE"), primary_key=True
    )
    document_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_class.id", ondelete="CASCADE"), primary_key=True
    )
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cardinality: Mapped[str] = mapped_column(String(10), default="many", nullable=False)

    dossier_class: Mapped[DossierClass] = relationship(back_populates="document_classes")


# ─────────────────────────────────────────────────────────────
# Playbooks (content + scope, both SGR-side)
# ─────────────────────────────────────────────────────────────
class Playbook(Base, TimestampMixin):
    """Markdown playbook the deep-search / synthesis agents fetch via the
    SGR connector. `kind` is `retrieval` or `synthesis`."""

    __tablename__ = "playbook"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_playbook_kind_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    managed_by: Mapped[str | None] = mapped_column(String(128), index=True)

    scopes: Mapped[list["PlaybookScope"]] = relationship(
        back_populates="playbook", cascade="all, delete-orphan"
    )


class PlaybookScope(Base, TimestampMixin):
    """Assigns a Playbook to one or more applicable classes.

    Zero scope rows for a playbook = applies everywhere. A row with both
    class refs NULL is a sentinel meaning the same — used by the package
    importer to track ownership of an everywhere-scoped playbook.
    """

    __tablename__ = "playbook_scope"
    __table_args__ = (
        UniqueConstraint(
            "playbook_id",
            "document_class_id",
            "dossier_class_id",
            name="uq_playbook_scope",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("playbook.id", ondelete="CASCADE"), index=True
    )
    document_class_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_class.id", ondelete="CASCADE")
    )
    dossier_class_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dossier_class.id", ondelete="CASCADE")
    )
    managed_by: Mapped[str | None] = mapped_column(String(128), index=True)

    playbook: Mapped[Playbook] = relationship(back_populates="scopes")
