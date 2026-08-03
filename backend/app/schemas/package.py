"""GrovePackage — single-YAML domain config for a Grove deployment.

A package declares all Grove-side configuration (document classes + their
properties + attached entity types, entity types, relationship definitions
+ states, dossier classes + their properties + linked document classes,
playbook scope + playbook skill content) so a team can keep the file in
their own repo and import it idempotently.

Cross-references are by **name** — no UUIDs in the YAML. The importer
resolves names to ids in dependency order.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.config import SLUG_RE


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─────────────────────────────────────────────────────────────
# Resource entries
# ─────────────────────────────────────────────────────────────
class PackagePropertyEntry(_Strict):
    name: str
    description: str | None = None
    schema: dict[str, Any] = Field(default_factory=dict)
    guidance: str | None = None
    manual: bool = False
    required: bool = False
    cardinality: Literal["one", "many"] = "one"
    schema_version: int = 1


class PackageDocumentClassEntry(_Strict):
    name: str
    slug: str | None = None
    description: str | None = None
    summarization_guidance: str | None = None
    classification_hints: str | None = None
    properties: list[PackagePropertyEntry] = Field(default_factory=list)
    # entity types attached to this document class, by entity-type name
    entity_types: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str | None) -> str | None:
        if v is not None and not SLUG_RE.match(v):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9_]{0,63}$ (lowercase alnum and underscores)"
            )
        return v


class PackageEntityTypeEntry(_Strict):
    name: str
    description: str | None = None
    guidance: str | None = None
    creation_mode: Literal["open", "review", "closed"] = "open"


RefType = Literal["document_class", "entity_type", "dossier_class"]


class PackageRelationshipRef(_Strict):
    type: RefType
    name: str


class PackageRelationshipStateEntry(_Strict):
    name: str
    description: str | None = None
    counts_as_active: bool = True


class PackageRelationshipDefinitionEntry(_Strict):
    name: str
    description: str | None = None
    source: PackageRelationshipRef
    target: PackageRelationshipRef
    cardinality: Literal["one", "many"] = "many"
    extraction_guidance: str | None = None
    discovery_guidance: str | None = None
    creation_mode: Literal["open", "review", "closed"] = "open"
    states: list[PackageRelationshipStateEntry] = Field(default_factory=list)


class PackageDossierDocumentClassLink(_Strict):
    document_class: str  # document class name
    required: bool = False
    cardinality: Literal["one", "many"] = "many"


class PackageDossierClassEntry(_Strict):
    name: str
    slug: str | None = None
    description: str | None = None
    guidance: str | None = None
    summarization_guidance: str | None = None
    classification_hints: str | None = None
    properties: list[PackagePropertyEntry] = Field(default_factory=list)
    document_classes: list[PackageDossierDocumentClassLink] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str | None) -> str | None:
        if v is not None and not SLUG_RE.match(v):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9_]{0,63}$ (lowercase alnum and underscores)"
            )
        return v


class PackageAnnotationEntry(_Strict):
    """Config-declared derived field over the relationship graph.

    `path` uses the annotation path language (`/` sequence, `*` closure,
    `|` alternation, `^` inverse — nothing more); `reduce` is a reducer name
    (`first`, `terminal`, `length`) or a mapping of output keys to reducers.
    Syntax, reducers and referenced relationship names are validated loudly
    at import.
    """

    name: str
    description: str | None = None
    path: str
    reduce: str | dict[str, str]
    materialize: bool = False


PlaybookKind = Literal["retrieval", "synthesis"]


class PackagePlaybookScopeEntry(_Strict):
    """Scope row referenced by class name (one of the two must be set, or both
    omitted to indicate 'applies everywhere')."""

    document_class: str | None = None
    dossier_class: str | None = None


class PackagePlaybookEntry(_Strict):
    kind: PlaybookKind
    name: str
    description: str
    content: str
    # Empty list = applies everywhere (no scope rows).
    scope: list[PackagePlaybookScopeEntry] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Top level
# ─────────────────────────────────────────────────────────────
class PackageSpec(_Strict):
    document_classes: list[PackageDocumentClassEntry] = Field(default_factory=list)
    entity_types: list[PackageEntityTypeEntry] = Field(default_factory=list)
    relationship_definitions: list[PackageRelationshipDefinitionEntry] = Field(default_factory=list)
    dossier_classes: list[PackageDossierClassEntry] = Field(default_factory=list)
    playbooks: list[PackagePlaybookEntry] = Field(default_factory=list)
    annotations: list[PackageAnnotationEntry] = Field(default_factory=list)


class PackageMetadata(_Strict):
    name: str
    description: str | None = None


class PackageInfo(_Strict):
    name: str
    version: str
    description: str | None = None
    author: str | None = None
    url: str | None = None


class GrovePackage(_Strict):
    apiVersion: Literal["grove.sinas.co/v1"] = "grove.sinas.co/v1"
    kind: Literal["GrovePackage"] = "GrovePackage"
    metadata: PackageMetadata
    package: PackageInfo
    spec: PackageSpec = Field(default_factory=PackageSpec)

    @field_validator("package")
    @classmethod
    def _name_matches(cls, v: PackageInfo, info: Any) -> PackageInfo:
        meta = info.data.get("metadata")
        if meta is not None and meta.name != v.name:
            raise ValueError("package.name must equal metadata.name")
        return v


# ─────────────────────────────────────────────────────────────
# Validate / preview / import responses
# ─────────────────────────────────────────────────────────────
class PackageValidateResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PackageDiff(BaseModel):
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class PackageImportResult(BaseModel):
    package: str
    version: str
    diff: PackageDiff
    warnings: list[str] = Field(default_factory=list)
