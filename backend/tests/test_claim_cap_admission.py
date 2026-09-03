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

from app.services.query_runner import MAX_CLAIMS, _admit_adds, _cycle_key


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


# -- numbering the cycles ----------------------------------------------------
#
# Recording the right number was not enough. `_tele` merges by key, so a single
# `revision` key kept only the last cycle, and on three runs that each reached
# the cap every one reported add_dropped_at_cap = 0: the surviving cycle was
# not the cycle that hit it. These tests are about the numbering that fixes it.


def test_the_first_cycle_is_one():
    assert _cycle_key({}, "revision") == "revision_1"


def test_each_cycle_takes_the_next_number():
    seen = {}
    for expected in ("revision_1", "revision_2", "revision_3"):
        key = _cycle_key(seen, "revision")
        assert key == expected
        seen[key] = {}


def test_other_keys_in_the_stage_do_not_shift_the_count():
    """The validate stage holds round_N, gate_parts, fed_points and more in
    the same dict. Only the prefix being numbered may count."""
    entry = {"round_1": {}, "round_2": {}, "started": "t", "gate_parts": [],
             "revision_1": {}}
    assert _cycle_key(entry, "revision") == "revision_2"


def test_the_prefix_is_matched_with_its_separator():
    """`revision` and `revisionfoo` are not cycles of `revision_`. Counting
    them would skip a number and leave a gap that reads as a lost cycle."""
    entry = {"revision": {}, "revisionfoo": {}}
    assert _cycle_key(entry, "revision") == "revision_1"


def test_a_legacy_single_key_run_starts_at_one():
    """Runs recorded before the numbering carry a bare `revision`. A resumed
    one must not collide with it, and must not be numbered as though a cycle
    had already been recorded under the new scheme."""
    assert _cycle_key({"revision": {"added": 2}}, "revision") == "revision_1"


def test_the_same_helper_serves_another_prefix():
    entry = {"revision_1": {}, "revision_2": {}}
    assert _cycle_key(entry, "sweep") == "sweep_1"


# -- the same numbering, applied to extraction --------------------------------
#
# `extract` had the defect this helper exists to remove, and worse: a run
# extracts once for the draft and again for every revision cycle carrying a
# point, and the whole dict was rewritten each time. Every number in it read
# as whichever extraction went last, which is how a draft extraction came to
# be compared against a revision one.


def test_an_untouched_extract_stage_starts_at_one():
    assert _cycle_key({}, "cycle") == "cycle_1"


def test_a_run_recorded_before_the_numbering_starts_at_one():
    """The edge that mattered for revision, in the shape extraction has: a
    stage recorded before this existed carries its counters flat, and none of
    them is a cycle. Numbering must start at 1 and leave the inherited keys
    where they are, not read them as a cycle already recorded."""
    legacy = {
        "claims": 12, "documents_read": 43, "passages_proposed": 31,
        "passages_verified": 25, "extraction_errors": 0,
        "documents_truncated": 5, "characters_dropped": 118420,
        "lines_dropped": 0, "truncated_documents": [], "chunks_total": 72,
        "documents_chunked": 5, "extra_extraction_calls": 3,
        "started": "2026-09-03T09:56:50Z", "completed": "2026-09-03T09:57:04Z",
        "elapsed_s": 13.6,
    }
    assert _cycle_key(legacy, "cycle") == "cycle_1"


def test_extraction_cycles_number_in_order():
    seen = {"started": "t"}
    for expected in ("cycle_1", "cycle_2", "cycle_3"):
        key = _cycle_key(seen, "cycle")
        assert key == expected
        seen[key] = {}


def test_a_legacy_stage_that_later_gains_cycles_keeps_counting_from_them():
    """A resumed run can carry both: flat counters from before and numbered
    cycles from after. Only the numbered ones count."""
    mixed = {"claims": 12, "documents_read": 43, "cycle_1": {}, "cycle_2": {}}
    assert _cycle_key(mixed, "cycle") == "cycle_3"


def test_the_two_prefixes_do_not_count_each_other():
    """`revision_N` lives in the validate stage and `cycle_N` in extract, but
    the helper is one function and a shared counter would be a silent gap in
    whichever it numbered second."""
    entry = {"revision_1": {}, "revision_2": {}, "cycle_1": {}}
    assert _cycle_key(entry, "cycle") == "cycle_2"
    assert _cycle_key(entry, "revision") == "revision_3"
