"""A cited authority the collection records as superseded should say so.

An answer resting on case law has three things to get right: cite the leading
authority, cite the most recent decision that confirms it, and never rely on
superseded case law without flagging it. This covers the third. The first two
need a stance on citation edges that nothing extracts, and are not attempted
here.

The filter is the whole feature. A `supersedes` edge can point at an entity
that is not a source at all, such as a bare common noun that extraction made
into an entity. That target is mentioned wherever the word appears, so read
without a filter the check reports superseded authority in documents that
cite nothing superseded, because a generic string became an entity and the
entity acquired a relationship.

Run from the backend directory:
`python -m pytest tests/test_a_superseded_authority_is_flagged.py`
"""

from __future__ import annotations

from app.services.supersession import identified, message

# The patterns a deployment declares for its decision classes. Not hard-coded
# in the service: they are read from document_class.identifier_pattern.
COURT = r"\b([CTF])\s?-\s?0*(\d{1,4})/(\d{2})\b|\b(\d{2})-(\d{2})\.(\d{3})\b"
REG = r"\b(?:COMP/)?(M|AT|SA)\.0*(\d{1,5})\b|\b(\d{2})-(DCC|D|A|MC)-0*(\d{1,3})\b"


def _row(superseded, pattern=COURT, filename="a.md", superseding="Later v Case"):
    return {"filename": filename, "superseded": superseded,
            "superseding": superseding, "identifier_pattern": pattern}


def test_a_named_case_is_kept():
    rows = [_row("Case T-451/20 Kestrel Holdings v Northmoor Authority")]
    assert len(identified(rows)) == 1


def test_the_generic_entity_that_carries_the_relationship_is_dropped():
    """A bare common noun that carries the relationship names no authority,
    however often it is mentioned."""
    assert identified([_row("Decision")]) == []


def test_a_year_and_a_noun_is_not_an_identifier():
    for junk in ("2014 Decision", "2002 decision", "1974 Order", "Opinion No. 551"):
        assert identified([_row(junk)]) == [], junk


def test_a_regulatory_reference_matches_its_own_class_pattern():
    assert len(identified([_row("Commission Decision COMP/M.4064", REG)])) == 1
    assert identified([_row("Commission Decision COMP/M.4064", COURT)]) == []


def test_a_class_with_no_declared_pattern_drops_the_row():
    """Not knowing what an identifier looks like is not a reason to assume."""
    assert identified([_row("Case T-451/20", pattern=None)]) == []
    assert identified([_row("Case T-451/20", pattern="")]) == []


def test_an_uncompilable_pattern_drops_the_row_rather_than_raising():
    """A bad pattern in the package must not take the gate down with it."""
    assert identified([_row("Case T-451/20", pattern="(unclosed")]) == []


def test_a_missing_name_is_dropped():
    assert identified([_row("")]) == []
    assert identified([_row(None)]) == []


def test_the_message_says_what_it_is_and_is_not():
    """It reports the record, not a legal judgement about the point."""
    m = message(_row("Case T-451/20", filename="an-earlier-decision.md",
                     superseding="Case C-123/45 Later"))
    assert "an-earlier-decision.md" in m
    assert "Case C-123/45 Later" in m
    assert "not a judgement" in m


def test_every_relationship_read_is_filtered_to_active_edges():
    """A rejected or withdrawn assertion keeps its row. `relationship_state`
    carries `counts_as_active`, and reading an edge without it tells a reader
    an authority is superseded on the strength of an assertion the graph has
    already refused.

    Counted rather than spot-checked, so a third relationship read added later
    fails here instead of shipping unguarded. It defends the width of the
    query, not its meaning: see the NULL convention asserted below.
    """
    import re

    from app.services.supersession import _CITED_SUPERSEDED

    sql = str(_CITED_SUPERSEDED)
    reads = len(re.findall(r"(?:FROM|JOIN)\s+relationship\s", sql))
    guards = sql.count("counts_as_active")
    assert reads == 2, f"expected two relationship reads, found {reads}"
    assert guards == reads, (
        f"{reads} relationship reads but {guards} active-state guards"
    )


def test_an_edge_with_no_state_counts_as_active():
    """The convention is the annotation walker's, in three places there, and
    is not re-decided here: a NULL `current_state_id` is active. Dropping
    those would silence the whole check on a graph whose edges were never
    given a state."""
    from app.services.supersession import _CITED_SUPERSEDED

    sql = str(_CITED_SUPERSEDED)
    assert sql.count("current_state_id IS NULL") == 2


def test_a_source_is_never_recorded_as_superseding_itself():
    """A supersession edge can run from an entity to that same entity. Here an
    interim order and the final judgment in one case both resolved to the
    case's entity, and the edge extracted between the two documents collapsed
    to a loop. Read as a finding, the loop told the drafter the deciding
    judgment of a question was "recorded as superseded by" itself, and the
    drafter dropped it. A citation the answer has to keep must not be lost to
    a loop.

    Pinned on the supersession read specifically, by position: the loop
    guard must sit in the WHERE of the outer query, where `r` is the
    supersession edge, not in the full-text CTE where it would mean nothing.
    """
    import re

    from app.services.supersession import _CITED_SUPERSEDED

    sql = str(_CITED_SUPERSEDED)
    outer = sql[sql.index("SELECT DISTINCT"):]
    assert re.search(r"r\.source_id\s*<>\s*r\.target_id", outer), (
        "the supersession read must exclude an edge whose two ends are the "
        "same entity")
