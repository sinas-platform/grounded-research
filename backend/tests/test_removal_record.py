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
