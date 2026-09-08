"""Unit tests for the source-naming check.

Pure core only: `review` takes claims and their sources and returns findings,
so no DB and no network. The loader around it is exercised against the real
stack elsewhere, same convention as the other runner tests.

Run from the backend directory: `python -m pytest tests/test_claim_naming.py`
"""

from app.services.claim_naming import (
    Claim,
    Finding,
    Mismatch,
    Source,
    attributes,
    carries_identifier,
    case_identity,
    cases_named,
    identifier_core,
    message,
    mismatch_message,
    mismatches,
    review,
)

CUES = frozenset({"held", "found", "ruled"})
SRC = Source(key="doc-1", identifiers=("X-111/22",), label="a.md")
OTHER = Source(key="doc-2", identifiers=("Y-333/44",), label="b.md")


def _review(claims, sources, cues=CUES):
    return review([Claim(s, t) for s, t in claims], sources, cues)


# ── identifier_core ──────────────────────────────────────────────────────────


def test_core_strips_a_varying_prefix():
    """The same thing is written with and without its scheme; the digits are
    what distinguish it."""
    assert identifier_core("X-111/22") == "111/22"
    assert identifier_core("111/22") == "111/22"
    assert identifier_core("ZZ/M.98765") == "98765"


def test_core_keeps_only_the_first_run():
    assert identifier_core("Q-555/66 R") == "555/66"


def test_core_of_a_value_with_no_digits_is_empty():
    assert identifier_core("no digits here") == ""


# ── carries_identifier ───────────────────────────────────────────────────────


def test_identifier_written_differently_still_matches():
    assert carries_identifier("as set out in 111/22, the body", ("X-111/22",))


def test_identifier_broken_by_whitespace_matches():
    assert carries_identifier("see X-111/\n22 above", ("X-111/22",))


def test_absent_identifier_does_not_match():
    assert not carries_identifier("no reference of any kind", ("X-111/22",))


def test_an_identifier_punctuated_differently_still_matches():
    """The same decision is written `AT.39796` by the corpus and
    `COMP/39.796` by the prose. Comparing on the characters that carry the
    identity, and not on the separators between them, is what makes those the
    same thing."""
    assert carries_identifier(
        "In Commission Decision COMP/39.796 (Suez Environnement), the "
        "Commission found a broken seal", ("AT.39796",))


def test_two_identifiers_written_side_by_side_both_match():
    """`Nos. 85-4053, 85-4068` is two identifiers touching. Removing the
    separators from the claim would run them into one number and hide both."""
    assert carries_identifier(
        "In In re Antitrust Grand Jury, Nos. 85-4053, 85-4068, the appellate "
        "court held", ("85-4053, 85-4068",))


def test_a_number_embedded_in_a_longer_one_is_not_a_match():
    """Dropping separators would otherwise let an identifier match the middle
    of an unrelated number."""
    assert not carries_identifier("the 2011122 undertakings", ("X-111/22",))


def test_a_core_below_the_minimum_never_matches():
    """A two-character remainder occurs by chance in ordinary prose."""
    assert not carries_identifier("chapter 7 of the report", ("A-7",))


def test_any_of_several_identifiers_counts():
    assert carries_identifier("under 333/44", ("X-111/22", "Y-333/44"))


# ── attributes ───────────────────────────────────────────────────────────────


def test_cue_makes_a_claim_attributing():
    assert attributes("the body held that it applies", CUES)


def test_claim_without_a_cue_is_not_attributing():
    assert not attributes("the market is national in scope", CUES)


def test_cue_matches_whole_words_only():
    """A cue that is a substring of another word must not fire."""
    assert not attributes("the withheld document", frozenset({"held"}))


def test_no_cues_configured_means_nothing_attributes():
    assert not attributes("the body held that it applies", frozenset())


# ── review: the first-mention rule ───────────────────────────────────────────


def test_first_mention_that_identifies_its_source_is_clean():
    found = _review([(1, "In X-111/22 the body held that it applies")], {1: [SRC]})
    assert found == []


