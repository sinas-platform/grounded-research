"""Unit tests for the record of where an answer's concluding claim is.

A reviewer's finding on one question was that the answer ends off-topic with
no conclusion. Across the eleven runs carrying gate telemetry, two end on a
claim that answers the question, two arguably do, and seven end on a source
note or a procedural aside, so the shape is real and common.

The record has to keep three findings apart, because they want different
remedies: an answer that closes, a conclusion buried at claim N with case notes
after it, and no conclusion anywhere. `no_conclusion` already in the verdict is
a boolean and collapses the last two into one, and records nothing when false.

Nothing here blocks. This is `_audit_coverage`'s contract: recorded so the next
batch can say how often each shape happens, and the decision comes after.

The helper is pure. The recorder's DB read and telemetry write are stubbed, so
no DB and no network.

Run from the backend directory:
`python -m pytest tests/test_closing_record.py`
"""

import uuid

import pytest

from app.services import query_runner as qr
from app.services.query_runner import _closing_record

# Sequence to claim id, which is what the recorder is given. A bare set of
# sequences was enough until the numbers turned out not to survive the run:
# `_compact_claim_sequences` renumbers 1..N at publication, so a number
# recorded mid-run can name a different claim in the answer a reviewer reads.
SEQS = {n: uuid.uuid4() for n in (1, 2, 3, 9, 14)}
PARTS3 = [{"asks": "a"}, {"asks": "b"}, {"asks": "c"}]


def rec(data, seqs=SEQS, parts=PARTS3):
    return _closing_record(data, seqs, parts)


# -- the three shapes ----------------------------------------------------------


def test_an_answer_that_closes():
    r = rec({"concludes_at": 14})
    assert r["shape"] == "closes" and r["ends_on_it"] is True
    assert r["concludes_at"] == 14 and r["last"] == 14


def test_a_conclusion_buried_before_the_end():
    """Six of the seven. The claim exists; case notes follow it."""
    r = rec({"concludes_at": 9})
    assert r["shape"] == "buried" and r["ends_on_it"] is False
    assert r["concludes_at"] == 9 and r["last"] == 14


def test_no_conclusion_anywhere():
    r = rec({"concludes_at": None})
    assert r["shape"] == "absent" and r["ends_on_it"] is False
    assert r["concludes_at"] is None


def test_the_three_shapes_are_distinguishable():
    """The whole point: a boolean cannot tell buried from absent."""
    assert {rec({"concludes_at": v})["shape"] for v in (14, 9, None)} == {
        "closes", "buried", "absent"}


# -- reading the sequence ------------------------------------------------------


def test_a_sequence_naming_no_claim_is_not_a_location():
    """The gate can name a claim that is not in the answer; that is what
    `covered_by_missing` exists for. Absent, not a false location."""
    r = rec({"concludes_at": 77})
    assert r["concludes_at"] is None and r["shape"] == "absent"


def test_a_sequence_written_as_a_string_is_read():
    assert rec({"concludes_at": "9"})["concludes_at"] == 9


def test_a_boolean_names_no_claim():
    """True is an int in Python and would otherwise be read as claim 1."""
    assert rec({"concludes_at": True})["concludes_at"] is None


def test_a_fractional_sequence_names_no_claim():
    assert rec({"concludes_at": 9.5})["concludes_at"] is None


def test_junk_is_dropped_not_guessed():
    for v in ("last", [], {}, "nine"):
        assert rec({"concludes_at": v})["concludes_at"] is None


def test_a_missing_key_does_not_raise():
    assert rec({})["shape"] == "absent"


def test_an_empty_answer_has_no_last_claim():
    assert rec({"concludes_at": 3}, seqs=set())["last"] is None
    assert rec({"concludes_at": 3}, seqs=set())["shape"] == "absent"


# -- the gate's own boolean, recorded beside the position ----------------------


def test_the_gate_boolean_is_recorded_either_way():
    assert rec({"no_conclusion": True})["gate_said_none"] is True
    assert rec({"no_conclusion": False})["gate_said_none"] is False
    assert rec({})["gate_said_none"] is False


def test_a_non_canonical_scalar_is_not_a_yes():
    """`bool("false")` is True. Read for truthiness, a reply of "false" would
    manufacture exactly the disagreement this field exists to measure, so the
    boolean is read strictly and anything else is a reply that did not answer.

    Note the blocking consumer still reads the same field for truthiness, so
    the two can diverge. That is a defect in the blocking path and is left
    alone here deliberately: fixing it changes what publishes."""
    for v in ("false", "no", 0, 1, "true", [], None):
        assert rec({"no_conclusion": v})["gate_said_none"] is False, v
    assert rec({"no_conclusion": True})["gate_said_none"] is True


def test_disagreement_between_the_two_is_recordable():
    """The measurement worth having. `no_conclusion` has fired in none of the
    39 runs since gate telemetry landed, while three of eleven read by hand end
    with no conclusion anywhere. Both values are kept so that gap can be
    counted rather than argued about."""
    r = rec({"no_conclusion": True, "concludes_at": 9})
    assert r["gate_said_none"] is True and r["shape"] == "buried"


