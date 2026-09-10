"""A citation should resolve to the sentence it rests on, not the paragraph.

`Span` has modelled character offsets since it was written, "a line range or
character offsets", and both write sites recorded None. Measured over the
evidence rows held on 10 September 2026: 6,388 carry a line span, none carries
a character span, the average span covers 1,547 characters and 84% cover more
than 400. Everything downstream reads a span at that precision: the passage a
reviewer is shown in the review workbook, the passage the reviser is handed
when the gate asks for a revision, and the export.

Run from the backend directory:
`python -m pytest tests/test_a_span_points_at_a_sentence.py`
"""

from __future__ import annotations

from app.services.query_runner import (_canonical, _canonical_offsets,
                                       _locate_chars, _verified_quote)

PARA = ("The Commission may copy data during an inspection. "
        "Those documents found to be relevant are placed in the file and the "
        "remainder of the copied data is deleted. "
        "The undertaking is informed of the outcome in due course.")
DOC = "---\ntitle: A judgment\n---\n\n" + PARA + "\nA following line.\n"


def test_the_offset_map_agrees_with_the_canonical_form():
    """They must never disagree, so one is written in terms of the other."""
    for t in ("Plain text.", "  ragged\t\tspacing\n\nhere ", "Quotes “and” dashes — here",
              "soft­hyphen", "MiXeD CaSe", "", "İstanbul"):
        canon, src = _canonical_offsets(t)
        assert canon == _canonical(t), repr(t)
        assert len(src) == len(canon), repr(t)
        assert all(0 <= i < len(t) for i in src), repr(t)


def test_a_quote_resolves_to_its_own_characters():
    quote = ("those documents found to be relevant are placed in the file "
             "and the remainder of the copied data is deleted")
    line = DOC.split("\n").index(PARA) + 1
    at = _locate_chars(DOC, line, line, quote)
    assert at is not None
    lo, hi = at
    assert _canonical(DOC[lo:hi]) == _canonical(quote)
    # and it is a fraction of the line it sits on
    assert (hi - lo) < len(PARA) * 0.75


def test_offsets_survive_rendering_differences():
    """The quote as copied is not byte-identical to the source."""
    doc = "Line one.\nThe Court held “that the data — all of it — is deleted” here.\n"
    quote = 'that the data - all of it - is deleted'
    at = _locate_chars(doc, 2, 2, quote)
    assert at is not None
    lo, hi = at
    assert _canonical(doc[lo:hi]) == _canonical(quote)


def test_a_quote_that_is_not_there_returns_nothing():
    assert _locate_chars(DOC, 5, 5, "a sentence that appears nowhere at all") is None


def test_a_quote_under_the_floor_returns_nothing():
    """Same floor the line locator applies: too short to place safely."""
    assert _locate_chars(DOC, 5, 5, "is deleted") is None


def test_out_of_range_lines_return_nothing():
    assert _locate_chars(DOC, 0, 1, "x" * 40) is None
    assert _locate_chars(DOC, 2, 900, "x" * 40) is None


def test_the_quote_comes_from_the_verified_extract():
    verified = {("a.md", 10, 12): "the checked passage text"}
    assert _verified_quote(verified, "a.md",
                           {"line_from": 10, "line_to": 12}) == "the checked passage text"


def test_a_widened_range_still_finds_its_one_passage():
    verified = {("a.md", 10, 12): "the checked passage text"}
    assert _verified_quote(verified, "a.md",
                           {"line_from": 9, "line_to": 14}) == "the checked passage text"


def test_an_ambiguous_overlap_is_refused():
    """Two candidates means choosing would be a guess, and a quote located
    into the wrong sentence is worse than no offset."""
    verified = {("a.md", 10, 12): "first passage", ("a.md", 11, 13): "second passage"}
    assert _verified_quote(verified, "a.md", {"line_from": 9, "line_to": 14}) == ""


def test_another_document_is_never_borrowed_from():
    verified = {("b.md", 10, 12): "text from another file"}
    assert _verified_quote(verified, "a.md", {"line_from": 10, "line_to": 12}) == ""


def test_the_reviewer_is_shown_the_sentence_when_the_row_has_offsets():
    """The whole point: the verdict should be given against the quote."""
    from app.review_export import _passage

    span = {"line_from": 5, "line_to": 5, "char_from": 24, "char_to": 47}
    got = _passage(DOC, span)
    assert got == DOC[24:47].strip()
    assert len(got) < 60


def test_the_reviewer_still_sees_the_lines_on_an_older_row():
    """Rows written before the offsets existed must not show nothing."""
    from app.review_export import _passage

    line = DOC.split("\n").index(PARA) + 1
    got = _passage(DOC, {"line_from": line, "line_to": line})
    assert got == PARA


def test_the_stored_quote_is_used_when_offsets_could_not_be_placed():
    from app.review_export import _passage

    got = _passage(DOC, {"line_from": None}, quote="the passage as verified")
    assert got == "the passage as verified"


def test_offsets_outside_the_document_fall_back_rather_than_slice():
    from app.review_export import _passage

    line = DOC.split("\n").index(PARA) + 1
    got = _passage(DOC, {"line_from": line, "line_to": line,
                         "char_from": 10, "char_to": 99999})
    assert got == PARA
