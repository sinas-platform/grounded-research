"""Unit tests for the check that stands between the extractor and the draft:
a proposed passage is kept only if it occurs in the lines it claims.

The check exists to stop a fabricated quote, so these are written in two
halves. The first says a faithful copy is accepted however the copier
rendered its punctuation. The second says everything the check was built to
reject is still rejected, because a normalization that is too generous
retires the guarantee quietly instead of failing loudly.

Pure: both functions take text and return text or a bool, so no DB and no
network.

Run from the backend directory:
`python -m pytest tests/test_passage_quote_verification.py`
"""

from app.services.query_runner import _canonical, _verify_passage

# Every non-ASCII character here is written as an escape, and this file is
# ASCII. These tests are ABOUT character identity: a source file that reached
# an editor or a transfer that helpfully "fixed" its quotes would still pass
# while testing nothing.
LSQUO, RSQUO = "\u2018", "\u2019"  # single quotation marks
LDQUO, RDQUO = "\u201c", "\u201d"  # double quotation marks
EM_DASH, EN_DASH = "\u2014", "\u2013"
SOFT_HYPHEN = "\u00ad"  # invisible; PDF hyphenation
LAQUO, RAQUO = "\u00ab", "\u00bb"  # guillemets
MINUS = "\u2212"  # mathematical operator, not a dash
PRIME = "\u2032"  # derivative, minute, chemical locant
ZWNJ = "\u200c"  # orthographic joiner, changes the word
ZWSP = "\u200b"  # zero-width space; a line-break hint
BOM = "\ufeff"  # byte-order mark / zero-width no-break space


def numbered(*lines: str) -> str:
    """Lines in the form the extractor is shown them, 1-indexed."""
    return "\n".join(f"{i}: {text}" for i, text in enumerate(lines, start=1))


SOURCE = numbered(
    "The authority may not rely on a document it has not disclosed to the",
    f"party concerned, and the {LDQUO}right to be heard{RDQUO} applies to",
    f"an inspection at the undertaking{RSQUO}s premises {EM_DASH} a rule the",
    f"court has applied since 2011{EN_DASH}2012 without exception.",
    f"A record shall be kept, and any coop{SOFT_HYPHEN}eration offered by the",
    "undertaking shall be noted in paragraph 42 of the report.",
    "The file shall be preserved by the authority.450 Regulation 1 provides",
    "that information obtained may be used only for the stated purpose.",
    f"The {LAQUO}stated purpose{RAQUO} is the subject matter of the decision.",
    f"The margin fell by {MINUS}5 percentage points over the reference period.",
    f"The compound 4,4{PRIME}-methylene is listed in the annex to the decision.",
    f"A zero{ZWNJ}width joiner sits inside a word of this sentence.",
    f"Costs of{ZWSP} the proceedings are borne by the unsuccessful party.",
    f"Annex II{BOM} sets out the timetable agreed between the parties.",
)
ALL_LINES = (1, 14)


def verify(quote: str, span: tuple[int, int] = ALL_LINES) -> bool:
    return _verify_passage(SOURCE, span[0], span[1], quote)


# -- a faithful copy, rendered differently -----------------------------------


def test_straight_double_quotes_match_typographic_ones():
    """The measured cause: the extractor types ASCII quotes, the source
    carries typographic ones, and not a word between them differs."""
    assert verify('party concerned, and the "right to be heard" applies to')


def test_straight_apostrophe_matches_a_typographic_one():
    assert verify("an inspection at the undertaking's premises")


def test_a_hyphen_matches_an_em_dash():
    assert verify("at the undertaking's premises - a rule the court")


def test_a_hyphen_matches_an_en_dash():
    assert verify("court has applied since 2011-2012 without exception")


def test_a_soft_hyphen_in_the_source_is_not_a_difference():
    """PDF extraction leaves the hyphenation of the printed page inside the
    word. Nothing renders it and no copy reproduces it."""
    assert verify("A record shall be kept, and any cooperation offered")


def test_guillemets_match_straight_double_quotes():
    assert verify('The "stated purpose" is the subject matter')


def test_case_and_whitespace_are_still_normalized():
    """Unchanged behaviour, kept under test because the normalization moved."""
    assert verify("THE AUTHORITY   may not\n rely on a document")


