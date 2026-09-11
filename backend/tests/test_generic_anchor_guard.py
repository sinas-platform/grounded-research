"""A generic-marked entity is never force-picked as an anchor, and its
mentions count only where a model recognised them.

The other half of marking generic entities: the mark existed and nothing
read it, so an anchor on a marked entity still turned into every document
containing the word. These pin the three consumers — the anchor pick, the
planner's match line, and the mention channel — without a database.

Run from the backend directory:
`python -m pytest tests/test_generic_anchor_guard.py`
"""

import inspect

from app import retrieval_first as rf


def _match(i, docs, generic=False):
    return {"id": f"e{i}", "value": f"name{i}", "type": "Company / Undertaking",
            "docs": docs, "generic": generic, "validated_docs": 2}


# -- the anchor pick ----------------------------------------------------------


def test_the_union_never_force_picks_a_generic_entity():
    """The old union took the top matches by document count — which selects
    a generic term precisely for being junk: ubiquity read as strength."""
    matches = {m["id"]: m for m in [
        _match(1, 14704, generic=True), _match(2, 120), _match(3, 80)]}
    picked = rf._pick_anchors([], matches)
    assert "e1" not in picked
    assert picked == ["e2", "e3"]


def test_the_model_may_still_pick_a_generic_entity_deliberately():
    """A question genuinely about the marked entity is the one case the
    mark must not foreclose. The model saw the label; its pick stands."""
    matches = {m["id"]: m for m in [_match(1, 14704, generic=True), _match(2, 120)]}
    assert rf._pick_anchors(["e1"], matches) == ["e1", "e2"]


def test_a_model_pick_outside_the_matches_is_dropped():
    matches = {m["id"]: m for m in [_match(2, 120)]}
    assert rf._pick_anchors(["nonsense"], matches) == ["e2"]


def test_at_most_six_are_unioned_beyond_the_picks():
    matches = {m["id"]: m for m in [_match(i, 100 - i) for i in range(1, 10)]}
    assert len(rf._pick_anchors([], matches)) == 6


def test_the_union_is_ordered_strongest_first():
    matches = {m["id"]: m for m in [_match(1, 10), _match(2, 500), _match(3, 90)]}
    assert rf._pick_anchors([], matches) == ["e2", "e3", "e1"]


# -- the wiring, pinned by reading the source ---------------------------------

SRC = inspect.getsource(rf)


def test_the_match_line_says_what_a_generic_entity_is():
    """The model can only decline to anchor junk if it is told which match
    is junk, in the line where it reads the match."""
    assert "GENERIC TERM: matches the word, almost never the thing" in SRC


def test_the_mention_channel_gates_generic_entities_to_validated_tiers():
    assert "metadata ? 'generic_term'" in SRC
    assert "m.link_method = ANY(:validated)" in SRC


def test_the_validated_tiers_are_the_context_reading_ones():
    """`created` and `adjudicated` rows came from a model that read the
    document; the gazetteer's rows are string matches. Pinned so a tier is
    added here as a decision, not by accident."""
    assert rf.VALIDATED_LINK_METHODS == ("created", "adjudicated")


# -- the second-tier marker ----------------------------------------------------

from app.services import generic_entities as ge


def test_tier2_reads_a_deterministic_sample():
    """md5-ordered, so a re-run reads the same documents and a mark is
    reproducible rather than a function of ORDER BY chance."""
    assert "ORDER BY md5(d.id::text)" in str(ge._SAMPLE_TEXT)


def test_tier2_candidates_are_single_capitalised_words():
    """Multi-word names have no single lowercase-share; already-lowercase
    forms are tier 1's. This tier is exactly the complement."""
    src = str(ge._TIER2_CANDIDATES)
    assert "~ '^[A-Za-z]+$'" in src
    assert "<> lower(e.canonical_form)" in src


def test_an_unmeasured_word_is_not_judged():
    """A word the sample barely carries is left unmarked, not marked on
    noise — the same absence-over-arbitrary rule as everywhere else."""
    import inspect
    src = inspect.getsource(ge.mark_generic_by_case)
    assert "seen < min_occurrences" in src
    assert "unmeasured += 1" in src


def test_tier2_marks_through_the_same_judgement_as_tier1():
    import inspect
    src = inspect.getsource(ge.mark_generic_by_case)
    assert "is_generic(name, docs, evidence)" in src
    assert "mark(name, docs, evidence, when)" in src


# -- the primary signal: link probability --------------------------------------


def test_the_measured_poles_sit_far_from_the_floor():
    """THUS: 2 recognised of 14,704 matched. A real undertaking: nearly all.
    The floor must reject the first and never threaten the second."""
    assert ge.improbable_link(14704, 2) is True
    assert ge.improbable_link(300, 290) is False


def test_a_small_population_is_never_judged():
    """2 recognised of 40 matched is a rare entity, not a word — the ratio
    means nothing below MIN_DOCUMENTS."""
    assert ge.improbable_link(40, 0) is False


def test_the_floor_is_two_percent():
    """Pinned so moving it is a decision, not an accident."""
    assert ge.LINK_PROBABILITY_FLOOR == 0.02
    assert ge.improbable_link(1000, 19) is True
    assert ge.improbable_link(1000, 21) is False


def test_link_probability_needs_no_orthography():
    """The reason this is the primary signal: German capitalises every noun
    and French legal names are lower-case — the case tiers are blind in one
    and carve exceptions in the other. This function reads two counts."""
    import inspect
    src = inspect.getsource(ge.improbable_link)
    assert ".lower(" not in src and ".upper(" not in src and "re." not in src
