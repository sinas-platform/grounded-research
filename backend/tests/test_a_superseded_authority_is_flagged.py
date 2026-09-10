"""A cited authority the collection records as superseded should say so.

The reviewer's must-have has three parts and this is the third: never rely on
superseded case law without flagging it. The first two, citing the leading
authority together with the most recent decision confirming it, need a stance
on citation edges that nothing extracts, and are not attempted here.

The filter is the whole feature. Measured on 10 September 2026 the
`supersedes` relationship holds 188 edges over 149 targets reaching 21,327
mentions, and one target is an entity whose name is the bare word "Decision",
carrying 19,311 of them. Read without a filter the check reports nearly twenty
thousand mentions of superseded authority because a generic string became an
entity and the entity acquired a relationship.

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
    rows = [_row("Case T-451/20 Meta Platforms Ireland v Commission")]
    assert len(identified(rows)) == 1


def test_the_generic_entity_that_carries_the_relationship_is_dropped():
    """"Decision" is a real target of a real edge, with 19,311 mentions."""
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
    m = message(_row("Case T-451/20", filename="62020TJ0451.md",
                     superseding="Case C-123/45 Later"))
    assert "62020TJ0451.md" in m
    assert "Case C-123/45 Later" in m
    assert "not a judgement" in m
