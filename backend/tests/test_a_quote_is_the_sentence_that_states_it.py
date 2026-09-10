"""A passage should be the sentence that states the point, not the page.

The extractor was asked for "2-25 lines", which is not a unit: two lines is
about 90 characters in a short-lined document and over 2,000 in a long-lined
one, so one instruction asked for a sentence in one document and a page in
another. Measured on the first run to store its quotes, a passage ran 2 to
2.7 times the length of the claim it supported, and a citation therefore
resolved to a paragraph however precisely its coordinates were recorded.

200 characters because that is where a quote becomes wholly checked: the
verifier and the locator compare only the first 200 canonical characters, so
past that a quote is accepted on its opening alone.

Nothing here rejects a passage. The counts exist so the distribution can be
watched before anything is made binding on it.

Run from the backend directory:
`python -m pytest tests/test_a_quote_is_the_sentence_that_states_it.py`
"""

from __future__ import annotations

from app.services.query_runner import (_QUOTE_FLOOR_CHARS, _QUOTE_TARGET_CHARS,
                                       _quote_lengths)


def _r(*lengths):
    return [{"passages": [{"text": "x" * n} for n in lengths]}]


def test_the_target_is_where_verification_stops_looking():
    """Not an arbitrary round number: past it a quote is checked on its
    opening alone."""
    assert _QUOTE_TARGET_CHARS == 200
    assert _QUOTE_FLOOR_CHARS < _QUOTE_TARGET_CHARS


def test_nothing_to_count_is_not_an_error():
    assert _quote_lengths([])["passages"] == 0
    assert _quote_lengths([{"passages": []}])["passages"] == 0


def test_the_shape_is_reported_not_just_the_average():
    """An average hides the tail, and the tail is the question."""
    got = _quote_lengths(_r(100, 150, 900))
    assert got["median_chars"] == 150
    assert got["longest_chars"] == 900
    assert got["mean_chars"] == round((100 + 150 + 900) / 3)


def test_quotes_past_the_target_are_counted():
    got = _quote_lengths(_r(50, 199, 200, 201, 1000))
    assert got["over_target"] == 2
    assert got["over_target_pct"] == 40


def test_quotes_under_the_floor_are_counted_separately():
    """The opposite failure: verbatim, locatable, and unreadable alone."""
    got = _quote_lengths(_r(10, 79, 80, 500))
    assert got["under_floor"] == 2


def test_text_carried_past_verification_is_summed():
    """The finding the target answers, as a number per run."""
    got = _quote_lengths(_r(200, 300, 700))
    assert got["chars_beyond_verified"] == 0 + 100 + 500


def test_counting_does_not_reject_anything():
    """Soft on purpose: a passage of any length still comes back."""
    runs = _r(5, 5000)
    assert _quote_lengths(runs)["passages"] == 2
