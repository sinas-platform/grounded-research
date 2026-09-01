"""Unit tests for the top-rank obligation rule.

Pure: `_unaccounted_top` takes the manifest rows and the cited set and returns
rows, so no DB and no network. The recording around it is exercised against
the real stack elsewhere, same convention as the other runner tests.

Run from the backend directory:
`python -m pytest tests/test_top_rank_obligations.py`
"""

from app.services.query_runner import (
    TOP_RANK_NOTE,
    TOP_RANK_OBLIGATION_N,
    _unaccounted_top,
)


def rows(*names):
    """Manifest rows in rank order, which is how _manifest_rows returns them."""
    return [{"filename": n} for n in names]


# ── the cut ──────────────────────────────────────────────────────────────────


def test_the_cut_is_five():
    """Chosen from the feed cap, not from coverage, and the tests below assume
    it, so a change to it should fail here first."""
    assert TOP_RANK_OBLIGATION_N == 5


def test_nothing_below_the_cut_is_raised():
    got = _unaccounted_top(rows("a", "b", "c", "d", "e", "f", "g"), set())
    assert [r["filename"] for r in got] == ["a", "b", "c", "d", "e"]


def test_a_document_below_the_cut_is_never_raised_however_uncited():
    got = _unaccounted_top(rows("a", "b", "c", "d", "e", "f"), {"a", "b", "c", "d", "e"})
    assert got == []


def test_a_short_result_set_is_handled():
    got = _unaccounted_top(rows("a", "b"), set())
    assert [r["filename"] for r in got] == ["a", "b"]


def test_an_empty_result_set_raises_nothing():
    assert _unaccounted_top([], set()) == []


# ── what counts as accounted for ─────────────────────────────────────────────


def test_a_cited_document_is_not_raised():
    got = _unaccounted_top(rows("a", "b", "c"), {"b"})
    assert [r["filename"] for r in got] == ["a", "c"]


def test_all_cited_raises_nothing():
    assert _unaccounted_top(rows("a", "b", "c"), {"a", "b", "c"}) == []


def test_citations_below_the_cut_do_not_account_for_those_above():
    """A claim citing rank 9 says nothing about rank 2."""
    got = _unaccounted_top(rows("a", "b", "c", "d", "e", "f"), {"f"})
    assert [r["filename"] for r in got] == ["a", "b", "c", "d", "e"]


def test_rank_order_is_preserved():
    got = _unaccounted_top(rows("first", "second", "third"), {"second"})
    assert [r["filename"] for r in got] == ["first", "third"]


def test_a_row_without_a_filename_is_skipped():
    got = _unaccounted_top([{"filename": ""}, {"filename": "b"}], set())
    assert [r["filename"] for r in got] == ["b"]


def test_the_whole_row_comes_back_not_just_the_name():
    got = _unaccounted_top([{"filename": "a", "class": "X"}], set())
    assert got[0]["class"] == "X"


# ── the note ─────────────────────────────────────────────────────────────────


def test_the_note_says_what_kind_of_finding_this_is():
    """The reviser chooses between citing and waiving, and a ranking fact is
    weaker evidence than the gate's reasoning about a document."""
    assert "ranking fact" in TOP_RANK_NOTE
    assert "not a judgement" in TOP_RANK_NOTE


def test_the_note_offers_both_ways_out():
    assert "cite it" in TOP_RANK_NOTE
    assert "waive it" in TOP_RANK_NOTE
