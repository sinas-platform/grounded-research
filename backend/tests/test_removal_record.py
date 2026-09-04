"""Unit tests for recording what a deleted claim said and cited.

Three paths delete a claim and each recorded something different: the reviser's
drop kept a count, the sweep's forced drop kept a count, and only the
rounds-exhausted drop kept the text. None kept the citations, and the evidence
rows go with the claim, so afterwards nobody could ask whether a deletion took
the last citation of a source the gate had named.

Measured over the stored runs before this: 160 claims deleted across the three
paths, 123 with their text, 0 with their citations. The question was not hard
to answer, it was structurally unanswerable.

The session is stubbed, so no DB.

Run from the backend directory:
`python -m pytest tests/test_removal_record.py`
"""

import inspect
import pathlib

import pytest
from app.services import query_runner as qr


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """Returns one fixed row set, and remembers it was asked."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def execute(self, *a, **k):
        self.calls += 1
        return FakeResult(self.rows)


# (claim_id, sequence, claim_text, filename) — the shape of the outer join,
# one row per evidence row, so a claim with three citations arrives three times
# and a claim with none arrives once with a null filename.
def rec(rows):
    return qr._removal_record(FakeSession(rows), ["id-1"])


@pytest.mark.asyncio
async def test_a_claim_carries_its_text_and_its_citations():
    out = await rec([
        ("c1", 3, "The Commission may take a forensic copy.", "62018CJ0606.md"),
        ("c1", 3, "The Commission may take a forensic copy.", "62018CC0606.md"),
    ])
    assert out == [{"sequence": 3,
                    "claim": "The Commission may take a forensic copy.",
                    "cites": ["62018CJ0606.md", "62018CC0606.md"]}]


@pytest.mark.asyncio
async def test_a_repeated_filename_is_listed_once():
    """A claim can bind two spans in the same document. That is one source."""
    out = await rec([("c1", 1, "x", "a.md"), ("c1", 1, "x", "a.md"),
                     ("c1", 1, "x", "b.md")])
    assert out[0]["cites"] == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_a_claim_with_no_evidence_is_recorded_with_an_empty_list():
    """Not omitted. A claim that cited nothing is exactly the kind worth
    knowing about after the fact, and an absent key reads as unknown."""
    out = await rec([("c1", 2, "an unsupported claim", None)])
    assert out == [{"sequence": 2, "claim": "an unsupported claim", "cites": []}]


@pytest.mark.asyncio
async def test_claims_come_back_in_sequence_order():
    out = await rec([("c3", 9, "third", "c.md"), ("c1", 2, "first", "a.md"),
                     ("c2", 5, "second", "b.md")])
    assert [d["sequence"] for d in out] == [2, 5, 9]


@pytest.mark.asyncio
async def test_the_text_is_bounded():
    """It goes into a JSONB column a person reads, like every other record
    here. 400 characters is what the rounds-exhausted path already used."""
    out = await rec([("c1", 1, "x" * 900, "a.md")])
    assert len(out[0]["claim"]) == 400


@pytest.mark.asyncio
async def test_nothing_to_delete_asks_the_database_nothing():
    s = FakeSession([])
    assert await qr._removal_record(s, []) == []
    assert s.calls == 0


@pytest.mark.asyncio
async def test_a_null_claim_text_does_not_break_the_record():
    out = await rec([("c1", 1, None, "a.md")])
    assert out[0]["claim"] == ""


# -- the three call sites ------------------------------------------------------


def src() -> str:
    return pathlib.Path(inspect.getfile(qr)).read_text(encoding="utf-8")


def test_every_deleting_path_records_first():
    """The record has to be read before the delete, in the same session. After
    the delete there is nothing to read: the evidence rows go with the claim."""
    s = src()
    for call, delete in (
        ("dropped_here = await _removal_record(", "for seq in patch[\"drop\"]:"),
        ("swept = await _removal_record(s3, ids)", "for cid in ids:"),
        ("dropped = await _removal_record(session, list(failing_ids))",
         "for cid in failing_ids:"),
    ):
        assert call in s, call
        assert s.index(call) < s.index(delete, s.index(call) - 400), call


def test_the_revisers_drop_now_carries_a_detail():
    """It carried a bare count, which is why 14 claims left answers this month
    with no record of what they were."""
    assert '"dropped": len(patch["drop"]), "dropped_detail": dropped_here,' in src()


def test_the_sweeps_drop_now_carries_a_detail():
    """These are removed on an overreach verdict, which never fails an evidence
    row, so nothing downstream would otherwise name them."""
    assert "final_sweep_dropped_detail=swept)" in src()


def test_the_rounds_exhausted_path_keeps_its_reasons():
    """It already explained itself and must go on doing so; the citations are
    added beside the reasons, not instead of them."""
    s = src()
    assert 'entry["why"] = [r for r in reasons if r][:3]' in s
    assert "dropped_detail=dropped)" in s


def test_the_three_paths_share_one_recorder():
    """Three call sites, one function. Three near-copies is how they came to
    record three different things in the first place."""
    assert src().count("await _removal_record(") == 3


# -- a second deletion must not erase the first --------------------------------
#
# `_tele` merges by key, and both paths that delete outside a revision cycle
# wrote a flat one. `_stage_validate_publish` recurses and the sweep runs more
# than once, so the second deletion overwrote the first and the earlier claims
# and their citations vanished from the telemetry that exists to keep them.


def test_deletions_outside_a_revision_cycle_are_numbered():
    s = src()
    assert 'await _record_removal(run_id, "rounds_exhausted", dropped)' in s
    assert 'await _record_removal(run_id, "final_sweep", swept)' in s
    assert '_next_cycle_key(run_id, "validate", "removed")' in s


def test_the_prefix_numbers_from_one_beside_the_flat_keys():
    """`removed_` was chosen when `_cycle_key` counted any key starting with
    the prefix: `dropped_claims` and `dropped_detail` are both flat keys under
    `validate`, so `dropped_N` would have opened a run's history at
    `dropped_3`. The gate-cycle change has since tightened the helper to
    `prefix_<digits>`, so that hazard is gone and `dropped_` would number
    correctly too.

    The prefix stays anyway, and this pins what it now buys: the history is a
    different thing from the two flat keys, which carry only the latest state.
    """
    from app.services.query_runner import _cycle_key

    flat = {"dropped_claims": 2, "dropped_detail": [], "final_sweep_dropped": 1}
    assert _cycle_key(flat, "removed") == "removed_1"
    # No longer load-bearing, and recorded as such rather than left asserting
    # a hazard that another branch removed.
    assert _cycle_key(flat, "dropped") == "dropped_1"


def test_each_record_names_which_path_deleted():
    """Three paths delete for different reasons and the count alone never said
    which one had run."""
    s = src()
    assert '"path": path' in s
    assert '"rounds_exhausted"' in s and '"final_sweep"' in s


def test_nothing_to_record_writes_no_key():
    s = src()
    i = s.index("async def _record_removal")
    assert "if not entries:\n        return" in s[i:i + 1400]


def test_the_revisers_drop_is_not_double_recorded():
    """It already sits inside `revision_N`, which is numbered. Writing it here
    too would put the same deletion in the telemetry twice."""
    s = src()
    assert s.count("await _record_removal(") == 2


# -- a drop costs a reason -----------------------------------------------------
#
# Every other disposition pays for itself: revise needs text and spans, add
# needs those plus a type, keep needs a rationale, waive needs twenty
# characters of one. Drop was a bare integer — the cheapest thing the reviser
# could write, and the only one that removed a claim with no record of why.


def parse(payload):
    from app.services.query_runner import _parse_patch

    return _parse_patch(payload)


def test_a_drop_with_a_reason_is_applied_and_the_reason_kept():
    p = parse('{"drop": [{"seq": 3, "rationale": "no passage available carries '
              'the assertion about time limits"}]}')
    assert p["drop"] == [3]
    assert p["drop_reasons"][3].startswith("no passage available")


def test_a_bare_integer_is_a_drop_the_reviser_declined_to_explain():
    """The old shape. Recorded and not applied, so a reviser that will not
    explain shows up as a refusal rather than as a parse failure."""
    p = parse('{"drop": [9]}')
    assert p["drop"] == [] and p["drop_unexplained"] == [9]


def test_a_short_reason_does_not_count():
    """The same twenty-character floor a waive already has to clear."""
    p = parse('{"drop": [{"seq": 9, "rationale": "wrong"}]}')
    assert p["drop"] == [] and p["drop_unexplained"] == [9]


def test_a_drop_with_no_sequence_is_neither():
    """It names no claim, so it is not a drop and not a refusal to explain one.
    A patch with nothing else in it is no patch at all, which is what the
    parser already returns for a reply that changes nothing."""
    assert parse('{"drop": [{"rationale": "a reason long enough to clear it"}]}') is None


def test_a_fractional_sequence_names_no_claim():
    """int(9.5) is 9, and a drop is applied by deleting the claim it names. A
    sequence the reply did not write as a whole number is not rounded into one
    — the same guard revise and keep already apply, where the cost of getting
    it wrong is only a rewrite."""
    assert parse('{"drop": [{"seq": 9.5, "rationale": "no passage available '
                 'carries this assertion"}]}') is None


def test_a_boolean_sequence_names_no_claim():
    """int(True) is 1, so a JSON true would delete the first claim."""
    assert parse('{"drop": [{"seq": true, "rationale": "no passage available '
                 'carries this assertion"}]}') is None


def test_a_sequence_written_as_a_string_still_names_its_claim():
    """Models quote numbers. revise and keep accept it, so drop does too."""
    p = parse('{"drop": [{"seq": "9", "rationale": "no passage available '
              'carries this assertion"}]}')
    assert p["drop"] == [9]


def test_a_rejected_sequence_is_not_recorded_as_a_refusal_to_explain():
    """It came with a reason. What it lacked was a claim to apply it to, so
    reporting it beside the reviser's refusals would misread the failure."""
    p = parse('{"drop": [{"seq": 9.5, "rationale": "no passage available '
              'carries this assertion"}, 2]}')
    assert p["drop"] == [] and p["drop_unexplained"] == [2]