def test_several_rendering_differences_at_once():
    assert verify(
        'party concerned, and the "right to be heard" applies to '
        "an inspection at the undertaking's premises - a rule the"
    )


# -- everything the check exists to reject -----------------------------------


def test_a_dropped_word_fails():
    """Observed among the passages this change does NOT rescue: the copy
    reads well and is not what the source says."""
    assert not verify("The authority may not rely on document it has not")


def test_a_changed_word_fails():
    assert not verify("The authority may not rely on a ruling it has not")


def test_a_dropped_inline_footnote_number_fails():
    """The source glues the note number to the sentence. Dropping it is the
    commonest near-miss, and it is still a difference in the digits."""
    assert not verify("The file shall be preserved by the authority. Regulation 1")


def test_a_changed_digit_fails():
    """No fold in the normalization touches a digit, which is what a citation
    is made of."""
    assert not verify("shall be noted in paragraph 43 of the report")


def test_reordered_words_fail():
    assert not verify("may not rely the authority on a document it has not")


def test_text_absent_from_the_source_fails():
    assert not verify("The undertaking is entitled to compensation for the delay")


def test_text_present_but_outside_the_claimed_lines_fails():
    """Provenance is the point: the right words in the wrong place are not
    evidence for a claim bound to that place."""
    assert verify("shall be noted in paragraph 42 of the report")
    assert not verify("shall be noted in paragraph 42 of the report", span=(1, 2))


def test_a_quote_shorter_than_the_floor_fails():
    assert not verify("a rule")


def test_empty_and_missing_text_fail():
    assert not verify("")
    assert not _verify_passage(SOURCE, 1, 9, None)


def test_an_empty_source_range_accepts_nothing():
    assert not _verify_passage("", 1, 9, "The authority may not rely on a document")


def test_a_mathematical_minus_is_not_a_hyphen():
    """A minus is an operator. Folding it to a hyphen would verify a quote
    that does not say what the source says."""
    assert not verify("The margin fell by -5 percentage points over the")


def test_a_prime_is_not_an_apostrophe():
    """Primes mark derivatives, minutes and chemical locants. They are drawn
    like a quotation mark and do not mean one."""
    assert not verify("The compound 4,4'-methylene is listed in the annex")


def test_an_orthographic_joiner_is_not_removed():
    """Unlike a soft hyphen, a zero-width joiner is part of the word."""
    assert not verify("A zerowidth joiner sits inside a word of this sentence")


def test_a_zero_width_space_is_not_removed():
    """Harmless, and out of the map anyway: a deletion needs a case behind
    it, and this one has never been watched to matter."""
    assert not verify("Costs of the proceedings are borne by the unsuccessful")


def test_a_byte_order_mark_is_not_removed():
    """Out for the same reason as the zero-width space, not a safer one: it
    is a zero-width no-break space, and its position is a convention."""
    assert not verify("Annex II sets out the timetable agreed between the")


def test_the_excluded_characters_stay_out_of_the_fold():
    """Stated once as a fact about the map, so deleting a line from it fails
    a test instead of silently widening what verifies. The soft hyphen is
    the only character the fold deletes rather than replaces."""
    from app.services.query_runner import _RENDERING_VARIANTS

    for excluded in (0x2212, 0x2032, 0x2033, 0x200C, 0x200D, 0x200B, 0xFEFF):
        assert excluded not in _RENDERING_VARIANTS


# -- the normalization itself ------------------------------------------------


def test_canonical_folds_only_the_rendering():
    assert _canonical(f"{LDQUO}a{RDQUO} {LSQUO}b{RSQUO} c{EM_DASH}d") == "\"a\" 'b' c-d"


def test_canonical_joins_a_soft_hyphenated_word():
    assert _canonical(f"coop{SOFT_HYPHEN}eration") == "cooperation"


def test_canonical_keeps_every_word_and_digit():
    text = "Article 20(2) of Regulation 1 applies to 3 of the 42 documents."
    assert _canonical(text) == text.lower()


def test_canonical_does_not_join_words_across_a_dash():
    """A dash folds to a hyphen; it does not disappear. Deleting it would let
    two different ranges read alike."""
    assert _canonical(f"2011{EN_DASH}2012") == "2011-2012"


def test_canonical_tolerates_no_text():
    assert _canonical(None) == ""
    assert _canonical("") == ""