# -- single_part ---------------------------------------------------------------


def test_a_one_part_question_is_marked():
    """`parts: 1, covered: 1` is the whole coverage check, and every `only_*`
    counter is computed over covered parts, so all of them are inert. On such a
    run this record is the only whole-answer signal there is."""
    assert rec({"concludes_at": 14}, parts=[{"asks": "a"}])["single_part"] is True
    assert rec({"concludes_at": 14})["single_part"] is False


def test_no_parts_at_all_is_not_a_single_part():
    assert rec({}, parts=[])["single_part"] is False


# -- it reaches the numbered cycle ---------------------------------------------


@pytest.fixture
def telemetry(monkeypatch):
    state = {"validate": {}}

    class FakeRun:
        @property
        def telemetry(self):
            return {"validate": state["validate"]}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, run_id):
            return FakeRun()

    async def fake_tele(run_id, stage, **detail):
        state["validate"].update(detail)

    monkeypatch.setattr(qr, "AsyncSessionLocal", FakeSession)
    monkeypatch.setattr(qr, "_tele", fake_tele)
    return state


@pytest.mark.asyncio
async def test_the_record_lands_in_the_cycle(telemetry):
    await qr._record_gate_cycle("run-1", parts=[{"asks": "a"}],
                                closing={"shape": "buried", "concludes_at": 9})
    assert telemetry["validate"]["gate_1"]["closing"] == {
        "shape": "buried", "concludes_at": 9}


@pytest.mark.asyncio
async def test_each_cycle_keeps_its_own(telemetry):
    """A run gates more than once — `_pre_publish_sweep` returns False after
    feeding a repair cycle and the caller re-enters. A flat key would keep only
    the last, which is the defect four other fields here already had."""
    await qr._record_gate_cycle("run-1", parts=[], closing={"shape": "absent"})
    await qr._record_gate_cycle("run-1", parts=[], closing={"shape": "closes"})
    assert telemetry["validate"]["gate_1"]["closing"]["shape"] == "absent"
    assert telemetry["validate"]["gate_2"]["closing"]["shape"] == "closes"


@pytest.mark.asyncio
async def test_a_cycle_with_no_record_writes_an_empty_one(telemetry):
    """The unparseable path records `parts=[]` and passes no closing. An empty
    dict says the verdict could not be read; it must not inherit the last."""
    await qr._record_gate_cycle("run-1", parts=[], closing={"shape": "closes"})
    await qr._record_gate_cycle("run-1", parts=[], unparseable="boom")
    assert telemetry["validate"]["gate_2"]["closing"] == {}


@pytest.mark.asyncio
async def test_it_is_not_summarised_into_the_flat_coverage_key(telemetry):
    """`gate_coverage` is what a consumer reads for a last-write summary, and
    the closing record is per-cycle. Keeping it out of that key is what stops
    it being read as a count."""
    await qr._record_gate_cycle("run-1", parts=[], closing={"shape": "absent"},
                                coverage={"parts": 1, "covered": 1})
    assert "closing" not in telemetry["validate"]["gate_coverage"]
    assert telemetry["validate"]["gate_1"]["closing"]["shape"] == "absent"


# -- the number does not survive the run, the identity does --------------------


def test_the_conclusion_is_recorded_by_identity_as_well_as_by_number():
    """A sequence is a position in a list that is still being edited. The id
    is the claim. Both are kept: the number is the gate's own reading and the
    disagreement measure depends on it, the id is what still resolves later."""
    r = rec({"concludes_at": 9})
    assert r["concludes_at"] == 9
    assert r["concludes_claim_id"] == str(SEQS[9])


def test_no_conclusion_means_no_claim_id():
    for value in (None, "not a number", True, 2.5, 99):
        assert rec({"concludes_at": value})["concludes_claim_id"] is None


def test_renumbering_after_a_drop_leaves_the_number_wrong_and_the_id_right():
    """The defect this fixes, as it happened on a real run.

    The gate recorded the conclusion at sequence 12. The claim at sequence 11
    was then dropped, and `_compact_claim_sequences` closed the gap at
    publication, so the concluding claim became 11 and sequence 12 became a
    different claim about sealed envelopes. Anyone reading the record for the
    conclusion landed on the wrong sentence.
    """
    conclusion, other = uuid.uuid4(), uuid.uuid4()
    during_the_run = {10: uuid.uuid4(), 11: other, 12: conclusion}
    r = _closing_record({"concludes_at": 12}, during_the_run, PARTS3)

    # the drop of 11, then compaction: what was 12 is now 11
    published = {10: uuid.uuid4(), 11: conclusion}

    assert published.get(r["concludes_at"]) is None, "the number went stale"
    assert r["concludes_claim_id"] == str(conclusion)
    assert str(published[11]) == r["concludes_claim_id"], "the id still resolves"
