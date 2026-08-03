"""Unit tests for the annotation framework: path language, reducer
normalization, walk semantics, and package-level validation.

Pure logic plus a fake expander — the SQL expansion and materialization are
exercised against the real stack elsewhere, same convention as the runner
tests. Run from the backend directory:
`python -m pytest tests/test_annotations.py`
"""

import uuid

import pytest
from app.schemas.package import GrovePackage
from app.services.annotations import (
    AnnotationConfigError,
    Step,
    Walk,
    normalize_reduce,
    parse_path,
    reduce_walk,
    validate_definition,
    walk_path,
)
from app.services.package import validate_crossrefs


# ─────────────────────────────────────────────────────────────
# Path parsing
# ─────────────────────────────────────────────────────────────
def test_parse_single_hop():
    p = parse_path("issued_by")
    assert len(p.terms) == 1
    assert p.terms[0].steps == (Step("issued_by", inverse=False),)
    assert not p.terms[0].star


def test_parse_sequence_inverse_and_star():
    p = parse_path("issued_by/(^ranks_higher_than_court | ^ranks_higher_than_court_authority)*")
    assert len(p.terms) == 2
    first, second = p.terms
    assert first.steps == (Step("issued_by"),) and not first.star
    assert second.star
    assert second.steps == (
        Step("ranks_higher_than_court", inverse=True),
        Step("ranks_higher_than_court_authority", inverse=True),
    )
    assert p.names == {
        "issued_by",
        "ranks_higher_than_court",
        "ranks_higher_than_court_authority",
    }


def test_parse_star_on_plain_name():
    p = parse_path("cites*")
    assert p.terms[0].star and p.terms[0].steps == (Step("cites"),)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "issued_by/",
        "/issued_by",
        "issued_by//cites",
        "(a | b",
        "a | b",          # alternation only inside parentheses
        "(a / b | c)",    # sequences inside alternation are rejected
        "a**",
        "a^",
        "^",
        "a b",
        "a-b",
    ],
)
def test_parse_rejects_everything_outside_the_syntax(bad):
    with pytest.raises(AnnotationConfigError):
        parse_path(bad)


# ─────────────────────────────────────────────────────────────
# Reducers
# ─────────────────────────────────────────────────────────────
def test_normalize_reduce_accepts_known_forms():
    assert normalize_reduce("first") == {"": "first"}
    assert normalize_reduce({"depth": "length", "top": "terminal"}) == {
        "depth": "length",
        "top": "terminal",
    }


@pytest.mark.parametrize("bad", ["exists", "count", "path", "agg"])
def test_normalize_reduce_rejects_reserved(bad):
    with pytest.raises(AnnotationConfigError, match="reserved"):
        normalize_reduce(bad)


def test_normalize_reduce_rejects_unknown_and_empty():
    with pytest.raises(AnnotationConfigError, match="unknown reducer"):
        normalize_reduce("banana")
    with pytest.raises(AnnotationConfigError):
        normalize_reduce({})


def test_validate_definition_checks_relationship_names():
    validate_definition("a/b", "first", known_names={"a", "b"})
    with pytest.raises(AnnotationConfigError, match="unknown relationship"):
        validate_definition("a/b", "first", known_names={"a"})


# ─────────────────────────────────────────────────────────────
# Walk semantics
# ─────────────────────────────────────────────────────────────
def _ids(*ints):
    return {uuid.UUID(int=i) for i in ints}


def _graph_expander(edges: dict[tuple[str, bool], dict[uuid.UUID, set[uuid.UUID]]]):
    """Fake ExpandFn over an adjacency map keyed by (name, inverse)."""

    async def expand(frontier, steps):
        out = set()
        for step in steps:
            adj = edges.get((step.name, step.inverse), {})
            for node in frontier:
                out |= adj.get(node, set())
        return out

    return expand


@pytest.mark.asyncio
async def test_walk_sequence_records_each_step():
    doc, court, tribunal = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
    expand = _graph_expander(
        {
            ("issued_by", False): {doc: {court}},
            ("ranks_higher_than_court", True): {court: {tribunal}},
        }
    )
    path = parse_path("issued_by/(^ranks_higher_than_court)*")
    walk = await walk_path(doc, path, expand)
    assert walk.trace == [{court}, {tribunal}]
    assert walk.steps == 2


