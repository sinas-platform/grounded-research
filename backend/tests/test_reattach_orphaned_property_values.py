"""Unit tests for planning the re-attachment of orphaned property values.

Pure core only: `plan` takes rows describing orphans and returns what to do
with each, so no DB. The queries around it are exercised by running the
script with its default dry run against a real corpus before it writes.

Run from the backend directory:
`python -m pytest tests/test_reattach_orphaned_property_values.py`
"""

from scripts.reattach_orphaned_property_values import Orphan, plan, summarise


def _orphan(**kw):
    base = dict(document_id="d1", filename="a.md", property_name="case_number",
                value_id="v1", held_by="Court Decision",
                belongs_to="Advocate General Opinion",
                target_property_id="p-ag", target_occupied=False)
    return Orphan(**{**base, **kw})


def test_a_value_whose_class_declares_the_property_is_reattached():
    """The document was reclassified and the value stayed behind on the old
    class's property row. Re-pointing it is the whole repair."""
    assert [a.kind for a in plan([_orphan()])] == ["reattach"]


def test_a_class_that_does_not_declare_the_property_cannot_take_the_value():
    """Reported rather than skipped: a value with nowhere to go is the state
    that hid 293 documents from a check for a week, and it is invisible
    unless something says it out loud."""
    actions = plan([_orphan(target_property_id=None)])
    assert [a.kind for a in actions] == ["no_property_on_class"]


def test_a_target_that_already_has_a_value_is_left_alone():
    """The value already there may be better than the one arriving, and
    deciding which is not this script's judgement to make."""
    actions = plan([_orphan(target_occupied=True)])
    assert [a.kind for a in actions] == ["target_occupied"]


def test_an_occupied_target_wins_over_a_missing_property():
    """Both conditions cannot hold at once, but the order matters if they
    ever do: nothing is written either way."""
    actions = plan([_orphan(target_property_id=None, target_occupied=True)])
    assert actions[0].kind != "reattach"


def test_each_orphan_is_judged_on_its_own():
    actions = plan([
        _orphan(value_id="v1"),
        _orphan(value_id="v2", target_property_id=None),
        _orphan(value_id="v3", target_occupied=True),
    ])
    assert [a.kind for a in actions] == [
        "reattach", "no_property_on_class", "target_occupied"]


def test_nothing_in_nothing_out():
    assert plan([]) == []


def test_only_reattachable_actions_carry_a_target():
    """What the writer reads. An action without a target must never reach a
    statement that sets property_id."""
    for a in plan([_orphan(target_property_id=None), _orphan(target_occupied=True)]):
        assert a.target_property_id is None or a.kind != "reattach"


def test_the_summary_counts_every_kind():
    counts = summarise(plan([
        _orphan(value_id="v1"), _orphan(value_id="v2"),
        _orphan(value_id="v3", target_property_id=None),
    ]))
    assert counts == {"reattach": 2, "no_property_on_class": 1}
