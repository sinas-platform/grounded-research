"""Words that became entities, and the test that separates them from names.

The extractor made entities out of common words: "Decision", adverbs such as
"Thus" and "Only" typed as companies, and entities named after their own
entity type. They acquire relationships like any other entity, which is how
they were found: an entity named "Decision" can be a target of `supersedes`.

Shape does not separate them. "Kestrel", "Northmoor" and "Ashgrove" are one
word each, and a legitimate name can appear in a large share of a
collection's documents. Case does: names are almost never written
lower-case, and common words almost always are. The measured figures are in
issue 1474 in the originating deployment's tracker.

Run from the backend directory:
`python -m pytest tests/test_a_common_noun_is_not_an_entity.py`
"""

from __future__ import annotations

from app.services.generic_entities import (LOWERCASE_SHARE, MIN_DOCUMENTS,
                                           case_evidence, is_generic, mark)

# Written to the shape real text has: a common word appears
# lower-case far more often than it begins a sentence, which is why the
# capitalised occurrences do not save it.
TEXT = ("The Harbour Authority adopted a decision. Thus the parties were "
        "informed, and the decision was published in Northmoor. Only the "
        "Authority may act, and thus the decision stands; the decision was "
        "thus final, and thus binding, and thus the parties complied. This "
        "decision concerns services and distribution.")


def test_a_proper_noun_is_never_written_lower_case():
    ev = case_evidence("Harbour Authority", TEXT)
    assert ev["as_written"] == 1
    assert ev["lowercase"] == 0
    assert ev["lowercase_share"] == 0.0
    assert not is_generic("Harbour Authority", 5000, ev)


def test_a_common_word_is_mostly_written_lower_case():
    ev = case_evidence("Decision", TEXT)
    assert ev["lowercase"] > ev["as_written"]
    assert is_generic("Decision", 5000, ev)


def test_a_sentence_initial_capital_is_not_a_name():
    """"Thus" is capitalised only where a sentence happens to start with it."""
    ev = case_evidence("Thus", TEXT)
    assert ev["as_written"] == 1
    assert ev["lowercase"] > ev["as_written"]
    assert is_generic("Thus", 4000, ev)


def test_an_even_split_is_left_alone():
    """A word genuinely written both ways is not evidence enough. The
    threshold is deliberately clear of the boundary rather than on it."""
    ev = case_evidence("Thus", "Thus it was so. And thus it remained.")
    assert ev["lowercase_share"] == 0.5
    assert not is_generic("Thus", 4000, ev)


def test_an_already_lower_case_name_needs_no_counting():
    ev = case_evidence("services", TEXT)
    assert ev["lowercase_share"] == 1.0
    assert "canonical form" in ev["note"]
    assert is_generic("services", 3000, ev)


def test_a_rare_entity_is_left_alone_whatever_it_looks_like():
    """The junk that matters is common by construction."""
    ev = case_evidence("Decision", TEXT)
    assert not is_generic("Decision", MIN_DOCUMENTS - 1, ev)


def test_an_unmeasurable_name_is_left_alone():
    """No occurrences means no evidence, which is not evidence of guilt."""
    ev = case_evidence("Ashgrove", TEXT)
    assert ev["lowercase_share"] is None
    assert not is_generic("Ashgrove", 9999, ev)


def test_the_threshold_sits_clear_of_both_populations():
    """Above an even split, so a word written both ways is left alone, and
    short of always, so a word that sometimes opens a sentence is still
    caught. Where it sits between those was set clear of the populations
    recorded in issue 1474 in the originating deployment's tracker."""
    assert 0.5 < LOWERCASE_SHARE < 1.0


def test_a_partial_word_is_not_a_match():
    """"Case" must not be found inside "Cases" or "staircase"."""
    ev = case_evidence("Case", "Cases and staircase and lowercase.")
    assert ev["as_written"] == 0 and ev["lowercase"] == 0


def test_the_mark_carries_its_own_reasons():
    ev = case_evidence("Decision", TEXT)
    m = mark("Decision", 5000, ev, "2026-09-10")["generic_term"]
    assert m["documents"] == 5000
    assert m["marked_at"] == "2026-09-10"
    assert str(MIN_DOCUMENTS) in m["test"]
    assert "evidence" in m["not_deleted"]


def test_the_strict_test_needs_no_corpus_at_all():
    """A lower-case canonical form is what the extractor saw. Nothing to weigh."""
    from app.services.generic_entities import written_as_a_word

    for word in ("services", "national", "distribution", "support", "power", "control"):
        assert written_as_a_word(word), word


def test_a_lower_case_word_is_lower_case_in_any_script():
    """`re.match(r"^[a-z]", name)` is a question about the ASCII range, not
    about the character. Every word here is a lower-case common noun that the
    check read as a name and left as an entity."""
    from app.services.generic_entities import written_as_a_word

    for word in ("état", "échange", "établissement", "édition", "égalité",
                 "ökonomie", "überschuss", "época"):
        assert written_as_a_word(word), word


def test_an_accented_name_is_still_spared():
    """The widening must not start marking names. A capital is a capital
    whatever letter carries it."""
    from app.services.generic_entities import written_as_a_word

    for name in ("État", "Établissements Ashgrove", "Élysée", "Ökonomie"):
        assert not written_as_a_word(name), name


