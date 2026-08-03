"""Annotations: config-declared derived fields computed from the relationship graph.

An annotation definition is name + property path + reducer + materialize flag,
declared in the GrovePackage. The path walks relationship instances by
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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnnotationDefinition,
    AnnotationValue,
    Entity,
    Relationship,
    RelationshipDefinition,
    RelationshipState,
)

REDUCERS = {"first", "terminal", "length"}
RESERVED_REDUCERS = {"exists", "count", "path", "agg"}
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
            "relationship definition(s) not present in this Grove: " + ", ".join(missing)
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
                select(Entity.id, Entity.name).where(Entity.id.in_(list(reached)))
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
