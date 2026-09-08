"""SgrPackage — single-YAML domain config for a SGR deployment.

A package declares all SGR-side configuration (document classes + their
properties + attached entity types, entity types, relationship definitions
+ states, dossier classes + their properties + linked document classes,
playbook scope + playbook skill content) so a team can keep the file in
their own repo and import it idempotently.

Cross-references are by **name** — no UUIDs in the YAML. The importer
resolves names to ids in dependency order.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

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
    # Naming check: the property whose value identifies a document of this
    # class, and the words that mark a claim as attributing to it. Declaring
    # both switches the check on for the class; declaring neither leaves it
    # off, which is the default.
    identifier_property: str | None = None
    # The shape of that identifier, as a regular expression whose CAPTURE
    # GROUPS are the comparison key. SGR cannot know what a case number, a
    # merger reference or an invoice number looks like without naming a
    # deployment, so the shape is declared here beside the cues. Capture the
    # parts that identify and leave out the parts that decorate: two values
    # differing only by what is not captured compare equal, which is how a
    # deployment says a procedural suffix does not change identity without
    # SGR knowing what a suffix is.
    identifier_pattern: str | None = None
    # The property holding the document's name, for claims that name a source
    # in prose rather than by its identifier. Optional and separate: a class
    # may be identified by number and never named in words, or the reverse.
    name_property: str | None = None
    attribution_cues: list[str] = Field(default_factory=list)
    properties: list[PackagePropertyEntry] = Field(default_factory=list)
    # entity types attached to this document class, by entity-type name
    entity_types: list[str] = Field(default_factory=list)

    @field_validator("identifier_pattern")
    @classmethod
    def _pattern_compiles(cls, v: str | None) -> str | None:
        """Refused at import rather than at the first answer that uses it.

        A pattern that does not compile would otherwise raise inside a check
        that is deliberately best-effort, where it would be swallowed and the
        class would look like one that had opted out.
        """
        if v is None:
            return v
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"identifier_pattern is not a regex: {exc}") from exc
        return v

    @model_validator(mode="after")
    def _points_at_a_declared_property(self):
        """A property named by this class must be one this class declares.

        Nothing checked this, and the cost is on record. The Advocate General
        Opinion class declared `identifier_property: case_number` and defined
        no properties at all, so the naming check's join matched nothing and
        293 documents were invisible to it. There was no error and no warning:
        a class pointing at a property it does not define looks, from every
        check downstream, exactly like a class that opted out.

        Safe to validate here because the importer reconciles a class's
        properties by replacing the set rather than merging into it, so the
        entry carries the whole list and a name absent from it is absent after
        the import too.
        """
        declared = {p.name for p in self.properties}
        for field in ("identifier_property", "name_property"):
            named = getattr(self, field)
            if named and named not in declared:
                raise ValueError(
                    f"{field} is {named!r}, which this class does not declare"
                )
        return self

    @model_validator(mode="after")
    def _shape_declared_with_property(self):
        """A class that opts into the correspondence check must say what its
        identifiers look like.

        Declaring `identifier_property` and no `identifier_pattern` leaves the
        check unable to run, and a check that cannot run returns no findings,
        which is byte-identical to a clean answer. Refusing it here removes the
        steady state of that failure; the deploy window between a migration and
        a re-import is covered by the gate announcing it in `issues`.

        Not defaulted, deliberately. A default would mean guessing an
        identifier's shape, which is the deployment knowledge this field exists
        to keep out of the platform.
        """
        if self.identifier_property and not self.identifier_pattern:
            raise ValueError(
                "identifier_property is declared without identifier_pattern, "
                "so the correspondence check cannot run for this class"
            )
        return self

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


PlaybookKind = Literal["retrieval", "synthesis", "validation"]


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


class SgrPackage(_Strict):
    # The pre-rename spelling is still accepted: deployment manifests live in
    # client repos and must keep installing across the rename. New exports emit
    # the sgr form.
    apiVersion: Literal["sgr.sinas.co/v1", "grove.sinas.co/v1"] = "sgr.sinas.co/v1"
    kind: Literal["SgrPackage", "GrovePackage"] = "SgrPackage"
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
