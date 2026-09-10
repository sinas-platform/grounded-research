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


def test_a_bare_string_evidence_entry_does_not_abort_the_draft():
    """The drafter's reply is unvalidated at this point. `["a.md"]` instead of
    `[{"filename": "a.md"}]` raised AttributeError out of the drafting
    transaction, which rolled the draft back and failed the whole run over one
    malformed field in one claim."""
    rec = _no_text_record(4, {
        "rationale": "Why.",
        "evidence": ["a.md", {"filename": "b.md"}, None],
    })
    assert rec["evidence"] == ["b.md"]
    assert rec["evidence_unreadable"] == 2
    assert "evidence" in rec["carried"], (
        "the field did arrive; what is unreadable is what was in it"
    )


def test_evidence_that_is_not_a_list_reads_as_none_of_it():
    """`"evidence": "a.md"` is the other shape. It used to be sliced as a
    string and iterated character by character."""
    rec = _no_text_record(5, {"rationale": "Why.", "evidence": "a.md"})
    assert rec["evidence"] == []
    assert rec["evidence_unreadable"] == 0


def test_a_readable_record_still_counts_nothing_unreadable():
    rec = _no_text_record(6, {
        "rationale": "Why.",
        "evidence": [{"filename": "a.md"}, {"filename": "b.md"}],
    })
    assert rec["evidence"] == ["a.md", "b.md"]
    assert rec["evidence_unreadable"] == 0


def test_the_slice_is_taken_before_the_filter():
    """At most four entries are considered. A claim that sends six does not
    get more of them read by sending two bad ones."""
    rec = _no_text_record(7, {
        "rationale": "Why.",
        "evidence": ["x", "y", {"filename": "c.md"}, {"filename": "d.md"},
                     {"filename": "e.md"}, {"filename": "f.md"}],
    })
    assert rec["evidence"] == ["c.md", "d.md"]
    assert rec["evidence_unreadable"] == 2
