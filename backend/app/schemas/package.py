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
#: What a property MEANS to the engine, declared by the deployment on the
#: property itself. The engine used to find these by matching names — a
#: property whose name contained "date" was a date, one matching
#: "jurisdiction|country|member_?state" was a jurisdiction, a citation was
#: built from the first of ("case_number", "celex", "reference", "number")
#: that existed. A deployment naming its properties anything else got no
#: currency note, no jurisdiction note and no identifier in its citations,
#: silently. The role says what the engine does with the value; the name
#: stays the deployment's own.
PROPERTY_ROLES = (
    "date",              # when the source speaks: currency, ordering, "as at"
    "jurisdiction",      # whose law or authority the source belongs to
    "status",            # in force, repealed, superseded …
    "superseded_by",     # what replaced it, when status says it was replaced
    "alternate_identifier",  # a second citable identifier (a neutral one)
)

#: What an annotation MEANS to the engine. Same fault, same fix: the tier and
#: the issuing body were found by the literal names "authority_tier" and
#: "issuing_body".
ANNOTATION_ROLES = (
    "standing_tier",     # how high this source stands, 1 highest
    "issuing_body",      # who issued it
)


class PackagePropertyEntry(_Strict):
    name: str
    description: str | None = None
    # `schema` is what the package and the API say; the attribute is `schema_`
    # because BaseModel already owns `schema`.
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    guidance: str | None = None
    manual: bool = False
    required: bool = False
    cardinality: Literal["one", "many"] = "one"
    schema_version: int = 1
    #: What this property means to the engine, if anything. Absent is the
    #: normal case: most properties are the deployment's own business.
    engine_role: str | None = None

    @model_validator(mode="after")
    def _known_role(self):
        if self.engine_role is not None and self.engine_role not in PROPERTY_ROLES:
            raise ValueError(
                f"property {self.name!r} declares engine_role "
                f"{self.engine_role!r}; known roles are "
                f"{', '.join(PROPERTY_ROLES)}"
            )
        return self


class PackageDeclaredProperty(_Strict):
    """One header key a class reads directly instead of asking a model."""

    key: str
    property: str
    on_conflict: Literal["replace", "fill_only"] = "fill_only"


