"""Words that became entities, and the test that separates them from names.

Measured on a 35,407-document corpus on 10 September 2026: an entity named
"Decision" with 19,311 document mentions, "Thus" and "Only" typed as
undertakings with 14,706 and 18,804, four entities named after their own
entity type. They acquire relationships like any other entity, which is how
they were found: "Decision" is one of the 149 targets of `supersedes`.

Shape does not separate them. "Google", "Nexans" and "Servier" are one word;
"European Commission" appears in 53.8% of this corpus and is legitimate. Case
does: measured over 11.2 MB, "European Commission", "TFEU" and "France" are
never written lower-case, "Council" 7% of the time, while "Must" and "Will"
are lower-case in 100% of occurrences and "Decision" in 86%.

Run from the backend directory:
`python -m pytest tests/test_a_common_noun_is_not_an_entity.py`
"""

from __future__ import annotations

from app.services.generic_entities import (LOWERCASE_SHARE, MIN_DOCUMENTS,
                                           case_evidence, is_generic, mark)

# Written to the shape the corpus actually has: a common word appears
# lower-case far more often than it begins a sentence, which is why the
# capitalised occurrences do not save it.
TEXT = ("The European Commission adopted a decision. Thus the parties were "
        "informed, and the decision was published in France. Only the "
        "Commission may act, and thus the decision stands; the decision was "
        "thus final, and thus binding, and thus the parties complied. This "
        "decision concerns services and distribution.")


def test_a_proper_noun_is_never_written_lower_case():
    ev = case_evidence("European Commission", TEXT)
    assert ev["as_written"] == 1
    assert ev["lowercase"] == 0
    assert ev["lowercase_share"] == 0.0
    assert not is_generic("European Commission", 19032, ev)


def test_a_common_word_is_mostly_written_lower_case():
    ev = case_evidence("Decision", TEXT)
    assert ev["lowercase"] > ev["as_written"]
    assert is_generic("Decision", 19311, ev)


def test_a_sentence_initial_capital_is_not_a_name():
    """"Thus" is capitalised only where a sentence happens to start with it."""
    ev = case_evidence("Thus", TEXT)
    assert ev["as_written"] == 1
    assert ev["lowercase"] > ev["as_written"]
    assert is_generic("Thus", 14706, ev)


def test_an_even_split_is_left_alone():
    """A word genuinely written both ways is not evidence enough. The
    threshold is deliberately clear of the boundary rather than on it."""
    ev = case_evidence("Thus", "Thus it was so. And thus it remained.")
    assert ev["lowercase_share"] == 0.5
    assert not is_generic("Thus", 14706, ev)


def test_an_already_lower_case_name_needs_no_counting():
    ev = case_evidence("services", TEXT)
    assert ev["lowercase_share"] == 1.0
    assert "canonical form" in ev["note"]
    assert is_generic("services", 13894, ev)


def test_a_rare_entity_is_left_alone_whatever_it_looks_like():
    """The junk that matters is common by construction."""
    ev = case_evidence("Decision", TEXT)
    assert not is_generic("Decision", MIN_DOCUMENTS - 1, ev)


def test_an_unmeasurable_name_is_left_alone():
    """No occurrences means no evidence, which is not evidence of guilt."""
    ev = case_evidence("Nexans", TEXT)
    assert ev["lowercase_share"] is None
    assert not is_generic("Nexans", 9999, ev)


def test_the_threshold_sits_clear_of_both_populations():
    """Highest legitimate name measured 7%, lowest junk 76%."""
    assert 0.07 < LOWERCASE_SHARE < 0.76


def test_a_partial_word_is_not_a_match():
    """"Case" must not be found inside "Cases" or "staircase"."""
    ev = case_evidence("Case", "Cases and staircase and lowercase.")
    assert ev["as_written"] == 0 and ev["lowercase"] == 0


def test_the_mark_carries_its_own_reasons():
    ev = case_evidence("Decision", TEXT)
    m = mark("Decision", 19311, ev, "2026-09-10")["generic_term"]
    assert m["documents"] == 19311
    assert m["marked_at"] == "2026-09-10"
    assert str(MIN_DOCUMENTS) in m["test"]
    assert "evidence" in m["not_deleted"]


def test_the_strict_test_needs_no_corpus_at_all():
    """A lower-case canonical form is what the extractor saw. Nothing to weigh."""
    from app.services.generic_entities import written_as_a_word

    for word in ("services", "national", "distribution", "support", "power", "control"):
        assert written_as_a_word(word), word


def test_the_strict_test_spares_everything_written_as_a_name():
    from app.services.generic_entities import written_as_a_word

    for name in ("European Commission", "France", "TFEU", "Decision", "Only",
                 "Thus", "Court", "Nexans", "eBay"):
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
    for market in ("retail market", "upstream market", "resale price maintenance"):
        assert written_as_a_word(market), market


def test_the_exclusion_is_by_type_not_by_name():
    """A rule, not a list of entities: 229 individual judgements would be the
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

    name = "ordonnance n° 86-1243 du 1er décembre 1986"
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

    assert not carries_an_identifier("ordonnance n° 86-1243", [])
    assert not carries_an_identifier("ordonnance n° 86-1243", None)


def test_an_uncompilable_pattern_does_not_spare_and_does_not_raise():
    from app.services.generic_entities import carries_an_identifier

    assert not carries_an_identifier("ordonnance n° 86-1243", ["(unclosed"])
