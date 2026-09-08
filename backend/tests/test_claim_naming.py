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
    identifier_core,
    identifier_key,
    identifiers_named,
    message,
    mismatch_message,
    mismatches,
    review,
)

# The shape a deployment declares. The module knows nothing about it: these
# tests supply one the way a package would, and any other scheme with capture
# groups would exercise the same code. Written to capture the parts that
# identify and to leave out a trailing suffix, so two values differing only by
# that suffix compare equal without the module knowing what a suffix is.
SHAPE = r"\b([A-Z])\s?[-\u2010-\u2015\u2212]\s?0*(\d{1,4})/(\d{2})\b"

CUES = frozenset({"held", "found", "ruled"})
SRC = Source(key="doc-1", identifiers=("X-111/22",), label="a.md", pattern=SHAPE)
OTHER = Source(key="doc-2", identifiers=("Y-333/44",), label="b.md", pattern=SHAPE)


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
    bare = Source(key="doc-3", identifiers=(), label="c.md", pattern=SHAPE)
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
    base = identifier_key("C-606/18", SHAPE)
    assert identifier_key("C-606/18 P", SHAPE) == base
    assert identifier_key("C-65/18 P(R)", SHAPE) == identifier_key("C-65/18", SHAPE)
    assert identifier_key("T-1097/23 R-RENV", SHAPE) == identifier_key("T-1097/23", SHAPE)


def test_leading_zeros_are_the_shape_s_business_not_the_module_s():
    """One deployment writes a padded number and another does not. SHAPE says
    the padding does not distinguish, by consuming it outside the group, and
    the module never learns that zeros are padding."""
    assert identifier_key("C-010/18", SHAPE) == identifier_key("C-10/18 P", SHAPE)
    unpadded = r"\b([A-Z])-(\d{1,4})/(\d{2})\b"
    assert identifier_key("C-010/18", unpadded) != identifier_key("C-10/18", unpadded)


def test_identity_keeps_the_scheme_letter():
    """T-449/14 and C-449/14 are different cases before different courts."""
    assert identifier_key("T-449/14", SHAPE) != identifier_key("C-449/14", SHAPE)


def test_identity_of_an_identifier_that_names_no_case_is_none():
    """Merger and national references are identifiers, but not of the shape
    this check can read, so it declines to read them."""
    assert identifier_key("COMP/M.1234", SHAPE) is None
    assert identifier_key("11-D-17", SHAPE) is None
    assert identifier_key("509 U.S. 209", SHAPE) is None


# ── identifiers_named: what a claim writes, read with the declared shape ─────


def test_an_identifier_in_parentheses_is_found():
    got = identifiers_named("Nexans v Commission (C-606/18 P), paragraph 87", SHAPE)
    assert set(got.values()) == {"C-606/18"}


def test_the_prefixed_form_is_found():
    got = identifiers_named("The General Court, in Case T-249/17, reasoned", SHAPE)
    assert set(got.values()) == {"T-249/17"}


def test_several_are_all_found():
    got = identifiers_named("T-125/03 and T-253/03 were heard together", SHAPE)
    assert set(got.values()) == {"T-125/03", "T-253/03"}


def test_one_spaced_around_its_hyphen_is_found():
    """Judgment text as published writes the separator with spaces, and a
    claim quoting a passage carries that spelling in."""
    got = identifiers_named("in Case C \u2011 541/23 P the Court", SHAPE)
    assert len(got) == 1


def test_prose_naming_none_yields_nothing():
    assert identifiers_named("The Court held that the seal was broken.", SHAPE) == {}


def test_a_paragraph_or_article_number_is_not_an_identifier():
    """The shape requires a letter, a number and a year. `Article 20(4)` and
    `paragraph 87` hold no year and are not identifiers under it."""
    assert identifiers_named("Article 20(4), paragraph 87, Regulation 1/2003", SHAPE) == {}


