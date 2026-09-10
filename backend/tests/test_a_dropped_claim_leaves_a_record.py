"""A claim the drafter sends with no text disappears without a trace.

Its number is its position in the reply, assigned before anything checks it,
so skipping it leaves a hole: published answers run 9, 11, 12. Measured over
the runs held on 10 September 2026, 74 of 238 published answers carry at least
one such hole and 142 claims are missing across them, and a further 22 runs do
not start at 1, which is the same cause landing on the first claim.

The hole alone cannot say what was lost. An empty placeholder costs only the
numbering. A claim whose text failed to arrive while its reasoning and sources
did costs the answer a proposition. Nothing recorded which, because the reply
was discarded once the claims were written.

Run from the backend directory:
`python -m pytest tests/test_a_dropped_claim_leaves_a_record.py`
"""

from __future__ import annotations

from app.services.query_runner import _no_text_record


def test_an_empty_placeholder_carries_nothing():
    rec = _no_text_record(7, {"text": "", "rationale": "", "evidence": []})
    assert rec["sequence"] == 7
    assert rec["carried"] == []
    assert rec["rationale"] == ""
    assert rec["evidence"] == []


def test_a_claim_that_lost_only_its_text_says_so():
    """The case that matters: everything arrived except the sentence."""
    rec = _no_text_record(11, {
        "text": "",
        "rationale": "Sets out the deadline the question asks about.",
        "evidence": [{"filename": "a.md", "line_from": 3, "line_to": 9},
                     {"filename": "b.md"}],
        "type": "legal_principle",
    })
    assert rec["carried"] == ["evidence", "rationale", "type"]
    assert rec["rationale"].startswith("Sets out the deadline")
    assert rec["evidence"] == ["a.md", "b.md"]


def test_the_record_is_bounded():
    """It sits in telemetry beside everything else, so it cannot be unbounded."""
    rec = _no_text_record(2, {
        "text": "",
        "rationale": "x" * 5000,
        "evidence": [{"filename": f"{i}.md"} for i in range(20)],
        "type": "y" * 200,
    })
    assert len(rec["rationale"]) == 400
    assert len(rec["evidence"]) == 4
    assert len(rec["type"]) == 50


def test_a_missing_text_key_is_treated_as_no_text():
    """`text` absent and `text` empty are the same failure to the caller."""
    rec = _no_text_record(1, {"rationale": "Something."})
    assert rec["carried"] == ["rationale"]
    assert rec["sequence"] == 1