class PackageFilenameRule(_Strict):
    """One filename shape that means a document is of this class.

    `pattern` is a regular expression matched against the bare filename with
    `re.search`. `confidence` is how sure the deployment is: at or above the
    engine's write threshold the class is assigned outright, below it the
    rule is passed to the classifying model as a hint. `reason` is what the
    rule is FOR, in the deployment's own words — it is stored on the
    document and is the only record of why a class was assigned without a
    model ever reading the file.

    This is knowledge about a collection, not about documents in general: a
    regulator's register scheme, a feed's numeric ids. The engine carried
    five such patterns for years, mapped to one deployment's class names,
    and every other deployment got no rule hits and a model call per
    document with nothing saying why.
    """

    pattern: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, v: str) -> str:
        """Refused where it is written. A rule that does not compile would
        otherwise raise inside the ingestion path, per document, on a rung
        that is meant to be free."""
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"filename rule pattern does not compile: {exc}") from exc
        return v


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
    # What a claim citing a document of this class says about the source, in
    # the words a reader needs — "commentary", "not binding", whatever this
    # collection calls it. SGR prints it after the sentence and never invents
    # it: which kinds of source have to be flagged, and in what words, is
    # knowledge about a collection, exactly as the identifier shape is.
    #
    # Declaring a label is also what says a class may not carry a rule alone:
    # the drafter is shown the label above the passages and told that a
    # labelled source states what it says rather than what the rules are. A
    # class that declares none is unlabelled and carries rules.
    authority_label: str | None = Field(default=None, max_length=40)
    #: Whether a claim asserting a rule on a source of this class must NAME
    #: it in the sentence. A review of published answers asked for this structurally:
    #: where the source is one the field treats as authority, the claim must
    #: identify it so a reader can look it up, and a claim that does not is
    #: sent back. Which classes those are is the deployment's to say; the
    #: engine holds no list.
    naming_required: bool = False
    # How high a source of this class stands against the other classes of the
    # same deployment, as a rank: 1 stands highest, larger numbers stand
    # lower. Absent means UNRANKED, and an unranked class is inert — a source
    # of it neither satisfies the rule that a general proposition rests on the
    # highest-standing source available nor violates it.
    #
    # A rank rather than a list of class names in order, for two reasons. It
    # sits on the class, like `authority_label` and like `engine_role` on a
    # property, so adding a class is one edit in one place and a class cannot
    # be silently left out of an ordering written somewhere else. And it
    # admits TIES: two classes that stand equally share a number, which a
    # total ordering would have to break arbitrarily.
    #
    # The number is an ordinal and nothing else. The engine compares two of
    # them and never reads a meaning into the value, so a deployment may
    # number 1,2,3 or 10,20,30 and leave room to insert a class between two
    # others without renumbering its corpus.
    standing: int | None = Field(default=None, ge=1)
    # Whether documents of this class carry propositions — the statements
    # they establish, apply or decide — that ingestion extracts and retrieval
    # matches a question's hypotheses against. False by default: which
    # classes hold the propositions a researcher looks for is knowledge about
    # a collection, and an undeclared class is left alone.
    propositions: bool = False
    attribution_cues: list[str] = Field(default_factory=list)
    # Properties whose value a document of this class states about itself, in
    # its front matter. Each entry is {key, property, on_conflict}: which
    # header key feeds which declared property, and whether the header is the
    # source of truth for it or merely one source among others.
    #
    # Here rather than in the platform for the same reason the identifier
    # shape is. What a header key means, and whether a printed date beats an
    # extracted one, is knowledge about a collection. `on_conflict: replace`
    # says the document is right and a model that disagreed was wrong;
    # `fill_only`, the default, says fill a gap and leave what is there.
    declared_properties: list[PackageDeclaredProperty] = Field(
        default_factory=list)
    # How a document of this class is recognised from its filename alone.
    # Empty is the default and means it is not — see PackageFilenameRule.
    filename_rules: list[PackageFilenameRule] = Field(default_factory=list)
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
        # A header value mapped to a property the class does not have was
        # accepted here, stored, and then skipped at ingestion for every
        # document with nothing recording it: the same failure as above, with
        # the same cure.
        for d in self.declared_properties:
            if d.property not in declared:
                raise ValueError(
                    f"declared_properties maps {d.key!r} to {d.property!r}, "
                    f"which this class does not declare"
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


    @model_validator(mode="after")
    def _one_property_per_role(self):
        """A class naming two properties for the same role would leave the
        engine to choose at read time, which is the guessing this mechanism
        exists to remove, so it is refused where it is written."""
        seen: dict[str, str] = {}
        for prop in self.properties:
            role = getattr(prop, "engine_role", None)
            if not role:
                continue
            if role in seen:
                raise ValueError(
                    f"class {self.name!r} declares {seen[role]!r} and "
                    f"{prop.name!r} both as the {role!r} property"
                )
            seen[role] = prop.name
        return self

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


#: The roles the engine looks for, and nothing else. Each names what the
#: engine DOES with such an edge, never what a deployment calls it:
#:
#:   standing     — one issuing body outranks another. The engine uses it to
#:                  find the annotation that derives a source's tier: the
#:                  annotation whose path walks a standing relation is the
#:                  tier annotation, whatever it is named.
#:   supersession — one source replaces another. The engine uses it to tell a
#:                  reader that a cited source is recorded as superseded.
#:   full_text    — a document IS the full text of the subject it records.
#:                  The engine uses it to get from a cited document to the
#:                  subject other edges hang off.
#:
#: Adding a role here is a change to the engine, not to a deployment: a name
#: nothing reads would be a declaration that does nothing.
ENGINE_ROLES = ("standing", "supersession", "full_text")




class PackageRelationshipRoles(_Strict):
    """Which of this deployment's relationship definitions carry which
    engine-meaningful role.

    Engine features that need an edge of a particular MEANING used to find it
    by its name — `WHERE rd.name IN ('is_full_text_of', …)`, a walk over names
    beginning `ranks_higher_than`. A deployment that named its relations
    anything else got no supersession check and no tier, with no error and no
    log: the query simply matched nothing, which is byte-identical to a corpus
    that records no supersession. This block is where that knowledge belongs —
    the deployment says which of ITS relations mean what, and the engine reads
    the role.

    Every role is optional and every role takes a LIST, because a meaning can
    be spread over several definitions: a definition's name is its unique key,
    so a deployment needing the same meaning between two different pairs of
    types must declare two definitions, and both carry the role.

    An absent role disables the feature that needs it, loudly — see
    `app.services.relationship_roles`, which logs which role is missing and
    what stopped working. The one exception is `full_text`, which has a
    structural fallback that needs no name at all (a document→entity
    definition with cardinality "one"); that fallback is documented at its
    reader and is what the engine used before this block existed.
    """

    standing: list[str] = Field(default_factory=list)
    supersession: list[str] = Field(default_factory=list)
    full_text: list[str] = Field(default_factory=list)

    def names_for(self, role: str) -> list[str]:
        return list(getattr(self, role, []) or [])

    @model_validator(mode="after")
    def _one_role_per_definition(self):
        """A definition carrying two roles would leave the engine to guess
        which feature meant it, so it is refused where it is written rather
        than resolved arbitrarily at read time."""
        seen: dict[str, str] = {}
        for role in ENGINE_ROLES:
            for name in self.names_for(role):
                if not str(name).strip():
                    raise ValueError(f"role {role!r} lists an empty name")
                if name in seen:
                    raise ValueError(
                        f"{name!r} is declared under both {seen[name]!r} and "
                        f"{role!r}; a definition carries one role"
                    )
                seen[name] = role
        return self


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
    #: What this annotation means to the engine, if anything.
    engine_role: str | None = None

    @model_validator(mode="after")
    def _known_annotation_role(self):
        if (self.engine_role is not None
                and self.engine_role not in ANNOTATION_ROLES):
            raise ValueError(
                f"annotation {self.name!r} declares engine_role "
                f"{self.engine_role!r}; known roles are "
                f"{', '.join(ANNOTATION_ROLES)}"
            )
        return self
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
    # Additive and optional: this block did not exist, the schema forbids
    # extras, and deployment manifests live in client repos. A package that
    # omits it keeps working — see PackageRelationshipRoles for what each
    # absent role costs.
    relationship_roles: PackageRelationshipRoles = Field(
        default_factory=PackageRelationshipRoles)
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
