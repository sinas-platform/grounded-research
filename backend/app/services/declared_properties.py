"""Properties a document states about itself, read rather than asked about.

Front matter is parsed at upload and never consulted again. Ingestion puts the
document, header included, into a prompt and asks a model to return the
properties, so a value the document states outright arrives by transcription.
On one corpus that cost 693 disagreements on `decision_date` out of 20,688,
3.3%, while `language` disagreed 0 times in 14,318 and CELEX 0 in 5,215. Codes
the model copies; the one field it has to decide about is the one it gets
wrong, and that field is what recency and supersession are decided on.

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


@dataclass(frozen=True)
class Existing:
    """What is already stored for one property."""
    value: str
    method: str
    locked: bool


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

    Deliberately the shape the 7 September joined-case split already used:
    `backfill <date> <operation>; prior value: {"_": "<old>"}`. Reusing it
    means a reader who finds a replaced value knows where to look for the one
    it replaced, and that this operation is reversible the same way that one
    was, rather than having to learn a second convention.
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
    settle, and the corpus settled it: of the 693 stored dates that disagree
    with their header, every one is `auto` and none is locked, while all 689
    `manual` values sit elsewhere. So a person's decision outranks a header, a
    header outranks a model, and nothing is overwritten quietly.
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
