"""Unit tests for the source-naming check.

Pure core only: `review` takes claims and their sources and returns findings,
so no DB and no network. The loader around it is exercised against the real
stack elsewhere, same convention as the other runner tests.

Run from the backend directory: `python -m pytest tests/test_claim_naming.py`
"""

from app.services.claim_naming import (
    Claim,
    Finding,
    Source,
    attributes,
    carries_identifier,
    identifier_core,
    message,
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