def test_first_mention_without_the_identifier_is_reported():
    found = _review([(1, "The body held that it applies")], {1: [SRC]})
    assert [f.kind for f in found] == ["unnamed_first_mention"]
    assert found[0].seqs == (1,)


def test_later_claims_need_not_repeat_the_identifier():
    """The rule is per source, not per claim: naming it once is the ask."""
    found = _review(
        [(1, "In X-111/22 the body held that it applies"),
         (2, "The body further held that it also applies here")],
        {1: [SRC], 2: [SRC]},
    )
    assert found == []


def test_naming_it_only_later_still_fails_the_first_mention():
    found = _review(
        [(1, "The body held that it applies"),
         (2, "In X-111/22 the body found the same")],
        {1: [SRC], 2: [SRC]},
    )
    assert [f.kind for f in found] == ["unnamed_first_mention"]
    assert found[0].seq == 1


def test_non_attributing_claims_do_not_start_the_count():
    """A claim that merely describes its source is not a first mention."""
    found = _review(
        [(1, "The market is national in scope"),
         (2, "The body held that it applies")],
        {1: [SRC], 2: [SRC]},
    )
    assert [f.seq for f in found] == [2]


# ── review: the chain ────────────────────────────────────────────────────────


def test_several_unnamed_attributions_are_one_chain_not_many_findings():
    found = _review(
        [(1, "The body held that it applies"),
         (2, "The body further found the same"),
         (3, "The body ruled likewise")],
        {1: [SRC], 2: [SRC], 3: [SRC]},
    )
    assert [f.kind for f in found] == ["unanchored_chain"]
    assert found[0].seqs == (1, 2, 3)


def test_a_chain_whose_first_claim_names_the_source_is_clean():
    found = _review(
        [(1, "In X-111/22 the body held that it applies"),
         (2, "The body further found the same")],
        {1: [SRC], 2: [SRC]},
    )
    assert found == []


def test_chain_is_reported_when_the_identifier_appears_nowhere():
    found = _review(
        [(1, "The body held one thing"), (2, "The body held another")],
        {1: [SRC], 2: [SRC]},
    )
    assert found[0].kind == "unanchored_chain"


def test_chains_sort_before_single_findings():
    """The cap on findings is a real cut, so the worse defect must survive it."""
    found = _review(
        [(1, "The body held a thing about the second source"),
         (2, "The body held one thing"),
         (3, "The body held another")],
        {1: [OTHER], 2: [SRC], 3: [SRC]},
    )
    assert [f.kind for f in found] == ["unanchored_chain", "unnamed_first_mention"]


def test_repeated_evidence_from_one_document_is_not_a_chain():
    """A claim can hold several passages from the same document. That is one
    claim relying on one source, and must not read as a chain of claims."""
    found = _review([(1, "The body held that it applies")], {1: [SRC, SRC, SRC]})
    assert [f.kind for f in found] == ["unnamed_first_mention"]
    assert found[0].seqs == (1,)


# ── review: what is out of scope ─────────────────────────────────────────────


def test_a_source_with_no_identifier_is_not_checked():
    """Nothing can be demanded of a source that has no identifier to give."""
    bare = Source(key="doc-3", identifiers=(), label="c.md")
    assert _review([(1, "The body held that it applies")], {1: [bare]}) == []


def test_claims_citing_nothing_are_ignored():
    assert _review([(1, "The body held that it applies")], {}) == []


def test_two_sources_are_judged_independently():
    found = _review(
        [(1, "In X-111/22 the body held a thing"),
         (2, "The body held another thing")],
        {1: [SRC], 2: [OTHER]},
    )
    assert [(f.kind, f.seq) for f in found] == [("unnamed_first_mention", 2)]


def test_one_claim_citing_two_sources_can_fail_for_one_of_them():
    found = _review(
        [(1, "In X-111/22 the body held a thing")], {1: [SRC, OTHER]}
    )
    assert [f.source.key for f in found] == ["doc-2"]


# ── the message ──────────────────────────────────────────────────────────────