def test_a_word_is_a_whole_word_in_any_script():
    """The boundary was `[A-Za-z]`, so an accented letter did not count as a
    letter and a name could match inside a longer word that continues with
    one."""
    # No occurrence at all: the function says so by returning no share.
    assert case_evidence("Kestrel", "the Kestrelé filing")[
        "lowercase_share"] is None
    assert case_evidence("Kestrel", "the Kestrel filing")["any_case"] == 1


def test_decomposed_text_counts_the_same_as_composed():
    """A combining mark is not a letter to the boundary, so in decomposed
    text a name matches inside a longer word that the composed form correctly
    rejects. Which form arrives depends on where the text was extracted, and
    a count must not vary with that."""
    composed = "the Kestrel\u00e9 filing"
    decomposed = "the Kestrele\u0301 filing"
    assert composed != decomposed
    for t in (composed, decomposed):
        assert case_evidence("Kestrel", t)["lowercase_share"] is None, t
    # The name can arrive decomposed too, and is still the same name: it is
    # found in composed text and reported as never written lower-case.
    # Without the fold it is not found at all and the share is None, which
    # reads as "this word does not appear" rather than "it appears and is
    # always capitalised".
    assert case_evidence("E\u0301tat", "the \u00c9tat filing")[
        "lowercase_share"] == 0.0


def test_composing_is_a_check_before_it_is_a_copy():
    """The case tier hands the same sample of whole documents to every
    candidate, so an unconditional normalise rebuilt a large string once per
    entity. Already-composed text must come back as the same object."""
    from app.services.generic_entities import _composed

    composed = "the \u00c9tat filing"
    assert _composed(composed) is composed
    decomposed = "the E\u0301tat filing"
    assert _composed(decomposed) == composed


def test_the_candidate_query_does_not_decide_what_lower_case_means():
    """The SQL used to ask `~ '^[a-z]'`, which made the Python test below it
    unreachable for the words it was widened to catch. The question is asked
    once now, where the character can be asked."""
    from app.services.generic_entities import _CANDIDATES

    sql = str(_CANDIDATES)
    assert "'^[a-z]'" not in sql
    assert "lower(e.canonical_form)" in sql, "the cheap half stays"


def test_the_strict_test_spares_everything_written_as_a_name():
    from app.services.generic_entities import written_as_a_word

    for name in ("Harbour Authority", "Northmoor", "HPCA", "Decision", "Only",
                 "Thus", "Court", "Ashgrove", "eKestrel"):
        assert not written_as_a_word(name), name


def test_the_strict_test_is_a_subset_of_the_wider_one():
    """Whatever the strict test marks, the wider test would mark too."""
    from app.services.generic_entities import written_as_a_word

    for word in ("services", "national"):
        ev = case_evidence(word, "some text about services and national things")
        assert written_as_a_word(word)
        assert is_generic(word, MIN_DOCUMENTS, ev)


def test_a_type_whose_names_are_common_nouns_is_excluded_as_a_rule():
    """A relevant market is named by a common noun phrase because that is what
    a market is. The case test cannot tell a correctly recorded market from a
    mistakenly recorded one, so it is not asked to."""
    from app.services.generic_entities import EXCLUDED_TYPES, written_as_a_word

    assert "Relevant Market" in EXCLUDED_TYPES
    # the test would otherwise fire on all of these, and should not
    for market in ("retail market", "upstream market", "wholesale supply"):
        assert written_as_a_word(market), market


def test_the_exclusion_is_by_type_not_by_name():
    """A rule, not a list of entities: one judgement per entity would be the
    thing a mark exists to avoid."""
    from app.services.generic_entities import EXCLUDED_TYPES

    assert all(isinstance(t, str) for t in EXCLUDED_TYPES)
    assert "Company / Undertaking" not in EXCLUDED_TYPES
    assert "Competition Authority" not in EXCLUDED_TYPES


# Patterns as a deployment declares them, read from document_class at run time.
FR = r"n°\s?\d{2}-\d{3,4}"
EU = r"\b(?:Regulation|Directive)\s+(?:\(EU\)\s+)?No\s+\d+/\d+"


def test_a_french_instrument_is_spared_although_it_is_lower_case():
    """Case separates names from words in English and nothing in French."""
    from app.services.generic_entities import carries_an_identifier, written_as_a_word

    name = "décret n° 01-999 du 31 février 2001"
    assert written_as_a_word(name), "lower case, so the case test would mark it"
    assert carries_an_identifier(name, [FR, EU]), "and the identifier saves it"


def test_junk_carries_no_identifier_in_any_language():
    from app.services.generic_entities import carries_an_identifier

    for junk in ("paragraph 49", "décision attaquée", "record", "world",
                 "federal district court", "firms"):
        assert not carries_an_identifier(junk, [FR, EU]), junk


def test_no_declared_pattern_spares_nothing():
    """A deployment that declares no identifier cannot use this to spare."""
    from app.services.generic_entities import carries_an_identifier

    assert not carries_an_identifier("décret n° 01-999", [])
    assert not carries_an_identifier("décret n° 01-999", None)


def test_an_uncompilable_pattern_does_not_spare_and_does_not_raise():
    from app.services.generic_entities import carries_an_identifier

    assert not carries_an_identifier("décret n° 01-999", ["(unclosed"])
