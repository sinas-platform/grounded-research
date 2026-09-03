"""Unit tests for recording what the coverage check objected to.

The check asks whether the union of a claim's spans carries the whole claim,
and it works: 384 claims marked across 3,777 judged in the stored runs. Its
verdicts were the one kind `validate_answer_evidence` returns without
persisting — per-span verdicts land in `claim_evidence`, a coverage verdict is
separated off before that and lives only long enough to build the reviser's
feedback. Only its count reached telemetry, so 15 answers published with
overreach standing in the round that decided them and nothing records which
claim it was.

Pure: dicts in, dicts out, so no DB and no network.

Run from the backend directory:
`python -m pytest tests/test_overreach_detail.py`
"""

from app.services.query_runner import _overreach_detail


def verdict(seq=3, uncovered="the figure appears in no passage",
            claim="The Commission imposed a fine of EUR 38 million.", **extra):
    """A coverage verdict in the shape faithfulness returns it."""
    return {"claim_coverage": "partial", "claim_id": "abc",
            "claim_sequence": seq, "claim_text": claim,
            "uncovered": uncovered, **extra}


# -- what is kept --------------------------------------------------------------


def test_the_sequence_survives():
    """Without it the record names no claim, which is the whole defect."""
    assert _overreach_detail([verdict(seq=7)])[0]["seq"] == 7


def test_the_ground_survives():
    assert _overreach_detail([verdict(uncovered="no span carries the date")]) \
        [0]["uncovered"] == "no span carries the date"


def test_the_claim_text_survives():
    """Kept beside the sequence on purpose: revision narrows an overreaching
    claim to its supported core, so reading it back by sequence after the run
    gives what it became, not what was objected to."""
    d = _overreach_detail([verdict(claim="The Court held X for the whole period.")])
    assert d[0]["claim"] == "The Court held X for the whole period."


def test_every_verdict_is_recorded():
    d = _overreach_detail([verdict(seq=1), verdict(seq=4), verdict(seq=9)])
    assert [x["seq"] for x in d] == [1, 4, 9]


def test_order_is_preserved():
    """The reviser is fed these in order and the record should read the same
    way, so the two can be compared without sorting either."""
    d = _overreach_detail([verdict(seq=12), verdict(seq=2)])
    assert [x["seq"] for x in d] == [12, 2]


# -- what is left out ----------------------------------------------------------


def test_nothing_marked_records_nothing():
    """An empty list, not a null: the round ran and objected to nothing."""
    assert _overreach_detail([]) == []


def test_long_text_is_truncated_on_both_fields():
    """Telemetry is a JSONB column read by people. `uncovered` is already
    capped at 500 upstream and a claim is capped at 4000 in the table, so both
    are cut again here rather than trusting either."""
    d = _overreach_detail([verdict(uncovered="u" * 900, claim="c" * 900)])
    assert len(d[0]["uncovered"]) == 300
    assert len(d[0]["claim"]) == 300


def test_a_verdict_missing_its_fields_does_not_raise():
    """Bookkeeping must not fail the round it serves. A malformed verdict
    records blanks and is still counted, rather than taking the write down."""
    d = _overreach_detail([{"claim_coverage": "partial"}])
    assert d == [{"seq": None, "uncovered": "", "claim": ""}]


def test_none_values_become_empty_strings():
    d = _overreach_detail([verdict(uncovered=None, claim=None)])
    assert d[0]["uncovered"] == "" and d[0]["claim"] == ""


# -- the contract the existing reader depends on -------------------------------


def test_the_count_is_not_replaced_by_this():
    """`answer_regress` reads `round_N.overreaching` by prefix. The detail is
    added beside the count, never instead of it, so an existing reader that
    knows nothing about this keeps working."""
    from pathlib import Path

    from app.services import obligations

    src = Path(obligations.__file__).with_name("query_runner.py").read_text(
        encoding="utf-8")
    assert '"overreaching": len(verdict.get("overreaching") or []),' in src
    assert '"overreaching_claims": _overreach_detail(' in src


def test_the_sweep_records_it_too():
    """The final sweep is a second overreach finding of the same kind, 37
    across 23 stored runs. Recording the subject in one place and not the
    other is how the mirrored defects in #103 and #106 appeared."""
    from pathlib import Path

    from app.services import obligations

    src = Path(obligations.__file__).with_name("query_runner.py").read_text(
        encoding="utf-8")
    assert '"overreaching_claims": _overreach_detail(f_over)' in src