# ── identifier_key: what makes two spellings the same identifier ─────────────


def test_a_value_that_does_not_match_the_shape_has_no_key():
    assert identifier_key("inspections-eu-law-2nd.md", SHAPE) is None
    assert identifier_key("COMP/M.11936", SHAPE) is None


def test_the_key_is_anchored_so_a_value_must_be_an_identifier():
    """A value merely containing something shaped like one is not one. Being
    strict can only shrink the set a claim is judged against, which loses a
    finding rather than inventing one."""
    assert identifier_key("see T-249/17", SHAPE) is None


def test_what_the_shape_does_not_capture_does_not_distinguish():
    """The contract. SHAPE captures letter, number and year and stops, so a
    trailing suffix is not part of identity, and this module never learns what
    the suffix means."""
    assert identifier_key("C-606/18 P", SHAPE) == identifier_key("C-606/18", SHAPE)
    assert identifier_key("C-606/18 P-DEP", SHAPE) == identifier_key("C-606/18", SHAPE)


def test_what_the_shape_does_capture_does_distinguish():
    """The other half of the same contract: the scheme letter is captured, so
    two schemes are two identifiers."""
    assert identifier_key("T-449/14", SHAPE) != identifier_key("C-449/14", SHAPE)


def test_a_claim_and_a_stored_value_meet_on_the_same_key():
    named = identifiers_named("as C-606/18 P held", SHAPE)
    assert set(named) == {identifier_key("C-606/18", SHAPE)}


def test_a_pattern_that_captures_nothing_keys_on_the_whole_match():
    """A deployment may declare a shape with no groups. Then the match itself
    is the identity, which is the only reading available."""
    whole = r"INV-\d{4}"
    assert identifier_key("INV-1234", whole) == identifier_key("INV-1234", whole)
    assert identifier_key("INV-1234", whole) != identifier_key("INV-5678", whole)


NEXANS = Source(key="d-606", identifiers=("C-606/18 P",),
                label="62018CJ0606.md", pattern=SHAPE)
PRYSMIAN = Source(key="d-601", identifiers=("C-601/18 P",),
                  label="62018CJ0601.md", pattern=SHAPE)
CASINO_GC = Source(key="d-249", identifiers=("T-249/17",),
                   label="62017TJ0249.md", pattern=SHAPE)
