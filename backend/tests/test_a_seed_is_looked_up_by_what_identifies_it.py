"""A seed the planner proposes is looked up by its identifier when it gives
one, by its whole name otherwise, and never by fragments of either.

The name resolver used to cut every name on "/" and "," and look each piece
up beside the whole: a case number `C-583/13` became `C-583` and `13`, a
merger `Gaz de France/Suez` became two utilities, and a fragment like
`Court of Justice` pulled in the six most-mentioned entities containing it.
Measured on two independent plannings of one question, half of every
resolved entity set was that noise and the halves differed — half the
run-to-run variance of retrieval, from a string rule.

Which part of a name identifies the thing is a judgement. The planner makes
it, in the round-1 call it already makes, by returning each seed as
`{"identifier": …, "name": …}`; nothing here parses a name.
"""

from __future__ import annotations

import inspect

from app import retrieval_first as rf


def test_an_identifier_is_the_key_when_the_planner_gives_one():
    assert rf.lookup_keys({"identifier": "C-583/13 P",
                           "title": "Kestrel Holdings v Northmoor Authority"}) == ["C-583/13 P"]


def test_the_whole_title_is_the_key_when_there_is_no_identifier():
    for item in ({"identifier": "", "title": "Kestrel Holdings v Northmoor Authority"},
                 {"title": "Kestrel Holdings v Northmoor Authority"},
                 {"name": "Kestrel Holdings v Northmoor Authority"},
                 "Kestrel Holdings v Northmoor Authority"):
        assert rf.lookup_keys(item) == ["Kestrel Holdings v Northmoor Authority"], item


def test_the_field_names_carry_no_domain():
    """The engine ships to every deployment; what a source is called is the
    deployment's business, said in its schema and its guidance."""
    for word in ("case", "decision", "part", "legal", "court"):
        assert word not in rf._ROUND1_PROMPT.lower(), word


def test_nothing_is_split_on_punctuation():
    """A slash or a comma inside a name is part of the name."""
    assert rf.lookup_keys("Gaz de France/Suez") == ["Gaz de France/Suez"]
    assert rf.lookup_keys({"identifier": "T-289/11, T-290/11", "title": "x"}) == ["T-289/11, T-290/11"]


def test_a_string_too_short_to_name_anything_is_not_looked_up():
    assert rf.lookup_keys("v") == []
    assert rf.lookup_keys({"identifier": "13", "title": "Ltd"}) == []
    assert rf.lookup_keys({"identifier": "13", "title": "Kestrel Holdings"}) == ["Kestrel Holdings"], (
        "an identifier too short to be one falls back to the name")


def test_whitespace_is_normalised_so_the_same_seed_is_one_key():
    assert rf.lookup_keys("  Kestrel   Holdings\n") == ["Kestrel Holdings"]


def test_the_resolver_never_cuts_a_name():
    src = inspect.getsource(rf._resolve_names) + inspect.getsource(rf.lookup_keys)
    assert 'replace("/"' not in src and 'split(",")' not in src, (
        "the fragment rule is back; it put six arbitrary entities per "
        "fragment into every plan")


def test_the_planner_is_asked_for_the_identifier():
    assert '"known_sources"' in rf._ROUND1_PROMPT
    assert '"identifier"' in rf._ROUND1_PROMPT and '"title"' in rf._ROUND1_PROMPT
    assert "known_sources" in rf._ROUND1_GROUPS[0]
