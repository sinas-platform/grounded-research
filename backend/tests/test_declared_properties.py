"""A document that states a property is read, not asked about.

Front matter is parsed at upload and never consulted again. Ingestion puts the
whole document, header included, into a prompt and asks a model to return the
properties, and on one corpus the model and the header disagree on 693 dates:
a header saying 1973-03-26 stored as 1973-02-22, weeks out, which reads like a
date taken from the body while the header said otherwise.

The disagreement is concentrated, which is what makes this worth doing. Across
the same corpus, language disagreed 0 times in 14,318, CELEX 0 in 5,215, ECLI
0 in 284, url 7 in 1,960, and title 205 in 26,123 once truncation and
punctuation are set aside. Codes the model copies; the one field it has to
decide about is the one it gets wrong. That field decides which of two
judgments is the later one, and 188 supersedes relationships exist across some
26,000 decisions, so recency is the date and nothing else.

What is checked here is the precedence, because overwriting is a decision:

  no value            write it
  method `auto`       replace it, keeping the old value in `reason`
  method `manual`     leave it, and count it
  locked              leave it, and count it

and the `on_conflict` a class declares per property, so a deployment decides
which fields the header is the better source for. `replace` for a date the
document states outright; `fill_only` for a title, where the header is not
obviously better and 0.8% does not argue that it is.
"""

from __future__ import annotations

import pytest

from app.services.declared_properties import (
    DECLARED_METHOD,
    Existing,
    plan_declared_values,
    replacement_reason,
)

MAPPING = [
    {"key": "decision_date", "property": "decision_date", "on_conflict": "replace"},
    {"key": "title", "property": "title", "on_conflict": "fill_only"},
    {"key": "language", "property": "language"},
]
HEADER = {"decision_date": "1973-03-26", "title": "A judgment",
          "language": "en", "seat": "somewhere"}


def _plan(existing=None):
    return plan_declared_values(HEADER, MAPPING, existing or {})


def test_a_property_with_no_value_is_written():
    w = {p.property: p for p in _plan().write}
    assert w["decision_date"].value == "1973-03-26"
    assert w["decision_date"].replaces is None


def test_a_declared_value_is_written_with_its_own_method_and_full_confidence():
    """Not `auto`. A value read from the document is not a value a model
    guessed, and the two must be told apart afterwards."""
    p = _plan().write[0]
    assert p.method == DECLARED_METHOD != "auto"
    assert p.confidence == 1.0


def test_a_key_the_class_does_not_declare_is_ignored():
    """`seat` is in the header and in no mapping. The platform does not decide
    what a header key means; the class does."""
    assert "seat" not in {p.property for p in _plan().write}


def test_a_mapped_key_absent_from_the_header_is_not_invented():
    plan = plan_declared_values({"language": "fr"}, MAPPING, {})
    assert {p.property for p in plan.write} == {"language"}


def test_an_auto_value_that_disagrees_is_replaced():
    """The 693. Nothing about them is a human decision: every one is `auto`
    and none is locked."""
    plan = _plan({"decision_date": Existing(value="1973-02-22", method="auto",
                                            locked=False)})
    p = {x.property: x for x in plan.write}["decision_date"]
    assert p.value == "1973-03-26"
    assert p.replaces == "1973-02-22"


def test_a_manual_value_is_never_replaced():
    """689 values in that corpus are `manual` and none is among the 693. A
    person deciding a value outranks a header."""
    plan = _plan({"decision_date": Existing(value="1973-02-22", method="manual",
                                            locked=False)})
    assert "decision_date" not in {p.property for p in plan.write}
    assert plan.kept_manual == ["decision_date"]


def test_a_locked_value_is_never_replaced():
    plan = _plan({"decision_date": Existing(value="1973-02-22", method="auto",
                                            locked=True)})
    assert "decision_date" not in {p.property for p in plan.write}
    assert plan.kept_locked == ["decision_date"]


def test_fill_only_writes_into_a_gap_and_never_over_a_value():
    """The title case. The header is not obviously the better source for a
    title, so the class says so and the platform obeys."""
    assert "title" in {p.property for p in _plan().write}
    plan = _plan({"title": Existing(value="Something else", method="auto",
                                    locked=False)})
    assert "title" not in {p.property for p in plan.write}
    assert plan.kept_existing == ["title"]


def test_an_agreeing_value_is_not_rewritten():
    """Rewriting a value to the same value would churn the row and lose the
    method that says where the current one came from."""
    plan = _plan({"decision_date": Existing(value="1973-03-26", method="auto",
                                            locked=False)})
    assert "decision_date" not in {p.property for p in plan.write}
    assert plan.unchanged == ["decision_date"]


def test_a_value_this_path_already_wrote_is_left_alone():
    plan = _plan({"decision_date": Existing(value="1973-03-26",
                                            method=DECLARED_METHOD, locked=False)})
    assert "decision_date" not in {p.property for p in plan.write}


def test_the_reason_follows_the_backfill_convention():
    """Same shape as the 7 September joined-case split, so a reader who finds
    a replaced value knows where the old one is:
    `backfill 2026-09-07 joined_split; prior value: {"_": "T-515/13 ..."}`
    """
    r = replacement_reason("2026-09-10", "1973-02-22")
    assert r.startswith("backfill 2026-09-10 declared_properties; prior value:")
    assert '{"_": "1973-02-22"}' in r


def test_a_first_write_says_where_the_value_came_from_without_a_prior():
    r = replacement_reason("2026-09-10", None)
    assert "prior value" not in r
    assert "declared_properties" in r


def test_an_unknown_on_conflict_is_refused_rather_than_guessed():
    """A typo in the deployment's file must not silently become `fill_only`
    and quietly stop replacing the field a must-have depends on."""
    with pytest.raises(ValueError, match="on_conflict"):
        plan_declared_values(HEADER, [{"key": "decision_date",
                                       "property": "decision_date",
                                       "on_conflict": "overwrite"}], {})
