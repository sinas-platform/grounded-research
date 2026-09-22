"""A word broken across a line with a soft hyphen reads as one word.

Stored text breaks long words at a line end with a soft hyphen, `mainte\\xad`
then `nance` on the next line. The canonical form dropped the hyphen and kept
the break as a space, `mainte nance`, which no copy of the rendered text has.
A quote reading `maintenance` then stopped matching at that point:
verification, which checks the opening 200 characters, rejected any quote
whose break fell inside them, and the locator fell back to the opening of any
longer quote and recorded a span that ended before the passage did.

The passage below is invented.

Run from the backend directory:
`python -m pytest tests/test_a_soft_hyphen_at_a_line_break_joins_the_word.py`
"""

import pytest
from app.services import query_runner as qr

LINES = [
    "Harbour notice",
    "A harbour pass admits its holder to the quay provided that the vessel is moored for mainte\u00ad",
    "nance and not for trade, that the fee is paid by the day before the vessel leaves the ",
    "quay, and that the holder is registered with the harbour office, that is to say, is ",
    "a person entered in the register kept at the office and not merely known to the ",
    "harbour master. ",
    "Next paragraph.",
]
DOC = "\n".join(LINES)
NUMBERED = "\n".join(f"{i}: {t}" for i, t in enumerate(LINES, 1))
QUOTE = ("A harbour pass admits its holder to the quay provided that the vessel is "
         "moored for maintenance and not for trade, that the fee is paid by the day "
         "before the vessel leaves the quay, and that the holder is registered with "
         "the harbour office, that is to say, is a person entered in the register "
         "kept at the office and not merely known to the harbour master.")


@pytest.mark.parametrize("broken", [
    "mainte\u00ad\nnance", "mainte\u00ad nance", "mainte\u00ad  \n  nance"])
def test_a_soft_hyphen_then_a_break_joins(broken):
    """The verifier joins span lines with a space before canonicalising, so
    the break arrives either way."""
    assert qr._canonical(broken) == "maintenance"


def test_a_soft_hyphen_inside_a_word_is_unchanged():
    assert qr._canonical("main\u00adtenance") == "maintenance"


def test_emphasis_keeps_its_space():
    """Emphasis markers are also dropped characters; only the soft hyphen
    joins across whitespace."""
    assert qr._canonical("**14.** The notice") == "14. the notice"
    assert qr._canonical("_Harbour_ notice") == "harbour notice"


def test_the_offset_map_stays_in_step():
    canon, src = qr._canonical_offsets(DOC)
    assert len(canon) == len(src)
    at = canon.find("maintenance")
    assert DOC[src[at]:src[at + len("maintenance") - 1] + 1].startswith("mainte")


def test_a_quote_across_the_break_verifies():
    assert qr._verify_passage(NUMBERED, 2, 6, QUOTE)


def test_the_located_span_covers_the_whole_passage():
    """It used to stop at the break, on the lines of the opening 200
    characters, which here carry only the first of the passage's three
    conditions."""
    assert qr._locate_passage(NUMBERED, 1, len(LINES), QUOTE, back=0, fwd=0) == (2, 6)


def test_the_character_span_covers_the_whole_passage():
    span = qr._locate_chars(DOC, 2, 6, QUOTE)
    assert span is not None
    assert DOC[span[0]:span[1]].rstrip().endswith("harbour master.")