def test_message_gives_the_identifier_to_write():
    """A reviser told only that something is unnamed changes nothing."""
    text = message(Finding("unnamed_first_mention", SRC, (4,)))
    assert "X-111/22" in text
    assert "a.md" in text
    assert "4" in text


def test_chain_message_lists_the_claims_and_points_at_the_first():
    text = message(Finding("unanchored_chain", SRC, (2, 5, 9)))
    assert "2, 5, 9" in text
    assert "claim 2" in text


# ── case_identity: a stored identifier reduced to the case it names ──────────


def test_identity_drops_a_procedural_suffix():
    """`C-606/18` and `C-606/18 P` are written for the same case. Dropping the
    suffix can only make two things compare equal, which is the safe
    direction: it costs a finding, never invents one."""
    assert case_identity("C-606/18 P") == "C-606/18"
    assert case_identity("C-606/18") == "C-606/18"
    assert case_identity("C-65/18 P(R)") == "C-65/18"
    assert case_identity("T-1097/23 R-RENV") == "T-1097/23"


def test_identity_normalises_leading_zeros():
    """A number derived from a CELEX name is written without them and the same
    case stored by hand is written with them."""
    assert case_identity("C-010/18") == case_identity("C-10/18 P")


def test_identity_keeps_the_court_letter():
    """T-449/14 and C-449/14 are different cases before different courts."""
    assert case_identity("T-449/14") != case_identity("C-449/14")


def test_identity_of_an_identifier_that_names_no_case_is_none():
    """Merger and national references are identifiers, but not of the shape
    this check can read, so it declines to read them."""
    assert case_identity("COMP/M.1234") is None
    assert case_identity("11-D-17") is None
    assert case_identity("509 U.S. 209") is None


# ── cases_named: the cases a claim writes ────────────────────────────────────


def test_a_case_in_parentheses_is_found():
    assert cases_named("Nexans v Commission (C-606/18 P), paragraph 87") == {
        "C-606/18"
    }


def test_the_case_prefixed_form_is_found():
    assert cases_named("The General Court, in Case T-249/17, reasoned") == {
        "T-249/17"
    }


def test_several_cases_are_all_found():
    named = cases_named(
        "in Joined Cases T-125/03 and T-253/03, appealed in Case C-550/07 P"
    )
    assert named == {"T-125/03", "T-253/03", "C-550/07"}


def test_a_case_spaced_around_its_hyphen_is_found():
    """Judgment text as published writes `Case C \u2011 541/23 P`, with a
    non-breaking hyphen and a space on either side. A claim quoting a passage
    carries that spelling in, and reading it as naming no case at all would
    send the claim to the mismatch check against whichever case it mentions
    next."""
    assert cases_named("Case C \u2011 541/23 P Polwax v Commission") == {
        "C-541/23"
    }
    assert cases_named("in Case C - 606/18 P") == {"C-606/18"}


def test_prose_naming_no_case_yields_nothing():
    assert cases_named("An Advocate General's Opinion states the principle") == set()


def test_a_paragraph_or_article_number_is_not_a_case():
    """Bare numbers are everywhere in legal prose; only the court-and-year
    shape counts."""
    assert cases_named("paragraph 87 of Article 20(4) of Regulation 1/2003") == set()


# ── mismatches: naming one authority while resting on another ────────────────

NEXANS = Source(key="d-606", identifiers=("C-606/18 P",), label="62018CJ0606.md")
PRYSMIAN = Source(key="d-601", identifiers=("C-601/18 P",), label="62018CJ0601.md")
CASINO_GC = Source(key="d-249", identifiers=("T-249/17",), label="62017TJ0249.md")
CASINO_CJ = Source(key="d-690", identifiers=("C-690/20 P",), label="62020CJ0690.md")


def _mismatches(claims, sources):
    return mismatches([Claim(s, t) for s, t in claims], sources)


def test_a_claim_naming_only_a_case_it_does_not_cite_is_reported():
    """The defect this exists for: the reader is sent to one judgment and the
    evidence is another."""
    found = _mismatches(
        [(7, "Continuing the examination is permissible, per paragraph 87 of "
             "Nexans France and Nexans v Commission (C-606/18 P), only where "
             "the Commission can legitimately consider it justified.")],
        {7: [PRYSMIAN]},
    )
    assert [(m.seq, m.named, m.cited) for m in found] == [
        (7, ("C-606/18",), ("C-601/18",))
    ]


