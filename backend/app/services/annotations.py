"""Annotations: config-declared derived fields computed from the relationship graph.

An annotation definition is name + property path + reducer + materialize flag,
declared in the SgrPackage. The path walks relationship instances by
definition name; the reducer folds the walk into a value. Everything domain-
specific lives in the package config — this module is pure mechanism.

Path syntax (deliberately small — anything else is rejected at config load):

    path  := term ('/' term)*          sequence of hops
    term  := atom '*'?                 '*' repeats the hop to a fixpoint
    atom  := step | '(' step ('|' step)* ')'
    step  := '^'? NAME                 '^' walks the relationship inverted

NAME is a relationship-definition name. Alternation members are single
(possibly inverted) names — nested sequences inside `(…)` are rejected.

Reducers (fixed, domain-free set): `first`, `terminal`, `length`. The reduce
field is either one reducer (the annotation value is that fold) or a mapping
of output keys to reducers (the value is an object). `exists`, `count`,
`path` and `agg` are reserved behind the same interface and rejected until
implemented.

Walk semantics: expanding a term advances a frontier of node ids; the trace
records every successful expansion step. `first` = a node from the first
non-empty frontier, `terminal` = a node from the last one, `length` = the
number of successful expansion steps (a `*` term counts one per iteration).
Deterministic tie-break everywhere: lowest id wins.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnnotationDefinition,
    AnnotationValue,
    Document,
    Entity,
    Relationship,
    RelationshipDefinition,
    RelationshipState,
)

REDUCERS = {"first", "terminal", "length"}
RESERVED_REDUCERS = {"exists", "count", "path", "agg"}
# The dual of an identity definition's cardinality "one": at most this many
# in-service documents may claim an entity THROUGH ONE identity definition
# before that identity stops being one. Counted per definition, so an entity
# with a regulatory text, a court text and a transcript — one document each
# through three identity definitions — is three healthy identities, not a
# hub. Two rather than one because a re-ingestion can legitimately leave a
# second in-service document behind (observed: the same decision ingested
# under two filenames). Past the bound the entity is an entity-resolution
# failure, and computing annotations from it would smear one entity's facts
# across every document resolved into it.
MAX_IDENTITY_DOCS = 2
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_STAR_ITERATIONS = 64


class AnnotationConfigError(ValueError):
    """A definition that must be rejected loudly at config load."""


# ─────────────────────────────────────────────────────────────
# Path parsing
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Step:
    name: str
    inverse: bool = False


@dataclass(frozen=True)
class Term:
    steps: tuple[Step, ...]  # alternation members
    star: bool = False


@dataclass(frozen=True)
class Path:
    terms: tuple[Term, ...]

    @property
    def names(self) -> set[str]:
        return {s.name for t in self.terms for s in t.steps}


class _Tokens:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def peek(self) -> str | None:
        self._skip_ws()
        return self.text[self.pos] if self.pos < len(self.text) else None

    def take(self, ch: str) -> None:
        self._skip_ws()
        if self.peek() != ch:
            raise AnnotationConfigError(
                f"expected '{ch}' at position {self.pos} in path '{self.text}'"
            )
        self.pos += 1

    def name(self) -> str:
        self._skip_ws()
        m = _NAME_RE.match(self.text, self.pos)
        if not m:
            raise AnnotationConfigError(
                f"expected a relationship name at position {self.pos} in path '{self.text}'"
            )
        self.pos = m.end()
        return m.group(0)


def parse_path(text: str) -> Path:
    """Parse a path expression; raise AnnotationConfigError on anything
    outside the documented syntax."""
    if not text or not text.strip():
        raise AnnotationConfigError("path is empty")
    toks = _Tokens(text)
    terms: list[Term] = [_parse_term(toks)]
    while toks.peek() == "/":
        toks.take("/")
        terms.append(_parse_term(toks))
    if toks.peek() is not None:
        raise AnnotationConfigError(
            f"unexpected '{toks.peek()}' at position {toks.pos} in path '{text}'"
        )
    return Path(terms=tuple(terms))


def _parse_step(toks: _Tokens) -> Step:
    inverse = False
    if toks.peek() == "^":
        toks.take("^")
        inverse = True
    return Step(name=toks.name(), inverse=inverse)


def _parse_term(toks: _Tokens) -> Term:
    if toks.peek() == "(":
        toks.take("(")
        steps = [_parse_step(toks)]
        while toks.peek() == "|":
            toks.take("|")
            steps.append(_parse_step(toks))
        toks.take(")")
    else:
        steps = [_parse_step(toks)]
    star = False
    if toks.peek() == "*":
        toks.take("*")
        star = True
    return Term(steps=tuple(steps), star=star)


# ─────────────────────────────────────────────────────────────
# Reducer normalization
# ─────────────────────────────────────────────────────────────
def normalize_reduce(reduce: str | dict) -> dict[str, str]:
    """Return {output_key: reducer}. A bare string means a single unnamed
    output (key ''). Rejects unknown and reserved reducers loudly."""
    if isinstance(reduce, str):
        mapping = {"": reduce}
    elif isinstance(reduce, dict) and reduce:
        mapping = {str(k): str(v) for k, v in reduce.items()}
    else:
        raise AnnotationConfigError("reduce must be a reducer name or a non-empty mapping")
    for r in mapping.values():
        if r in RESERVED_REDUCERS:
            raise AnnotationConfigError(
                f"reducer '{r}' is reserved and not implemented yet"
            )
        if r not in REDUCERS:
            raise AnnotationConfigError(
                f"unknown reducer '{r}' (known: {', '.join(sorted(REDUCERS))})"
            )
    return mapping


def validate_definition(path_text: str, reduce: str | dict, known_names: set[str]) -> Path:
    """Full config-load validation: syntax, reducers, relationship names.
    Returns the parsed path. Raises AnnotationConfigError with a message
    naming exactly what is wrong."""
    path = parse_path(path_text)
    normalize_reduce(reduce)
    unknown = sorted(path.names - known_names)
    if unknown:
        raise AnnotationConfigError(
            "path references unknown relationship definition(s): " + ", ".join(unknown)
        )
    return path


# ─────────────────────────────────────────────────────────────
# Walk
# ─────────────────────────────────────────────────────────────
# expand(frontier_ids, steps) -> {target_id, ...} across all alternation members
ExpandFn = Callable[[set[uuid.UUID], tuple[Step, ...]], Awaitable[set[uuid.UUID]]]


@dataclass
class Walk:
    trace: list[set[uuid.UUID]] = field(default_factory=list)

    @property
    def steps(self) -> int:
        return len(self.trace)

    @property
    def first(self) -> set[uuid.UUID]:
        return self.trace[0] if self.trace else set()

    @property
    def terminal(self) -> set[uuid.UUID]:
        return self.trace[-1] if self.trace else set()


async def walk_path(subject_id: uuid.UUID, path: Path, expand: ExpandFn) -> Walk:
    """Advance a frontier through the path's terms, recording every successful
    expansion. A `*` term iterates to a fixpoint (empty next frontier), with
    a hard iteration cap as a cycle guard."""
    walk = Walk()
    frontier = {subject_id}
    for term in path.terms:
        if term.star:
            visited = set(frontier)  # cycle guard: never revisit any node
            for _ in range(_MAX_STAR_ITERATIONS):
                nxt = await expand(frontier, term.steps)
                nxt -= visited
                if not nxt:
                    break
                visited |= nxt
                walk.trace.append(nxt)
                frontier = nxt
        else:
            nxt = await expand(frontier, term.steps)
            if not nxt:
                return walk  # path broken: reducers act on what was reached
            walk.trace.append(nxt)
            frontier = nxt
    return walk


def reduce_walk(
    walk: Walk, mapping: dict[str, str], resolve: Callable[[uuid.UUID], dict]
) -> dict | None:
    """Fold a walk into the annotation value. Returns None when the walk
    reached nothing (the annotation is absent, not zero)."""
    if not walk.trace:
        return None

    def _one(ids: set[uuid.UUID]) -> dict:
        return resolve(min(ids, key=str))

    out: dict[str, object] = {}
    for key, reducer in mapping.items():
        if reducer == "first":
            out[key] = _one(walk.first)
        elif reducer == "terminal":
            out[key] = _one(walk.terminal)
        elif reducer == "length":
            out[key] = walk.steps
    if list(out) == [""]:
        return {"value": out[""]}
    return out


# ─────────────────────────────────────────────────────────────
# SQL expansion + compute
# ─────────────────────────────────────────────────────────────
async def _definition_ids(session: AsyncSession, names: set[str]) -> dict[str, uuid.UUID]:
    rows = (
        await session.execute(
            select(RelationshipDefinition.name, RelationshipDefinition.id).where(
                RelationshipDefinition.name.in_(names)
            )
        )
    ).all()
    return {name: rid for name, rid in rows}


def make_expander(session: AsyncSession, def_ids: dict[str, uuid.UUID]) -> ExpandFn:
    """Expand a frontier one hop over active relationship instances.
    Forward steps go source→target; inverted steps target→source. All
    alternation members are resolved in one query per direction."""

    async def expand(frontier: set[uuid.UUID], steps: tuple[Step, ...]) -> set[uuid.UUID]:
        if not frontier:
            return set()
        out: set[uuid.UUID] = set()
        for inverse in (False, True):
            ids = [def_ids[s.name] for s in steps if s.inverse == inverse and s.name in def_ids]
            if not ids:
                continue
            src_col = Relationship.target_id if inverse else Relationship.source_id
            dst_col = Relationship.source_id if inverse else Relationship.target_id
            stmt = (
                select(dst_col)
                .outerjoin(
                    RelationshipState,
                    RelationshipState.id == Relationship.current_state_id,
                )
                .where(
                    Relationship.relationship_definition_id.in_(ids),
                    src_col.in_(list(frontier)),
                    (Relationship.current_state_id.is_(None))
                    | (RelationshipState.counts_as_active.is_(True)),
                )
            )
            out.update((await session.execute(stmt)).scalars().all())
        return out

    return expand


async def compute_annotations(
    session: AsyncSession,
    subject_ids: list[uuid.UUID],
    definitions: list[AnnotationDefinition],
) -> dict[uuid.UUID, dict[str, dict | None]]:
    """Compute the given definitions for the given subjects. Returns
    {subject_id: {annotation_name: value | None}}. Node values are rendered
    as {id, name} using the entity register (non-entity nodes fall back to
    the bare id)."""
    parsed: list[tuple[AnnotationDefinition, Path, dict[str, str]]] = []
    all_names: set[str] = set()
    for d in definitions:
        path = parse_path(d.path)
        parsed.append((d, path, normalize_reduce(d.reduce)))
        all_names |= path.names

    def_ids = await _definition_ids(session, all_names)
    missing = sorted(all_names - set(def_ids))
    if missing:
        raise AnnotationConfigError(
            "relationship definition(s) not present in this SGR: " + ", ".join(missing)
        )
    expand = make_expander(session, def_ids)

    walks: dict[tuple[uuid.UUID, str], Walk] = {}
    reached: set[uuid.UUID] = set()
    for subject_id in subject_ids:
        for d, path, _ in parsed:
            w = await walk_path(subject_id, path, expand)
            walks[(subject_id, d.name)] = w
            for frontier in w.trace:
                reached |= frontier

    names_by_id: dict[uuid.UUID, str] = {}
    if reached:
        rows = (
            await session.execute(
                select(Entity.id, Entity.canonical_form).where(Entity.id.in_(list(reached)))
            )
        ).all()
        names_by_id = {eid: name for eid, name in rows}

    def resolve(node_id: uuid.UUID) -> dict:
        out = {"id": str(node_id)}
        if node_id in names_by_id:
            out["name"] = names_by_id[node_id]
        return out

    result: dict[uuid.UUID, dict[str, dict | None]] = {}
    for subject_id in subject_ids:
        per_subject: dict[str, dict | None] = {}
        for d, _, mapping in parsed:
            per_subject[d.name] = reduce_walk(walks[(subject_id, d.name)], mapping, resolve)
        result[subject_id] = per_subject
    return result


async def materialize(
    session: AsyncSession,
    subject_ids: list[uuid.UUID],
    definitions: list[AnnotationDefinition] | None = None,
) -> int:
    """Compute and upsert AnnotationValue rows for materialized definitions.
    Returns the number of values written. A path that reaches nothing
    deletes any stale stored value."""
    if definitions is None:
        definitions = (
            await session.execute(
                select(AnnotationDefinition).where(AnnotationDefinition.materialize.is_(True))
            )
        ).scalars().all()
    definitions = [d for d in definitions if d.materialize]
    if not definitions or not subject_ids:
        return 0

    computed = await compute_annotations(session, subject_ids, definitions)

    existing = (
        await session.execute(
            select(AnnotationValue).where(
                AnnotationValue.annotation_definition_id.in_([d.id for d in definitions]),
                AnnotationValue.subject_id.in_(subject_ids),
            )
        )
    ).scalars().all()
    by_key = {(v.annotation_definition_id, v.subject_id): v for v in existing}

    written = 0
    for d in definitions:
        for subject_id in subject_ids:
            value = computed[subject_id][d.name]
            row = by_key.get((d.id, subject_id))
            if value is None:
                if row is not None:
                    await session.delete(row)
                continue
            if row is None:
                session.add(
                    AnnotationValue(
                        annotation_definition_id=d.id, subject_id=subject_id, value=value
                    )
                )
                written += 1
            elif row.value != value:
                row.value = value
                written += 1
    await session.flush()
    return written


# ─────────────────────────────────────────────────────────────
# Ordering (generic sort over annotation values)
# ─────────────────────────────────────────────────────────────
def _value_at(annotations: dict[str, dict | None], dotted: str):
    """Resolve 'annotation.key.subkey' against a subject's computed
    annotations; None when anything along the way is absent."""
    name, _, rest = dotted.partition(".")
    node: object = annotations.get(name)
    for part in rest.split(".") if rest else []:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


@dataclass(frozen=True)
class OrderKey:
    """One sort criterion: a dotted path into annotation values, ascending
    or descending. Subjects missing the value sort last either way."""

    by: str
    direction: str = "asc"  # asc | desc


def order_subjects(
    subject_ids: list[uuid.UUID],
    annotations: dict[uuid.UUID, dict[str, dict | None]],
    group_by: str | None = None,
    precedence: list[str] | None = None,
    then_by: list[OrderKey] | None = None,
) -> list[uuid.UUID]:
    """Order subjects by their annotation values. Pure and deterministic.

    `group_by` is a dotted path (e.g. 'jurisdiction.value.name') whose value
    buckets the subjects; `precedence` lists bucket values first, in the given
    order — the caller computes this list per request, which is where any
    domain rule lives. Unlisted buckets follow, alphabetically; subjects
    without a bucket value come last. Within a bucket, `then_by` criteria
    apply in order; the final tie-break is the subject id, so equal inputs
    always produce equal output.
    """
    prec_index = {v: i for i, v in enumerate(precedence or [])}
    keys = then_by or []

    def sort_key(sid: uuid.UUID):
        ann = annotations.get(sid, {})
        parts: list[tuple] = []
        if group_by is not None:
            bucket = _value_at(ann, group_by)
            if bucket is not None and str(bucket) in prec_index:
                parts.append((0, prec_index[str(bucket)], ""))
            elif bucket is not None:
                parts.append((1, 0, str(bucket)))
            else:
                parts.append((2, 0, ""))
        for k in keys:
            v = _value_at(ann, k.by)
            if v is None:
                parts.append((1, "", ""))  # missing sorts last in both directions
            elif isinstance(v, int | float) and not isinstance(v, bool):
                num = -v if k.direction == "desc" else v
                parts.append((0, "n", num))
            else:
                s = str(v)
                if k.direction == "desc":
                    s = "".join(chr(0x10FFFF - ord(c)) for c in s)
                parts.append((0, "s", s))
        parts.append((str(sid),))
        return tuple(parts)

    return sorted(subject_ids, key=sort_key)


# ─────────────────────────────────────────────────────────────
# Response surfacing: annotations for the entities documents stand for
# ─────────────────────────────────────────────────────────────


def _pick_subjects(
    pairs: list[tuple[uuid.UUID, uuid.UUID]], broken: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """One subject entity per document from its identity edges, or none.

    Broken identities are dropped before the pick, so a document holding
    both a healthy identity edge and a hub one keeps the healthy subject
    rather than losing its annotations to the hub. Among what remains the
    pick is deterministic: lowest entity id wins, as everywhere else.

    Pure: pairs and a set in, a mapping out.
    """
    subject_by_doc: dict[uuid.UUID, uuid.UUID] = {}
    for doc_id, ent_id in sorted(pairs, key=lambda p: (str(p[0]), str(p[1]))):
        if ent_id in broken:
            continue
        subject_by_doc.setdefault(doc_id, ent_id)
    return subject_by_doc


async def annotations_for_documents(
    session: AsyncSession,
    document_ids: list[uuid.UUID],
    definitions: list[AnnotationDefinition],
) -> dict[uuid.UUID, dict]:
    """Annotations for the case entities the given documents are the full
    text of, keyed by document id — the read-side surface for result and
    answer endpoints (?annotate=).

    A document stands for the entity it is linked to through an active
    IDENTITY edge: a document→entity definition with cardinality "one"
    (is_full_text_of and friends). Citation-shaped definitions are not
    subject edges — a document that cites five cases is not any one of
    them, and the old any-edge pick silently elected the lowest id.
    Cardinality "one" IS the identity declaration: a definition meant as
    an identity but declared with the default "many" has not declared one,
    and its documents get no subject — that is the contract, not a gap, so
    a deployment writing an identity definition must say cardinality: one.
    Documents with no such edge, and subjects whose path reaches nothing,
    report every annotation as None — absence is an answer, not an error.

    An entity claimed by more than MAX_IDENTITY_DOCS in-service documents
    THROUGH ONE identity definition yields no subject either. Cardinality
    "one" already says each document is the full text of one entity; the
    dual bound is the same schema intent read from the entity's side, per
    definition — so a decision with a regulatory text, a court text and a
    transcript through three identity definitions is three healthy
    identities, while a hub named "Decision" claimed by 1,135 documents
    through one definition is a resolution failure whose annotations would
    smear one entity's facts across unrelated documents. Absence over
    arbitrary, again.

    Values come from the materialized store where present; anything else
    (non-materialized definitions, subjects not yet backfilled) is
    computed on the fly. A subject whose path genuinely reaches nothing
    has no stored row either, so it is recomputed on every read — cheap,
    but a reason to keep materialized backfills current.
    """
    out: dict[uuid.UUID, dict] = {
        did: {"subject_entity_id": None, "values": {d.name: None for d in definitions}}
        for did in document_ids
    }
    if not document_ids or not definitions:
        return out

    pairs = (
        await session.execute(
            select(Relationship.source_id, Relationship.target_id)
            .join(
                RelationshipDefinition,
                RelationshipDefinition.id == Relationship.relationship_definition_id,
            )
            .outerjoin(
                RelationshipState,
                RelationshipState.id == Relationship.current_state_id,
            )
            .where(
                RelationshipDefinition.source_ref_type == "document_class",
                RelationshipDefinition.target_ref_type == "entity_type",
                RelationshipDefinition.cardinality == "one",
                Relationship.source_id.in_(document_ids),
                (Relationship.current_state_id.is_(None))
                | (RelationshipState.counts_as_active.is_(True)),
            )
        )
    ).all()

    # Broken identities out before the pick, so a document holding both a
    # healthy identity edge and a hub one keeps the healthy subject. The
    # count is global — how many documents in the whole store share this
    # identity — not a count within the requested batch, because an entity's
    # brokenness is a property of the entity, not of who is asking.
    candidate_ids = sorted({ent_id for _, ent_id in pairs}, key=str)
    broken: set[uuid.UUID] = set()
    if candidate_ids:
        # Only documents in service count toward an identity. A superseded
        # ingest leaves the same file behind as a staged duplicate with its
        # edges intact, and counting it would call the entity broken for
        # having been re-ingested — the same filter retrieval applies.
        counts = (
            await session.execute(
                select(
                    Relationship.target_id,
                    Relationship.relationship_definition_id,
                    func.count(Relationship.source_id.distinct()),
                )
                .join(
                    RelationshipDefinition,
                    RelationshipDefinition.id == Relationship.relationship_definition_id,
                )
                .join(Document, Document.id == Relationship.source_id)
                .outerjoin(
                    RelationshipState,
                    RelationshipState.id == Relationship.current_state_id,
                )
                .where(
                    RelationshipDefinition.source_ref_type == "document_class",
                    RelationshipDefinition.target_ref_type == "entity_type",
                    RelationshipDefinition.cardinality == "one",
                    Relationship.target_id.in_(candidate_ids),
                    Document.duplicate_of_id.is_(None),
                    Document.staged.is_(False),
                    (Relationship.current_state_id.is_(None))
                    | (RelationshipState.counts_as_active.is_(True)),
                )
                .group_by(
                    Relationship.target_id,
                    Relationship.relationship_definition_id,
                )
            )
        ).all()
        broken = {eid for eid, _rdef, n in counts if n > MAX_IDENTITY_DOCS}

    subject_by_doc = _pick_subjects(pairs, broken)
    if not subject_by_doc:
        return out
    subjects = sorted(set(subject_by_doc.values()), key=str)

    # materialized values first; compute whatever the store doesn't cover
    values: dict[tuple[uuid.UUID, str], dict | None] = {}
    materialized = [d for d in definitions if d.materialize]
    if materialized:
        by_def_id = {d.id: d.name for d in materialized}
        rows = (
            await session.execute(
                select(AnnotationValue).where(
                    AnnotationValue.annotation_definition_id.in_(list(by_def_id)),
                    AnnotationValue.subject_id.in_(subjects),
                )
            )
        ).scalars().all()
        for v in rows:
            values[(v.subject_id, by_def_id[v.annotation_definition_id])] = v.value

    missing_defs = [
        d for d in definitions
        if any((s, d.name) not in values for s in subjects)
    ]
    if missing_defs:
        computed = await compute_annotations(session, subjects, missing_defs)
        for s in subjects:
            for d in missing_defs:
                values.setdefault((s, d.name), computed[s][d.name])

    for did, subject in subject_by_doc.items():
        out[did] = {
            "subject_entity_id": subject,
            "values": {d.name: values.get((subject, d.name)) for d in definitions},
        }
    return out
