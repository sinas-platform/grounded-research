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

from app.services.query_runner import (
    _canonical,
    _locate_passage,
    _reject_reason,
    _verify_passage,
)

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


# -- the range is symmetric ---------------------------------------------------
#
# It used to be widened forward only. The extractor reports where it believes
# a quote sits and is off by a line or two either way; nothing about that error
# is directional. The forward-only rule arrived as a passenger in the commit
# that built extract drafting and was never argued for, while `_anchors_for`
# assumes the opposite three lines away, taking `lines[0] - 3` for its windows.


def test_a_quote_starting_a_line_early_verifies():
    """The case the old rule threw away: the text is verbatim, in the document,
    and the reported start is one line late."""
    assert _verify_passage(SOURCE, 2, 3, "The authority may not rely on a document")


def test_a_quote_starting_two_lines_early_verifies():
    assert _verify_passage(SOURCE, 3, 4, "The authority may not rely on a document")


def test_the_old_rule_rejected_exactly_that():
    """Pinned so the asymmetry cannot come back unnoticed: with back=0 the
    same passage fails, which is what `recovered_by_symmetry` counts."""
    assert not _verify_passage(
        SOURCE, 2, 3, "The authority may not rely on a document", back=0)


def test_forward_slack_is_unchanged():
    assert _verify_passage(SOURCE, 1, 1, "party concerned, and the")


def test_three_lines_early_still_fails():
    """Widened, not opened. Two lines each way is the tolerance; a quote from
    somewhere else in the document is still not evidence for this span."""
    assert not _verify_passage(SOURCE, 4, 5, "The authority may not rely on a document")


def test_provenance_still_holds_at_distance():
    """The guarantee the check exists for: the right words in the wrong place
    are not evidence for a claim bound to that place."""
    assert not _verify_passage(SOURCE, 1, 2, "shall be noted in paragraph 42")


def test_symmetry_does_not_admit_a_fabrication():
    """Two more lines of real document is more text to match against, not
    weaker matching."""
    assert not _verify_passage(
        SOURCE, 2, 3, "The undertaking is entitled to compensation for the delay")


# -- why a passage was rejected ------------------------------------------------
#
# 1,043 of 3,933 proposed passages were rejected across the stored runs with
# nothing recorded but the two counts. A quote the model invented and a quote
# it copied faithfully while misreporting its line number are different
# failures, and only one of them is the check working.


def shown_one() -> dict:
    return {"a.md": {"text": SOURCE, "strategy": "whole"}}


def test_a_kept_passage_has_no_reason():
    assert _reject_reason(
        shown_one(), "a.md", 1, 2, "The authority may not rely on a document") is None


def test_a_document_not_shown_says_so():
    """The model can name a file it was never given; that is not the same
    failure as quoting one it was."""
    assert _reject_reason(shown_one(), "other.md", 1, 2, "x" * 40) \
        == "document not among those shown"


def test_an_unreadable_line_range_says_so():
    assert _reject_reason(shown_one(), "a.md", None, None, "x" * 40) \
        == "line range not readable"


def test_a_quote_under_the_floor_says_so():
    assert _reject_reason(shown_one(), "a.md", 1, 2, "a rule") \
        == "quote under the length floor"


def test_a_quote_not_in_the_lines_says_so():
    """The interesting one: the text may be real and elsewhere, or invented.
    Both land here, which is why the sample is kept alongside the count."""
    assert _reject_reason(shown_one(), "a.md", 1, 2, "shall be noted in paragraph 42") \
        == "not found in the claimed lines"


def test_the_reasons_are_checked_in_a_useful_order():
    """A passage that is both from an unshown document and too short reports
    the document, because that is the actionable half."""
    assert _reject_reason(shown_one(), "other.md", 1, 2, "short") \
        == "document not among those shown"


# -- the recorded span is where the quote is ------------------------------------
#
# The verifier widens the reported range before checking containment, so a
# quote placed a line or two off still verifies — and the reported coordinates
# were then persisted as the evidence span, so a citation could point at lines
# that do not contain the text it cites. That was already true of the forward
# slack; making the range symmetric doubles the exposure and puts it at the end
# a reader looks at first. The span is corrected rather than the slack
# withdrawn: the tolerance lets a faithful quote through, the coordinates have
# to be true.