def test_no_cue_word_is_needed():
    """A written case number is itself the attribution. The cue vocabulary
    governs the naming check and has no say here, which is what lets this
    reach a claim that attributes with `per paragraph 87 of`."""
    found = _mismatches([(1, "per paragraph 87 of (C-606/18 P)")], {1: [PRYSMIAN]})
    assert len(found) == 1


def test_a_claim_that_names_the_case_it_cites_is_clean():
    found = _mismatches(
        [(1, "In Case T-249/17 the General Court annulled the decision")],
        {1: [CASINO_GC]},
    )
    assert found == []


def test_naming_a_second_case_beside_the_cited_one_is_clean():
    """Ordinary legal writing: an appeal relation, a case the cited judgment
    itself cites, a case being distinguished. The reader has the thread."""
    found = _mismatches(
        [(1, "On appeal in Casino v Commission (C-690/20 P), the Court of "
             "Justice set aside the judgment in Case T-249/17.")],
        {1: [CASINO_CJ]},
    )
    assert found == []


def test_a_claim_naming_no_case_is_not_reported():
    """Silence is the naming check's business, not this one's."""
    found = _mismatches(
        [(1, "An Advocate General's Opinion states the principle")],
        {1: [PRYSMIAN]},
    )
    assert found == []


def test_a_suffix_difference_is_not_a_mismatch():
    found = _mismatches([(1, "In Case C-601/18 the Court held")], {1: [PRYSMIAN]})
    assert found == []


def test_joined_cases_stored_on_one_document_need_only_one_named():
    ceske = Source(
        key="d-538",
        identifiers=("C-538/18 P", "C-539/18 P"),
        label="62018CJ0538.md",
    )
    found = _mismatches(
        [(1, "The Court dismissed the appeal in Case C-538/18 P")], {1: [ceske]}
    )
    assert found == []


def test_the_rule_is_per_claim_not_per_source():
    """A claim resting on a judgment and its appeal names one of them. Judged
    per source the unnamed one would fire, and that is ordinary writing."""
    found = _mismatches(
        [(1, "On appeal in Casino v Commission (C-690/20 P) the Court set "
             "aside the General Court's judgment")],
        {1: [CASINO_CJ, CASINO_GC]},
    )
    assert found == []


def test_a_source_with_no_identifier_is_out_of_scope():
    bare = Source(key="d-0", identifiers=(), label="chapter.md")
    assert _mismatches([(1, "as held in Case C-606/18 P")], {1: [bare]}) == []


def test_a_source_whose_identifier_names_no_case_is_out_of_scope():
    """A book chapter or a merger reference cannot be compared against a case
    number, and a claim citing one may name any case it likes."""
    merger = Source(key="d-m", identifiers=("COMP/M.1234",), label="m.md")
    assert _mismatches([(1, "as held in Case C-606/18 P")], {1: [merger]}) == []


def test_a_claim_citing_nothing_has_no_case_to_be_judged_against():
    assert _mismatches([(1, "as held in Case C-606/18 P")], {}) == []


def test_each_offending_claim_is_reported_once():
    found = _mismatches(
        [(1, "per (C-606/18 P)"), (2, "and per (C-606/18 P) again")],
        {1: [PRYSMIAN], 2: [PRYSMIAN]},
    )
    assert [m.seq for m in found] == [1, 2]


# ── the mismatch message ─────────────────────────────────────────────────────


def test_mismatch_message_names_both_the_written_case_and_the_cited_one():
    """A reviser told only that a claim is wrong cannot tell which half to
    change, so both halves are given and both repairs are offered."""
    text = mismatch_message(
        Mismatch(seq=7, named=("C-606/18",), cited=("C-601/18",),
                 labels=("62018CJ0601.md",))
    )
    assert "C-606/18" in text
    assert "C-601/18" in text
    assert "62018CJ0601.md" in text
    assert "7" in text
