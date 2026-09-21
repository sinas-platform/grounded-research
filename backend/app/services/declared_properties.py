"""Properties a document states about itself, read rather than asked about.

Front matter is parsed at upload and never consulted again. Ingestion puts the
document, header included, into a prompt and asks a model to return the
properties, so a value the document states outright arrives by transcription,
and a stored value can disagree with the document's own header. A code is
copied as it stands. A field the model has to decide about, such as which of
several dates in the body is the document's own, is where it goes wrong, and
that field is what recency and supersession are decided on.

This module is the deterministic half. It is pure: a header, a mapping and
what is already stored go in, a plan comes out, and the caller writes it. No
knowledge of what any key means lives here. Which header key feeds which
property, and whether the header is the better source for it, is the
deployment's to declare on the document class, for the same reason the shape
of an identifier is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: Written on every value this path produces. Distinct from `auto` so a value
#: read from the document can be told from one a model returned, which is the
#: whole point: without it the fix is invisible the moment it lands.
DECLARED_METHOD = "declared"

#: What a class may say about a property whose value its documents state.
#: `replace` where the header is the source of truth, which is the case for a
#: date the document prints in its own header. `fill_only` where it is merely
#: one source among others and an existing value should stand.
_ON_CONFLICT = ("replace", "fill_only")
_DEFAULT_ON_CONFLICT = "fill_only"


def stored_text(value) -> str | None:
    """A stored property value as the text a header value is compared with,
    or None when it is not one value. Pure.

    Values are written as `{"_": x}` so a scalar or a list fits the JSONB
    column, and that is the shape ingestion writes.
    The dict path is therefore left exactly as it was: changing how an
    existing row reads would change what the header replaces.

    What it adds is an answer for the shapes the column also accepts and the
    old reader could not take. `(value or {}).get("_")` on a bare string,
    number or list raised `AttributeError`, and the per-document isolation
    around ingestion turned that into a failed document with nothing to say
    why. A bare scalar is its own value. A bare list is not one value, so it
    is not compared and is never overwritten: the caller leaves it alone and
    reports it.
    """
    if isinstance(value, dict):
        return str(value.get("_", ""))
    if value is None:
        return ""
    if isinstance(value, list):
        return None
    return str(value)


@dataclass(frozen=True)
class Existing:
    """What is already stored for one property."""
    value: str
    method: str
    locked: bool


def unknown_targets(mapping: list[dict], declared: set[str] | dict) -> list[str]:
    """Properties a class maps header values to and no longer declares. Pure.

    Read off the mapping, not off the plan. A plan only names a property when
    the document states that header key, so a stale declaration on a document
    that does not state it would never be seen, and a class that lost a
    property would look like one whose documents simply lack the field.
    """
    return sorted({str(m.get("property") or "") for m in mapping}
                  - {""} - set(declared))


@dataclass(frozen=True)
class Held:
    """What the planner may compare, and what it must leave alone."""
    existing: dict[str, "Existing"]
    planned: list[dict]
    unreadable: list[str]
    unknown: list[str]


def read_held(mapping: list[dict], prop_ids: dict, rows: dict) -> Held:
    """The stored values this mapping compares, read the one way. Pure.

    `prop_ids` is property name to id for the class, `rows` is property id to
    the stored row for the document. Both callers, ingestion and the backfill
    script, go through here: they used to carry a copy each, and a fix to one
    left the other crashing on the same value.

    Only the properties the mapping names are read. Anything else the
    document holds is not this path's business, and a list on an unrelated
    property is a legitimate value, not something to report.

    A value that is not one value is left out of `existing` and its mapping
    entry is dropped from `planned`. Dropping only the first would read as
    "nothing stored", and the write would overwrite it. Unknown targets are
    dropped from `planned` for the same reason they are reported.
    """
    unknown = unknown_targets(mapping, prop_ids)
    existing: dict[str, Existing] = {}
    unreadable: list[str] = []
    for name in dict.fromkeys(str(m.get("property") or "") for m in mapping):
        if name not in prop_ids:
            continue
        r = rows.get(prop_ids[name])
        if r is None:
            continue
        text = stored_text(r.value)
        if text is None:
            unreadable.append(name)
            continue
        existing[name] = Existing(
            value=text, method=r.method, locked=bool(r.locked))
    skip = set(unreadable) | set(unknown)
    planned = [m for m in mapping if m.get("property") not in skip]
    return Held(existing, planned, unreadable, unknown)


@dataclass(frozen=True)
class Write:
    property: str
    value: str
    method: str
    confidence: float
    #: The value being replaced, or None on a first write. Carried so the
    #: caller can put it in `reason` and the operation stays reversible.
    replaces: str | None


@dataclass
class Plan:
    write: list[Write] = field(default_factory=list)
    #: Counted rather than silently dropped, for each reason separately: a run
    #: that changed nothing because everything was locked and a run that
    #: changed nothing because the header agreed are not the same run.
    kept_manual: list[str] = field(default_factory=list)
    kept_locked: list[str] = field(default_factory=list)
    kept_existing: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"written": len(self.write),
                "replaced": sum(1 for w in self.write if w.replaces is not None),
                "kept_manual": len(self.kept_manual),
                "kept_locked": len(self.kept_locked),
                "kept_existing": len(self.kept_existing),
                "unchanged": len(self.unchanged)}


def replacement_reason(on_date: str, prior: str | None) -> str:
    """The `reason` a written value carries.

    `backfill <date> <operation>; prior value: {"_": "<old>"}`. A reader who
    finds a replaced value knows where to look for the one it replaced, and
    any backfill that writes the same shape is reversible the same way, rather
    than each one needing its own convention.
    """
    stem = f"backfill {on_date} declared_properties"
    if prior is None:
        return stem
    return f"{stem}; prior value: {json.dumps({'_': prior}, ensure_ascii=False)}"


def plan_declared_values(
    header: dict,
    mapping: list[dict],
    existing: dict[str, Existing],
) -> Plan:
    """What to write for one document. Pure.

    `header` is the parsed front matter, `mapping` is the class's
    `declared_properties`, and `existing` is what is stored today keyed by
    property name.

    The precedence is the answer to the only question this design had to
    settle. A `manual` value is a person's decision and a locked one was locked
    on purpose, while an `auto` value is a model's reading of the document the
    header belongs to. So a person's decision outranks a header, a header
    outranks a model, and nothing is overwritten quietly.
    """
    plan = Plan()
    for entry in mapping:
        prop = entry.get("property")
        key = entry.get("key")
        on_conflict = entry.get("on_conflict") or _DEFAULT_ON_CONFLICT
        if on_conflict not in _ON_CONFLICT:
            # Refused rather than defaulted. A typo silently becoming
            # `fill_only` would stop replacing the field a must-have rests on
            # and look exactly like a class that had asked for that.
            raise ValueError(
                f"on_conflict {on_conflict!r} on property {prop!r} is not one "
                f"of {_ON_CONFLICT}")
        if not prop or not key:
            continue
        raw = header.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue

        held = existing.get(prop)
        if held is None:
            plan.write.append(Write(prop, value, DECLARED_METHOD, 1.0, None))
            continue
        if held.locked:
            plan.kept_locked.append(prop)
            continue
        if held.method == "manual":
            plan.kept_manual.append(prop)
            continue
        if held.value == value:
            # Rewriting a value to itself would churn the row and lose the
            # method that records where the current one came from.
            plan.unchanged.append(prop)
            continue
        if on_conflict == "fill_only" or held.method == DECLARED_METHOD:
            plan.kept_existing.append(prop)
            continue
        plan.write.append(Write(prop, value, DECLARED_METHOD, 1.0, held.value))
    return plan