def test_a_span_reported_late_is_corrected_back():
    """The case the symmetry admits: the quote is on line 1, reported as 2."""
    assert _locate_passage(
        SOURCE, 2, 3, "The authority may not rely on a document") == (1, 1)


def test_a_span_reported_early_is_corrected_forward():
    """The case the old forward slack already admitted, and already recorded
    wrongly."""
    assert _locate_passage(
        SOURCE, 4, 4, "A record shall be kept") == (5, 5)


def test_an_accurate_span_is_left_alone():
    assert _locate_passage(
        SOURCE, 1, 1, "The authority may not rely on a document") == (1, 1)


def test_a_quote_spanning_two_lines_reports_both():
    assert _locate_passage(
        SOURCE, 1, 4,
        f"party concerned, and the {LDQUO}right to be heard{RDQUO} applies to "
        f"an inspection at the undertaking{RSQUO}s premises") == (2, 3)


def test_the_narrowest_window_wins():
    """A quote that fits in one line is recorded as one line, not as the range
    the model guessed around it."""
    assert _locate_passage(
        SOURCE, 1, 6, "shall be noted in paragraph 42 of the report") == (6, 6)


def test_text_outside_the_tolerance_locates_nothing():
    assert _locate_passage(
        SOURCE, 8, 9, "The authority may not rely on a document") is None


def test_a_quote_under_the_floor_locates_nothing():
    """Same floor as the verifier, so the two cannot disagree about whether a
    passage exists."""
    assert _locate_passage(SOURCE, 1, 2, "a rule") is None


def test_locating_agrees_with_verifying():
    """The pair has to move together: anything the verifier accepts must be
    locatable, or the span would fall back to coordinates the verifier just
    said were approximate."""
    cases = [
        (2, 3, "The authority may not rely on a document"),
        (1, 1, "party concerned, and the"),
        (4, 5, "court has applied since 2011-2012 without exception"),
        (5, 6, "A record shall be kept, and any cooperation offered"),
    ]
    for lf, lt, q in cases:
        assert _verify_passage(SOURCE, lf, lt, q)
        assert _locate_passage(SOURCE, lf, lt, q) is not None


def test_what_the_verifier_rejects_is_not_located():
    for lf, lt, q in [
        (1, 2, "shall be noted in paragraph 42 of the report"),
        (1, 9, "The undertaking is entitled to compensation for the delay"),
    ]:
        assert not _verify_passage(SOURCE, lf, lt, q)
        assert _locate_passage(SOURCE, lf, lt, q) is None


# -- a long quote gets its whole span ------------------------------------------
#
# Verification matches on the first 200 canonical characters and up to 2,000
# are stored. Locating on the prefix alone ended the span before the text it
# was meant to cover: a citation shorter than the passage it cites, which is
# what the locator exists to prevent, reappearing at the other end.


LONG = numbered(*[f"Paragraph {i}. " + "the authority shall record the matter "
                  f"in the file as item {i} of the annex." for i in range(1, 9)])


def test_a_quote_longer_than_the_match_prefix_spans_all_its_lines():
    """Four lines of quote, well past 200 characters. The span has to reach
    the fourth, not stop at whichever line the first 200 end on."""
    quote = " ".join(row.split(": ", 1)[1] for row in LONG.splitlines()[1:5])
    assert len(_canonical(quote)) > 200
    assert _locate_passage(LONG, 2, 5, quote) == (2, 5)


def test_the_prefix_is_only_a_fallback():
    """When the whole quote is not inside the tolerance but its first 200
    characters are, the span still lands — verification accepted on that
    prefix, so the locator must not come back empty and drop to the reported
    coordinates it just called approximate."""
    quote = " ".join(row.split(": ", 1)[1] for row in LONG.splitlines()[1:5])
    assert _verify_passage(LONG, 2, 3, quote)
    assert _locate_passage(LONG, 2, 3, quote) is not None


def test_a_short_quote_is_unaffected():
    """Under 200 characters the two paths are the same search."""
    assert _locate_passage(
        SOURCE, 2, 3, "The authority may not rely on a document") == (1, 1)