def test_explained_and_unexplained_drops_can_arrive_together():
    p = parse('{"drop": [{"seq": 1, "rationale": "this claim rests on a passage '
              'that does not mention it"}, 2, {"seq": 3, "rationale": "no"}]}')
    assert p["drop"] == [1] and sorted(p["drop_unexplained"]) == [2, 3]


def test_an_unexplained_drop_alone_is_still_a_patch():
    """Otherwise the reply reads as unusable and the reviser is asked to repeat
    itself, when what it did was make one disposition that did not count."""
    assert parse('{"drop": [9]}') is not None


def test_the_prompt_asks_for_the_shape_and_says_the_price():
    s = src()
    assert '"drop": [{"seq": <int>, "rationale":' in s
    # Asserted in fragments: the sentence is wrapped across source lines, and
    # pinning the contiguous string would fail on a reformat that changes
    # nothing about what the reviser reads.
    assert "Dropping a claim costs a reason" in s
    assert "with no reason is not applied and the claim stays." in s


def test_the_reason_rides_with_what_the_claim_said_and_cited():
    """`_removal_record` reads the database and cannot know the reviser's
    words, so they are merged in at the call site."""
    s = src()
    assert 'patch.get("drop_reasons") or {}' in s
    assert '"dropped_unexplained": patch.get("drop_unexplained") or []' in s
def test_a_reply_of_nothing_but_refusals_still_records_them():
    """The reply that matters most takes the other exit. Nothing is applied,
    so `_revise_answer` returns through its no-change branch, and until this
    that branch wrote every count as zero and said nothing about the drops it
    had refused. A reviser that will remove a claim but not say why would then
    be the one behaviour the record could not show.
    """
    s = src()
    i = s.index("revision_yielded_no_change=True")
    branch = s[i:i + 700]
    assert '"dropped_unexplained": (patch or {}).get("drop_unexplained")' in branch


def test_the_no_change_branch_survives_an_unparsable_reply():
    """It is reached with `patch` None as well, and reads the same key off it."""
    s = src()
    i = s.index("revision_yielded_no_change=True")
    assert "(patch or {})" in s[i:i + 700]
