"""Unit tests for keeping the owed document in front of the extractor.

A revision cycle raised by an unused source exists to settle that one debt.
The document led the anchor list and was named nowhere else, so it competed
for attention on position alone — and, being short, it was shown in round 0
and absent from every round after it while a 2.8 MB decision beside it stayed
for all four.

Two halves, matching the two things that were wrong. The first is presence:
the owed document is in every round. The second is the instruction, which is
pinned at source level the way this repo pins its other prompt contracts.

Pure: dicts in, dict out, so no DB and no network.

Run from the backend directory:
`python -m pytest tests/test_owed_document_rounds.py`
"""

from pathlib import Path

from app.services import obligations
from app.services.query_runner import _shown_for_round

SRC = Path(obligations.__file__).with_name("query_runner.py").read_text(
    encoding="utf-8")


def doc(n: int) -> list[dict]:
    """A document cut into n chunks, each carrying its own index."""
    return [{"text": f"chunk {i}", "strategy": "lines"} for i in range(n)]


# -- presence, without an owed document --------------------------------------


def test_round_zero_shows_the_first_chunk_of_everything():
    docs = {"a.md": doc(1), "b.md": doc(4)}
    assert set(_shown_for_round(docs, 0)) == {"a.md", "b.md"}


def test_a_short_document_falls_out_after_round_zero():
    """Unchanged behaviour, and the reason the owed case needed fixing: a
    one-chunk document is visible once and gone for the rest."""
    docs = {"short.md": doc(1), "long.md": doc(4)}
    assert set(_shown_for_round(docs, 1)) == {"long.md"}
    assert set(_shown_for_round(docs, 3)) == {"long.md"}


def test_a_long_document_advances_through_its_chunks():
    docs = {"long.md": doc(4)}
    assert [_shown_for_round(docs, k)["long.md"]["text"] for k in range(4)] == [
        "chunk 0", "chunk 1", "chunk 2", "chunk 3"]


def test_no_owed_document_changes_nothing():
    docs = {"a.md": doc(1), "b.md": doc(3)}
    for k in range(3):
        assert _shown_for_round(docs, k) == _shown_for_round(docs, k, None)


# -- presence, with one --------------------------------------------------------


def test_a_short_owed_document_is_in_every_round():
    """The fix. It is what the cycle exists to settle, so it does not drop
    out while the documents it competes with stay."""
    docs = {"owed.md": doc(1), "long.md": doc(4)}
    for k in range(4):
        assert "owed.md" in _shown_for_round(docs, k, "owed.md")


def test_a_short_owed_document_repeats_its_only_chunk():
    docs = {"owed.md": doc(1), "long.md": doc(4)}
    for k in range(4):
        assert _shown_for_round(docs, k, "owed.md")["owed.md"]["text"] == "chunk 0"


def test_an_owed_document_cycles_its_own_chunks():
    """With two chunks and four rounds it shows both, twice, rather than
    repeating the first and never reaching the second."""
    docs = {"owed.md": doc(2), "long.md": doc(4)}
    seen = [_shown_for_round(docs, k, "owed.md")["owed.md"]["text"]
            for k in range(4)]
    assert seen == ["chunk 0", "chunk 1", "chunk 0", "chunk 1"]


def test_the_owed_document_keeps_its_place_at_the_head():
    """It leads the anchor list, and the blob is built in this order. Appending
    it after the loop would move it to the end in exactly the rounds where it
    had to be re-added."""
    docs = {"owed.md": doc(1), "b.md": doc(4), "c.md": doc(4)}
    assert list(_shown_for_round(docs, 2, "owed.md")) == ["owed.md", "b.md", "c.md"]


def test_a_long_owed_document_is_not_treated_specially_while_it_lasts():
    docs = {"owed.md": doc(4), "b.md": doc(4)}
    assert _shown_for_round(docs, 2, "owed.md")["owed.md"]["text"] == "chunk 2"


# -- what must not break -------------------------------------------------------


def test_an_owed_document_that_is_not_anchored_is_not_invented():
    docs = {"a.md": doc(1)}
    assert "missing.md" not in _shown_for_round(docs, 1, "missing.md")


def test_an_owed_document_with_no_chunks_is_skipped():
    """`_fetch_numbered` can return an entry with nothing in it. Indexing
    `chunks[k % 0]` would raise, and a bookkeeping detail must not fail the
    extraction it serves."""
    assert _shown_for_round({"owed.md": []}, 1, "owed.md") == {}


def test_no_documents_shows_nothing():
    assert _shown_for_round({}, 0, "owed.md") == {}


# -- the instruction, pinned at source ----------------------------------------


def test_the_prompt_names_the_owed_document():
    """Pinning it is only half: the model can weigh a document as the point of
    the call only if it is told which one it is."""
    assert "One document below is owed:" in SRC


def test_the_refusal_is_offered_and_has_somewhere_to_go():
    """An instruction to produce a passage with no expressible alternative is
    an instruction to strain for one, and a stretched quote from the real
    document passes verification exactly as a faithful one does."""
    assert "take nothing from it" in SRC
    assert '"owed_has_nothing": true|false' in SRC
    assert 'data.get("owed_has_nothing") is True' in SRC


def test_the_refusal_is_counted():
    """It is the only signal separating "this document does not bear on the
    objective" from "nothing was found"."""
    # Named without the punctuation that carries them: they are keyword
    # arguments here and dict entries once the per-cycle numbering in #109
    # lands, and the counter existing is the thing worth pinning.
    assert "owed_declared_empty" in SRC
    assert "owed_with_passage" in SRC


def test_the_debt_travels_as_a_field_not_inside_the_truncated_sentence():
    """`establishes` is cut at 600 characters. A 400-character note plus a
    130-character filename left 48 characters of margin, so lengthening either
    would have dropped the marker with nothing failing loudly."""
    assert '"owed": owed' in SRC
    assert '"establishes": (pt[:ob.start()] if ob else pt).strip()[:600]' in SRC


def test_repeated_spans_are_kept_once():
    """The owed document is shown every round, so the same span can come back
    more than once; the drafter would read two copies as two sources."""
    assert "seen_spans" in SRC


# -- the refusal only counts when it stood --------------------------------------
#
# A multi-chunk owed document is shown once per round, so one round can declare
# its chunk empty while another returns a verified passage from a different
# one. Pinning it into every round — what this change does — is what makes that
# reachable, and counting the declaration on its own would put the same point
# in both `owed_declared_empty` and `owed_with_passage`.


def test_the_refusal_is_counted_only_when_no_passage_came_back():
    assert 'if r.get("owed_empty") and not any(' in SRC


def test_the_two_counters_cannot_both_claim_one_point():
    """`owed_with_passage` and `owed_declared_empty` are read as a partition of
    the owed points; a point in both would make the diagnostic say two opposite
    things about the same document."""
    # Named without the punctuation that carries them: keyword arguments on
    # this branch, dict entries once #109's per-cycle numbering lands, and the
    # counters existing as a pair is what the test is for.
    assert "owed_with_passage" in SRC
    assert "owed_declared_empty" in SRC
    assert 'p["filename"] == r.get("owed")' in SRC
