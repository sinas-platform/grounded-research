"""Unit tests for granting a round to a claim that is still being narrowed.

Validation gets four rounds, and past that a round is granted only to a run
that is converging — `failed_history[-1] < failed_history[-2]`, a count of
failed evidence rows. A coverage verdict never fails an evidence row, so a
claim being narrowed contributes nothing to that count and buys no time.

Measured: Q53 claim 14 was marked overreaching in round 3, marked again in
round 4, and deleted when the budget ran out at four. The narrowing was
working and it ran out of attempts.

Why the repetition is a reliable signal, and the only one available: evidence
is judged with `pending_only=True`, so a claim whose spans have all passed is
not re-judged and produces no coverage verdict. An untouched claim stops being
reported rather than being reported again. The only way the same sequence comes
back is that the revision bound new spans, which `_bind_spans` writes with
`validated=False`, putting the claim back in front of the judge.

What it cannot tell us is how much better the claim got. The verdict is `full`
or `partial` with no degree, so "partial again" reads the same whether the
claim shrank by half or barely moved.

Pure: no DB, no network.

Run from the backend directory:
`python -m pytest tests/test_narrowing_earns_a_round.py`
"""

import inspect
import pathlib

from app.services.query_runner import _overreach_seqs, _still_narrowing


def v(*seqs):
    return {"overreaching": [{"claim_sequence": s} for s in seqs]}


# -- reading the claim numbers out of a verdict --------------------------------


def test_the_marked_sequences_come_back():
    assert _overreach_seqs(v(3, 14)) == {3, 14}


def test_no_overreach_is_an_empty_set():
    assert _overreach_seqs({}) == set()
    assert _overreach_seqs({"overreaching": []}) == set()


def test_a_sequence_written_as_a_string_is_read():
    assert _overreach_seqs(v("14")) == {14}


def test_a_fractional_sequence_names_no_claim():
    """Truncating 3.7 to 3 would grant a round on a claim the judge did not
    name. Dropping it is the conservative direction: the round is simply not
    earned."""
    assert _overreach_seqs(v(3.7)) == set()
    assert _overreach_seqs(v(3.0)) == {3}


def test_a_boolean_names_no_claim():
    """True is an int in Python and would otherwise read as claim 1."""
    assert _overreach_seqs(v(True)) == set()


def test_junk_is_dropped_not_guessed():
    assert _overreach_seqs({"overreaching": ["nonsense", {"claim_sequence": None},
                                             {"claim_sequence": 7}]}) == {7}


def test_a_missing_key_does_not_raise():
    assert _overreach_seqs({"overreaching": [{}]}) == set()


# -- the criterion -------------------------------------------------------------


def test_the_same_claim_twice_running_is_narrowing():
    """Q53's shape: claim 14 in round 3 and again in round 4."""
    assert _still_narrowing([{14}, {14}])


def test_one_of_several_returning_is_enough():
    """A run does not have to be narrowing everything to be narrowing
    something, and the something is what needs the round."""
    assert _still_narrowing([{3, 14}, {14, 21}])


def test_a_different_claim_each_round_is_not_narrowing():
    """New findings on new claims are not one claim being worked. Granting a
    round for that would extend a run that keeps discovering problems rather
    than one that is closing them."""
    assert not _still_narrowing([{3}, {14}])


def test_overreach_that_cleared_is_not_narrowing():
    """A claim marked and then not marked is a claim that was fixed, and needs
    no extra round."""
    assert not _still_narrowing([{14}, set()])


def test_a_first_marking_is_not_yet_narrowing():
    """One round cannot show repetition. The claim gets the rounds still left
    in the base budget."""
    assert not _still_narrowing([{14}])
    assert not _still_narrowing([set(), {14}])


def test_no_history_is_not_narrowing():
    assert not _still_narrowing([])


def test_only_the_last_two_rounds_count():
    """A claim marked in round 1, absent in 2, marked again in 3 is not being
    worked continuously, and the criterion is about the round just spent."""
    assert not _still_narrowing([{14}, set(), {14}][:2])
    assert _still_narrowing([{14}, set(), {14}][1:]) is False


# -- how it is wired -----------------------------------------------------------


def src() -> str:
    from app.services import query_runner as qr

    return pathlib.Path(inspect.getfile(qr)).read_text(encoding="utf-8")


def test_it_is_a_disjunction_and_converging_is_untouched():
    """The existing criterion keeps its exact meaning; the new one is a second
    reason, not a redefinition of the first."""
    s = src()
    assert ("converging = (len(failed_history) >= 2\n"
            "                          and failed_history[-1] < failed_history[-2])") in s
    assert "not (converging or narrowing)" in s


def test_failed_history_still_counts_only_failed_rows():
    """`answer_regress` reads the same counts out of `round_N` by prefix.
    Folding overreach in would change what every stored run means after the
    fact, which is why the histories are separate lists."""
    s = src()
    assert 'failed_history.append(len(verdict["failed"]))' in s
    assert "overreach_history.append(_overreach_seqs(verdict))" in s
    assert "failed_history.append(_overreach" not in s


def test_the_hard_ceiling_still_binds():
    """Narrowing earns rounds up to the same cap. A claim that never comes
    clean cannot extend a run forever."""
    assert "round_no > HARD_VALIDATE_ROUNDS or not (converging or narrowing)" in src()


def test_the_reason_is_recorded_in_the_round_it_bought():
    """`round_N` is already numbered per round. A flat key would keep only the
    last extension of a run."""
    s = src()
    assert '"extended_because": extended_because' in s
    i = s.index("extended_because = None")
    j = s.index('"extended_because": extended_because', i)
    assert '"overreaching": len(verdict.get("overreaching") or []),' in s[i:j]