CASINO_CJ = Source(key="d-690", identifiers=("C-690/20 P",),
                   label="62020CJ0690.md", pattern=SHAPE)


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
    # `cited` shows what the source calls itself, not the key it reduced to:
    # a reader is told the identifier, not the comparison.
    assert [(m.seq, m.named, m.cited) for m in found] == [
        (7, ("C-606/18",), ("C-601/18 P",))
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
        pattern=SHAPE,
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
    bare = Source(key="d-0", identifiers=(), label="chapter.md", pattern=SHAPE)
    assert _mismatches([(1, "as held in Case C-606/18 P")], {1: [bare]}) == []


def test_a_source_whose_identifier_names_no_case_is_out_of_scope():
    """A book chapter or a merger reference cannot be compared against a case
    number, and a claim citing one may name any case it likes."""
    merger = Source(key="d-m", identifiers=("COMP/M.1234",), label="m.md", pattern=SHAPE)
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


# ── a check that cannot run says so where its findings go ────────────────────


def test_the_unshaped_message_names_the_classes_and_refuses_to_read_as_none():
    """The line has to be unmistakable to someone who was not looking for it:
    it names the classes, says no claim of them was examined, and says in
    words that this is not a finding of none."""
    import asyncio

    from app.services import claim_naming as cn

    async def two(): return ["Court Decision", "Regulatory Decision"]
    real, cn.unshaped_classes = cn.unshaped_classes, two
    try:
        msg = asyncio.run(cn.unshaped_message())
    finally:
        cn.unshaped_classes = real
    assert "Court Decision" in msg and "Regulatory Decision" in msg
    assert "not a finding of none" in msg


def test_no_message_when_every_opted_in_class_can_be_read():
    import asyncio

    from app.services import claim_naming as cn

    async def none(): return []
    real, cn.unshaped_classes = cn.unshaped_classes, none
    try:
        assert asyncio.run(cn.unshaped_message()) is None
    finally:
        cn.unshaped_classes = real


def test_a_failure_to_check_is_silent_rather_than_fatal():
    """This is a quality note. It must not be the thing that fails a run."""
    import asyncio

    from app.services import claim_naming as cn

    async def boom(): raise RuntimeError("no database")
    real, cn.unshaped_classes = cn.unshaped_classes, boom
    try:
        assert asyncio.run(cn.unshaped_message()) is None
    finally:
        cn.unshaped_classes = real


# ── the loaders and the assembler must agree on how many columns there are ───


def test_every_loader_selects_what_the_assembler_unpacks():
    """The bug this pins cost the naming check entirely and said nothing.

    `_assemble` was widened to take the identifier pattern, `_LOAD_IDENTIFIED`
    was given the column and `_LOAD` was not, so `findings_for` built a
    five-tuple for a six-tuple unpack, raised on every answer, and was caught
    by a handler that returns an empty list. 157 findings became 0 with
    nothing in the gate output to show it.

    Counting columns is the cheap structural check that catches the whole
    class: a loader that feeds the assembler must select what it unpacks.
    """
    import inspect
    import re

    from app.services import claim_naming as cn

    body = inspect.getsource(cn._assemble)
    unpack = re.search(r"for (.+?) in rows:", body).group(1)
    wanted = len([x for x in unpack.split(",") if x.strip()])

    def selected(sql: str) -> int:
        head = sql[sql.index("select") + 6 : sql.index("from")]
        depth = 0
        cols = 1
        for ch in head:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                cols += 1
        return cols

    # _LOAD carries one extra column, the cues, which findings_for reads
    # directly rather than passing on.
    assert selected(str(cn._LOAD)) == wanted + 1, "_LOAD"
    assert selected(str(cn._LOAD_IDENTIFIED)) == wanted, "_LOAD_IDENTIFIED"


def test_a_crashed_check_says_so_where_its_findings_go():
    """An empty list is what a clean answer returns. A check that fell over
    must not be indistinguishable from one that passed."""
    import asyncio

    from app.services import claim_naming as cn

    async def boom(_):
        raise RuntimeError("no database")

    real_f, real_m = cn.findings_for, cn.mismatches_for
    cn.findings_for, cn.mismatches_for = boom, boom
    try:
        notes = asyncio.run(cn.issues_for("a"))
        found, failed = asyncio.run(cn.safe_mismatches_for("a"))
    finally:
        cn.findings_for, cn.mismatches_for = real_f, real_m
    assert notes and "not a finding of none" in notes[0]
    assert found == [] and failed and "not a finding of none" in failed


# ── a source named in prose has been named ───────────────────────────────────


def test_the_distinguishing_words_are_measured_not_listed():
    """Across the names one answer cites, the words that appear in most of them
    distinguish nothing. Nothing here is a stopword list: in a corpus of
    invoices the common word would be `invoice` and this would say so."""
    from app.services.claim_naming import distinctive_words

    names = [
        "T-141/08 E.ON Energie v Commission",
        "C-89/11 P E.ON Energie v Commission",
        "Judgment of the Court, Nexans France v Commission",
        "Judgment of the Court, Prysmian v Commission",
        "Judgment of the Court, Orange v Commission",
    ]
    d = distinctive_words(names)
    assert "commission" not in d and "judgment" not in d and "court" not in d
    assert {"nexans", "prysmian", "orange"} <= d


def test_a_claim_writing_a_party_name_has_named_the_source():
    from app.services.claim_naming import carries_name, distinctive_words

    names = ["Nexans France v Commission", "Prysmian v Commission",
             "Orange v Commission", "Casino v Commission"]
    d = distinctive_words(names)
    assert carries_name("In Nexans France the Court held", names[0], d)
    assert not carries_name("The Commission held", names[0], d)


def test_a_claim_writing_only_the_common_part_has_not():
    """The largest group of wrong findings the naive version produces: every
    name here contains Commission, so writing it names nothing."""
    from app.services.claim_naming import carries_name, distinctive_words

    names = ["Nexans v Commission", "Prysmian v Commission", "Orange v Commission"]
    d = distinctive_words(names)
    assert not carries_name("the Commission decided", names[0], d)


def test_a_document_with_no_name_is_judged_on_its_identifier_alone():
    from app.services.claim_naming import carries_name

    assert carries_name("anything at all", "", {"anything"}) is False


def test_naming_in_prose_clears_a_finding_that_the_identifier_alone_would_raise():
    """The behaviour change, end to end: same claim, same source, and the only
    difference is that the class declares where the name lives."""
    src_named = Source(key="d-1", identifiers=("X-999/99",), label="a.md",
                       pattern=SHAPE, name="Ferriere Nord v Commission")
    src_bare = Source(key="d-1", identifiers=("X-999/99",), label="a.md",
                      pattern=SHAPE, name="")
    claim = [Claim(1, "In Ferriere Nord the Court held that the seal was broken")]
    cues = frozenset({"held"})
    assert review(claim, {1: [src_bare]}, cues), "no name: reported as unnamed"
    assert review(claim, {1: [src_named]}, cues) == [], "named in prose: clean"


# ── how far each check got ───────────────────────────────────────────────────


def test_reach_separates_not_looking_from_finding_nothing():
    """The three numbers exist because an empty finding list has three causes
    and they need telling apart."""
    from app.services.claim_naming import review_with_reach

    src = Source(key="d-1", identifiers=("X-111/22",), label="a.md",
                 pattern=SHAPE, name="")
    claims = [Claim(1, "The Court held X-111/22 was decided"),
              Claim(2, "This sentence attributes nothing at all")]
    _, reach = review_with_reach(claims, {1: [src], 2: [src]},
                                 frozenset({"held"}))
    # both claims cite a source with an identifier, so both were eligible;
    # only the attributing one was judged
    assert reach.eligible == 2 and reach.judged == 1


def test_reach_shows_a_word_list_that_reaches_almost_nothing():
    """The failure nothing caught for months: the check runs, judges honestly,
    and is eligible for far more than it sees."""
    from app.services.claim_naming import review_with_reach

    src = Source(key="d-1", identifiers=("X-111/22",), label="a.md",
                 pattern=SHAPE, name="")
    claims = [Claim(i, "The Court observed that something happened")
              for i in range(1, 11)]
    _, reach = review_with_reach(claims, {i: [src] for i in range(1, 11)},
                                 frozenset({"held"}))
    assert reach.eligible == 10 and reach.judged == 0 and reach.flagged == 0


def test_correspondence_reach_counts_what_it_could_compare():
    from app.services.claim_naming import mismatches_with_reach

    src = Source(key="d-1", identifiers=("C-601/18 P",), label="a.md",
                 pattern=SHAPE, name="")
    claims = [Claim(1, "In Case C-606/18 the Court held"),
              Claim(2, "A sentence naming no identifier at all")]
    found, reach = mismatches_with_reach(claims, {1: [src], 2: [src]})
    assert reach.eligible == 2, "both cite a source declaring a shape"
    assert reach.judged == 1, "only one writes an identifier to compare"
    assert reach.flagged == len(found) == 1


def test_reach_is_a_plain_dict_for_the_record():
    from app.services.claim_naming import Reach

    assert Reach(9, 4, 1).as_dict() == {"eligible": 9, "judged": 4, "flagged": 1}
