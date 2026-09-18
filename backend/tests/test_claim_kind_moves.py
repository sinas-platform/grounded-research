"""A kind that moves is recorded, and a test left behind is named.

`normalise_claim` keeps a test only for a claim of kind `test`, and the
revision path writes `claim_kind` while reaching `test` only when the patch
carries one. So a revision that changes the kind and says nothing about the
test leaves the object on the row, under a kind that no longer routes to the
test block. That state was found by reading stored rows; nothing reported it.

Run from the backend directory:
`python -m pytest tests/test_claim_kind_moves.py`
"""

from __future__ import annotations

import uuid

from app.services.query_runner import _apply_structure

PARTS = [{"label": "part one", "text": "what it asks"}]


class _Row:
    """The columns `_apply_structure` reads and writes."""

    def __init__(self, kind="test", test=None, seq=5):
        self.id = uuid.uuid4()
        self.sequence = seq
        self.claim_text = "The court stated the test."
        self.claim_kind = kind
        self.claim_type = "legal_principle"
        self.test = test
        self.section = "analysis"
        self.part_index = 0
        self.part_label = "part one"
        self.follows_from = None
        self.authority_label = None
        self.authority_tier = None
        self.jurisdiction_note = None
        self.currency_note = None


def _two_conditions():
    return {"name": "The two-condition test",
            "conditions": [{"text": "the first holds", "cumulative": True},
                           {"text": "the second holds", "cumulative": True}]}


def test_a_kind_that_leaves_test_with_its_object_intact_is_named():
    """The case this exists for: the patch changes the kind and says nothing
    about the test, so the object survives under a kind that cannot render
    it."""
    row = _Row(kind="test", test=_two_conditions())
    moved = _apply_structure(row, {"kind": "rule"}, PARTS, {})

    assert moved is not None
    assert moved["from"] == "test" and moved["to"] == "rule"
    assert moved["orphaned_test"] is True
    assert moved["test_before"] is True and moved["test_after"] is True
    assert moved["sequence"] == 5
    assert isinstance(row.test, dict), "the object is still on the row"


def test_a_kind_that_does_not_move_reports_nothing():
    row = _Row(kind="rule", test=None)
    assert _apply_structure(row, {"kind": "rule"}, PARTS, {}) is None


def test_a_patch_that_carries_a_new_test_is_not_an_orphan():
    """The kind moves and the patch resends the test, so `test` is rewritten
    with it. Recorded as a move, not as an orphan."""
    row = _Row(kind="test", test=_two_conditions())
    moved = _apply_structure(
        row, {"kind": "rule", "test": _two_conditions()}, PARTS, {})

    assert moved is not None and moved["orphaned_test"] is False
    assert moved["test_after"] is False, "kind rule keeps no test"


def test_an_added_claim_reports_no_move():
    """An added claim is normalised whole and has no previous kind to move
    from, so it is not a transition."""
    row = _Row(kind="rule", test=None)
    moved = _apply_structure(
        row, {"kind": "test", "test": _two_conditions(), "text": "x"},
        PARTS, {}, added=True)
    assert moved is None


def test_a_move_away_from_test_with_no_object_is_recorded_but_not_orphaned():
    row = _Row(kind="test", test=None)
    moved = _apply_structure(row, {"kind": "fact"}, PARTS, {})
    assert moved is not None
    assert moved["from"] == "test" and moved["to"] == "fact"
    assert moved["orphaned_test"] is False
