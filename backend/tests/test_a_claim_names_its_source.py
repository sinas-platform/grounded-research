"""A claim asserting a rule names the source it rests on.

The requirement is an expert reviewer's, and was asked for structurally
rather than lexically: every proposition carries a source, and where that
source is one the field treats as authority the claim must name it — the
parties and the reference — so a reader can look it up and weigh it.

The check this replaces looked for attribution WORDS, which were English
while about a third of the collection is French, so a claim resting on a
French judgment passed by never tripping it. The tests below pin the
language-neutral behaviour that fixes it.
"""

from app.services.naming import (
    NAMED_KINDS,
    names_source,
    objection,
    unnamed_sources,
)


def test_a_reference_is_found_whatever_the_language_around_it():
    """The identifier is the handle, and it reads the same in any language."""
    english = "In Case Q-471/08 the tribunal held that the protection applies."
    french = "Dans l'affaire Q-471/08, le tribunal a jugé que la protection..."
    for text in (english, french):
        assert names_source(text, "Q-471/08", "Kestrel Holdings inspection ruling")


def test_a_reference_is_found_however_it_is_punctuated_or_padded():
    """One reference, several legitimate spellings."""
    for text in ("see Q-471/08", "see Q 471/08", "see Q0471/08", "see Q471/08"):
        assert names_source(text, "Q-471/08", None), text


def test_a_source_is_named_by_its_parties_not_its_stored_title():
    """A person names a case by the words that identify it.

    The stored title carries apparatus — a defendant, punctuation, accents —
    and a claim that names the parties has named the source.
    """
    assert names_source(
        "In Zvezda Remesla the tribunal found otherwise.",
        None, "Zvëzda Řemesla inspection ruling")
    # The accents are in the corpus, not necessarily in the claim, and the
    # splitter must not lose the first letter of a word outside Latin-1.
    assert names_source(
        "In Zvëzda Řemesla the tribunal found otherwise.",
        None, "Zvezda Remesla inspection ruling")


def test_one_shared_word_is_not_a_naming():
    """Sharing a common word with a title identifies nothing."""
    assert not names_source(
        "The authority decided otherwise.", None, "Zvëzda Řemesla inspection ruling")


def test_an_unnamed_source_is_caught():
    """The failure the requirement exists for."""
    assert not names_source(
        "The authority may seal records on request.",
        "Q-903/12", "Zvezda Remesla")


def test_a_source_with_nothing_to_name_by_is_not_held_against_the_claim():
    """A document with neither identifier nor name cannot be named.

    That is a gap in what was ingested; no way of writing the sentence fixes
    it, so it is not the drafter's failure.
    """
    assert names_source("Anything at all.", None, None)
    assert names_source("Anything at all.", "", "")


def test_only_claims_that_assert_a_rule_are_held_to_it():
    """An inference rests on other claims and names no source of its own; a
    label is about the source; an abstention says there is nothing to name."""
    docs = {"a.md": {"identifier": "T-1/01", "name": "Alpha Kestrel ruling",
                     "naming_required": True}}
    held = [{"sequence": 1, "kind": k, "text": "No source here.",
             "cites": ["a.md"]} for k in NAMED_KINDS]
    assert len(unnamed_sources(held, docs)) == len(NAMED_KINDS)
    for kind in ("inference", "label", "abstention", "fact", "procedure"):
        free = [{"sequence": 1, "kind": kind, "text": "No source here.",
                 "cites": ["a.md"]}]
        assert unnamed_sources(free, docs) == []


def test_a_class_that_does_not_require_naming_is_inert():
    """Which sources must be named is the deployment's to declare."""
    docs = {"a.md": {"identifier": "T-1/01", "name": "Alpha Kestrel ruling",
                     "naming_required": False}}
    claims = [{"sequence": 1, "kind": "rule", "text": "No source here.",
               "cites": ["a.md"]}]
    assert unnamed_sources(claims, docs) == []


def test_the_report_says_which_source_was_not_named():
    """Feedback has to be actionable, so it carries the source, not a count."""
    docs = {
        "a.md": {"identifier": "T-1/01", "name": "Alpha Kestrel ruling",
                 "naming_required": True},
        "b.md": {"identifier": "C-2/02", "name": "Gamma Northmoor ruling",
                 "naming_required": True},
    }
    claims = [{"sequence": 4, "kind": "rule", "claim_id": "x",
               "text": "As stated in the Alpha Kestrel ruling, the rule is thus.",
               "cites": ["a.md", "b.md"]}]
    found = unnamed_sources(claims, docs)
    assert len(found) == 1
    assert [u["filename"] for u in found[0]["unnamed"]] == ["b.md"]
    said = objection(found[0])
    assert "C-2/02" in said and "Gamma Northmoor ruling" in said and "4" in said


def test_the_objection_names_what_to_carry_not_how_to_write_it():
    """The wording is the drafter's; the field's conventions are the
    deployment's playbook. The engine says which source is missing."""
    said = objection({"sequence": 2, "unnamed": [
        {"filename": "a.md", "identifier": "T-1/01", "name": "Alpha Kestrel ruling"}]})
    assert "T-1/01" in said
    lowered = said.lower()
    for word in ("judgment", "court", "case law", "tribunal"):
        assert word not in lowered