@pytest.mark.asyncio
async def test_walk_broken_path_keeps_what_was_reached():
    doc, court = uuid.UUID(int=1), uuid.UUID(int=2)
    expand = _graph_expander({("issued_by", False): {doc: {court}}})
    walk = await walk_path(doc, parse_path("issued_by/appealed_to"), expand)
    assert walk.trace == [{court}]


@pytest.mark.asyncio
async def test_walk_star_survives_cycles():
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    expand = _graph_expander({("cites", False): {a: {b}, b: {a}}})
    walk = await walk_path(a, parse_path("cites*"), expand)
    # a -> b, then b -> a is suppressed by the revisit guard.
    assert walk.trace == [{b}]


@pytest.mark.asyncio
async def test_walk_empty_start():
    expand = _graph_expander({})
    walk = await walk_path(uuid.UUID(int=1), parse_path("issued_by"), expand)
    assert walk.trace == []


# ─────────────────────────────────────────────────────────────
# Reduction
# ─────────────────────────────────────────────────────────────
def _resolve(node_id):
    return {"id": str(node_id), "name": f"n{node_id.int}"}


def test_reduce_single_reducer_wraps_in_value():
    w = Walk(trace=[_ids(2), _ids(3)])
    assert reduce_walk(w, {"": "first"}, _resolve) == {"value": _resolve(uuid.UUID(int=2))}


def test_reduce_named_map():
    w = Walk(trace=[_ids(2), _ids(3), _ids(4)])
    out = reduce_walk(w, {"depth": "length", "top": "terminal"}, _resolve)
    assert out == {"depth": 3, "top": _resolve(uuid.UUID(int=4))}


def test_reduce_empty_walk_is_absent():
    assert reduce_walk(Walk(trace=[]), {"": "terminal"}, _resolve) is None


def test_reduce_tie_break_is_deterministic():
    w = Walk(trace=[_ids(7, 5, 9)])
    out = reduce_walk(w, {"": "first"}, _resolve)
    assert out == {"value": _resolve(min(_ids(7, 5, 9), key=str))}


# ─────────────────────────────────────────────────────────────
# Package validation
# ─────────────────────────────────────────────────────────────
def _pkg(annotations):
    return GrovePackage.model_validate(
        {
            "apiVersion": "grove.sinas.co/v1",
            "kind": "GrovePackage",
            "metadata": {"name": "t"},
            "package": {"name": "t", "version": "0.0.1"},
            "spec": {
                "entity_types": [{"name": "Court"}],
                "document_classes": [{"name": "Decision"}],
                "relationship_definitions": [
                    {
                        "name": "issued_by",
                        "source": {"type": "document_class", "name": "Decision"},
                        "target": {"type": "entity_type", "name": "Court"},
                    }
                ],
                "annotations": annotations,
            },
        }
    )


def test_package_accepts_valid_annotation():
    errors, warnings = validate_crossrefs(
        _pkg(
            [{"name": "issuing_body", "path": "issued_by", "reduce": "first", "materialize": True}]
        )
    )
    assert errors == [] and warnings == []


def test_package_rejects_bad_syntax_and_reserved_reducer():
    errors, _ = validate_crossrefs(
        _pkg(
            [
                {"name": "broken", "path": "issued_by/", "reduce": "first"},
                {"name": "reserved", "path": "issued_by", "reduce": "count"},
            ]
        )
    )
    assert len(errors) == 2
    assert "broken" in errors[0] and "reserved" in errors[1]


def test_package_warns_on_external_relationship_name():
    errors, warnings = validate_crossrefs(
        _pkg([{"name": "chain", "path": "issued_by/appealed_to", "reduce": "terminal"}])
    )
    assert errors == []
    assert any("appealed_to" in w for w in warnings)


def test_package_rejects_duplicate_annotation_names():
    errors, _ = validate_crossrefs(
        _pkg(
            [
                {"name": "dup", "path": "issued_by", "reduce": "first"},
                {"name": "dup", "path": "issued_by", "reduce": "terminal"},
            ]
        )
    )
    assert any("more than once" in e for e in errors)
