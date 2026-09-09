"""What became of each planned claim.

Written after a run where a planned claim was retrieved at rank 8, anchored,
read whole, extracted without error, and then appeared nowhere in the answer.
Establishing that took nine queries across the plan, the extraction record and
the citations. This record answers it in one.
"""

from __future__ import annotations

from app.services.query_runner import _plan_outcome


def test_a_planned_claim_the_extractor_found_nothing_for():
    """Its group is skipped before the drafter sees it, so it cannot be
    'unused' -- nothing was ever shown."""
    out = _plan_outcome([{"n": 4, "passages": []}], drafted=[])
    assert out == [{"n": 4, "passages": 0, "documents": [], "cited_by": [],
                    "state": "no_passages"}]


def test_a_planned_claim_shown_and_left_out():
    """The case this exists for: passages extracted, shown, and no drafted
    claim cites any of their documents."""
    extracts = [{"n": 8, "passages": [{"filename": "a.md"},
                                      {"filename": "a.md"}]}]
    out = _plan_outcome(extracts, drafted=[(1, {"b.md"})])
    assert out[0]["state"] == "extracted_unused"
    assert out[0]["passages"] == 2
    assert out[0]["documents"] == ["a.md"]
    assert out[0]["cited_by"] == []


def test_a_planned_claim_the_answer_used():
    extracts = [{"n": 2, "passages": [{"filename": "a.md"}]}]
    out = _plan_outcome(extracts, drafted=[(3, {"a.md"}),
                                           (5, {"other.md"})])
    assert out[0]["state"] == "used"
    assert out[0]["cited_by"] == [3]


def test_unused_is_the_claim_that_cannot_be_wrong():
    """Attribution is by document, because nothing carries an identity across
    the drafting call. Two planned claims on one document both read as used
    when one of them was -- so `used` may over-report and `extracted_unused`
    may not. A claim reported unused had none of its documents cited at all.
    """
    shared = [{"filename": "a.md"}]
    out = _plan_outcome([{"n": 1, "passages": shared},
                         {"n": 2, "passages": shared}],
                        drafted=[(4, {"a.md"})])
    assert [o["state"] for o in out] == ["used", "used"], "over-reports, by design"

    out2 = _plan_outcome([{"n": 1, "passages": shared},
                          {"n": 2, "passages": shared}],
                         drafted=[(4, {"elsewhere.md"})])
    assert [o["state"] for o in out2] == ["extracted_unused"] * 2, (
        "if nothing cites the document, neither planned claim can be used")


def test_every_planned_claim_appears_whatever_happened_to_it():
    """A record that omits the ones that went nowhere is the record we already
    had: the answer's claims, with no way to see what did not become one."""
    extracts = [{"n": 1, "passages": [{"filename": "a.md"}]},
                {"n": 2, "passages": []},
                {"n": 3, "passages": [{"filename": "b.md"}]}]
    out = _plan_outcome(extracts, drafted=[(1, {"a.md"})])
    assert [o["n"] for o in out] == [1, 2, 3]
    assert [o["state"] for o in out] == ["used", "no_passages", "extracted_unused"]
