"""Unit tests for how many of the reviser's additions the answer takes.

The cap itself is not new. What is new is that refusing an addition is
counted: an answer already at MAX_CLAIMS admits nothing, the reviser is not
told, and before this the run recorded the same "added" number whether the
claim was written or thrown away. So these tests are about the pair the
caller records, not only about the slice.

Pure: the function takes a list and an int and returns a list and an int, so
no DB and no network.

Run from the backend directory:
`python -m pytest tests/test_claim_cap_admission.py`
"""

from app.services.query_runner import MAX_CLAIMS, _admit_adds


def adds(n: int) -> list[dict]:
    return [{"text": f"claim {i}"} for i in range(n)]


# -- room for everything -----------------------------------------------------


def test_an_empty_answer_takes_every_addition():
    admitted, dropped = _admit_adds(adds(3), live=0)
    assert len(admitted) == 3
    assert dropped == 0


def test_an_answer_below_the_cap_takes_what_fits_under_it():
    admitted, dropped = _admit_adds(adds(2), live=MAX_CLAIMS - 2)
    assert len(admitted) == 2
    assert dropped == 0


def test_the_additions_keep_their_order():
    """The reviser puts the claim it was asked for first; a cap that reorders
    would refuse a different claim than the one it appears to refuse."""
    admitted, _ = _admit_adds(adds(3), live=MAX_CLAIMS - 2)
    assert [a["text"] for a in admitted] == ["claim 0", "claim 1"]


# -- the cap biting ----------------------------------------------------------


def test_an_answer_at_the_cap_takes_nothing_and_says_so():
    """The measured case: 14 claims, two additions requested, both refused,
    and the run recorded them as added."""
    admitted, dropped = _admit_adds(adds(2), live=MAX_CLAIMS)
    assert admitted == []
    assert dropped == 2


def test_a_partial_refusal_counts_only_what_was_refused():
    admitted, dropped = _admit_adds(adds(3), live=MAX_CLAIMS - 1)
    assert len(admitted) == 1
    assert dropped == 2


def test_an_answer_over_the_cap_takes_nothing():
    """Reachable: the cap moves down, or a patch adds before a later drop."""
    admitted, dropped = _admit_adds(adds(2), live=MAX_CLAIMS + 5)
    assert admitted == []
    assert dropped == 2


# -- the invariant the telemetry depends on ----------------------------------


def test_the_two_numbers_always_sum_to_what_was_asked_for():
    """`added` and `add_dropped_at_cap` are recorded side by side and read as
    a pair. If they stop summing to the request, a reader cannot tell a
    refusal from an addition that was never proposed."""
    for requested in range(0, 6):
        for live in range(0, MAX_CLAIMS + 3):
            admitted, dropped = _admit_adds(adds(requested), live=live)
            assert len(admitted) + dropped == requested


def test_nothing_requested_refuses_nothing():
    """An empty patch must not read as a cap hit."""
    assert _admit_adds([], live=MAX_CLAIMS) == ([], 0)
    assert _admit_adds([], live=0) == ([], 0)
