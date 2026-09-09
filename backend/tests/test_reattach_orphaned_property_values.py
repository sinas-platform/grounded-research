"""Unit tests for planning the re-attachment of orphaned property values.

Pure core only: `plan` takes rows describing orphans and returns what to do
with each, so no DB. The queries around it are exercised by running the
script with its default dry run against a real corpus before it writes.

Run from the backend directory:
`python -m pytest tests/test_reattach_orphaned_property_values.py`
"""

import inspect

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
    # The two reattachable ones point at different targets. They used to share
    # one, which is the state that produced two writes onto a single property
    # and is now reported as contested rather than counted twice.
    counts = summarise(plan([
        _orphan(value_id="v1", target_property_id="t-1"),
        _orphan(value_id="v2", target_property_id="t-2"),
        _orphan(value_id="v3", target_property_id=None),
        _orphan(value_id="v4", target_property_id="t-3"),
        _orphan(value_id="v5", target_property_id="t-3"),
    ]))
    assert counts == {"reattach": 2, "no_property_on_class": 1,
                      "contested_target": 2}


def test_two_orphans_competing_for_one_empty_target_are_both_refused():
    """`target_occupied` is read per row when the orphans are selected, so two
    rows for the same property both see the target empty and both would be
    re-pointed at it, leaving two values on the destination and nothing saying
    they came from a race.

    The script already refuses to choose between an orphan and a value already
    in place. It has no more business choosing between two orphans, so both are
    reported and neither is written.
    """
    pair = [_orphan(value_id="pv-1"), _orphan(value_id="pv-2")]
    assert [a.kind for a in plan(pair)] == ["contested_target", "contested_target"]
    assert all(a.target_property_id is None for a in plan(pair)), \
        "a refused action writes nothing"


def test_one_orphan_per_target_is_still_reattached():
    """The contest check must not catch the ordinary case, which is all of this
    corpus today: no document holds two orphaned rows for one property."""
    two_properties = [_orphan(value_id="pv-1", target_property_id="t-1"),
                      _orphan(value_id="pv-2", target_property_id="t-2")]
    assert [a.kind for a in plan(two_properties)] == ["reattach", "reattach"]


def test_a_contested_target_that_is_already_occupied_is_reported_as_occupied():
    """Occupancy is the stronger statement and comes first: the value in place
    is a fact, the contest is only between candidates."""
    pair = [_orphan(value_id="pv-1", target_occupied=True),
            _orphan(value_id="pv-2", target_occupied=True)]
    assert [a.kind for a in plan(pair)] == ["target_occupied", "target_occupied"]


def test_the_summary_names_every_outcome_it_counts():
    """A kind that writes nothing still has to appear, or the operator sees a
    gap between the categories and the total with nothing explaining it."""
    import scripts.reattach_orphaned_property_values as mod
    src = inspect.getsource(mod.main)
    kinds = {a.kind for a in plan([
        _orphan(value_id="v1", target_property_id="t-1"),
        _orphan(value_id="v2", target_property_id=None),
        _orphan(value_id="v3", target_occupied=True),
        _orphan(value_id="v4", target_property_id="t-3"),
        _orphan(value_id="v5", target_property_id="t-3"),
    ])}
    for kind in kinds:
        assert kind in src, f"{kind} is planned but never reported"


def test_the_write_restates_the_condition_the_plan_assumed():
    """Occupancy is read when the orphans are selected and the plan is printed
    for a human before anything is written. The target can fill in between, and
    there is no uniqueness constraint to catch it, so the update carries the
    check rather than trusting the earlier read."""
    import scripts.reattach_orphaned_property_values as mod
    sql = str(mod._REATTACH)
    assert "not exists" in sql.lower()
    assert "x.property_id = cast(:target as uuid)" in sql

