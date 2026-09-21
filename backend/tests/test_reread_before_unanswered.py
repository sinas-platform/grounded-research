"""Every source the answer cites is re-read in full before a part is called
unanswered.

A reviewer asked for four specific things and the answer addressed one. All
four were in documents the answer already cited. Extraction reads per planned
claim from that claim's anchors, not whole documents, so a document opened for
one passage can hold the material for a part in another passage that nobody
read. The gate then judges coverage on the claims, sees nothing about the
part, and says so, correctly, about an answer whose own sources contain it.

So this is a re-read and not a retrieval. The documents are identified, their
text is stored, and the condition that fires it is already computed: the gate
knows which parts it just marked uncovered. What is missing is opening those
documents again, whole, before that verdict leaves the building.

Cheap by construction. Nothing runs while every part is covered, which is most
answers, and the work is bounded by the documents the answer already cites.
"""

from __future__ import annotations

import pytest

from app.services.reread import (
    Cited,
    needs_reread,
    reread_prompt,
    apply_reread,
)

PARTS = [
    {"asks": "whether a copy may be taken", "covered": True, "gap": ""},
    {"asks": "whether it must be returned", "covered": False,
     "gap": "no claim addresses return"},
]
CITED = [Cited(filename="a-judgment.md", text="x" * 400),
         Cited(filename="an-opinion.md", text="y" * 400)]


def test_nothing_runs_when_every_part_is_covered():
    """The common case pays nothing. A re-read that fired on every answer
    would double the cost of the runs that are already right."""
    assert needs_reread([{"asks": "q", "covered": True, "gap": ""}], CITED) == []


def test_nothing_runs_when_the_answer_cites_nothing():
    """There is nothing to re-read. An answer with no citations has a
    different problem and this is not it."""
    assert needs_reread(PARTS, []) == []


def test_an_uncovered_part_with_cited_sources_is_re_read():
    got = needs_reread(PARTS, CITED)
    assert [p["asks"] for p in got] == ["whether it must be returned"]


def test_a_part_with_no_text_to_read_is_not_re_read():
    """A cited document whose text could not be loaded is not evidence that
    the part is absent from it. Saying nothing is the honest outcome."""
    assert needs_reread(PARTS, [Cited(filename="a.md", text="")]) == []


def test_the_prompt_carries_the_part_and_the_whole_document():
    """Whole, deliberately. Reading a window is what produced the defect:
    the material was in the document and outside the window."""
    p = reread_prompt(PARTS[1], CITED[0])
    assert "whether it must be returned" in p
    assert CITED[0].text in p
    assert CITED[0].filename in p


def test_a_found_passage_is_carried_to_revision_not_spent_on_the_verdict():
    """A passage existing is not the answer addressing the part.

    This test used to assert the opposite — that a found passage flipped the
    part to covered — and that was the defect. No claim has been written when
    this runs, and the prose a reader receives is unchanged; marking the part
    covered took it off the gate's uncovered list and let the run publish an
    answer still silent on it. "Looks answered, is not" is the failure every
    other check here exists to stop, and this was quietly manufacturing it.

    So the finding is carried rather than spent: the part keeps its gap, gains
    the passage, and revision is given something verbatim to write from. It
    becomes covered when a claim covers it.
    """
    parts, found = apply_reread(
        PARTS, {1: {"filename": "a-judgment.md", "line_from": 80,
                    "line_to": 84, "quote": "the copy is returned"}})
    assert parts[1]["covered"] is False, "a passage is not a claim"
    assert parts[1]["gap"], "the part must still say what is missing"
    assert "write the claim" in parts[1]["gap"]
    assert parts[1]["reread_from"]["filename"] == "a-judgment.md"
    assert parts[1]["reread_from"]["line_from"] == 80
    assert parts[1]["reread_from"]["quote"] == "the copy is returned"
    assert found == 1, "the read still counts as a find, for the telemetry"


def test_a_part_nothing_answers_stays_uncovered_and_records_the_attempt():
    """The other half of being worth trusting: a re-read that found nothing
    has to be distinguishable from a re-read that never ran, or the next
    reader cannot tell an absent limb from an unchecked one."""
    parts, found = apply_reread(PARTS, {1: None})
    assert parts[1]["covered"] is False
    assert parts[1]["reread"] == "no passage in any cited source addresses it"
    assert found == 0


def test_a_covered_part_is_never_touched():
    parts, _ = apply_reread(PARTS, {1: None})
    assert parts[0] == PARTS[0]


def test_the_verdict_may_not_invent_a_document_the_answer_does_not_cite():
    """A re-read that reaches outside the citations is a retrieval, and a
    retrieval at this point would let the gate answer the question itself."""
    with pytest.raises(ValueError, match="not cited"):
        apply_reread(PARTS, {1: {"filename": "elsewhere.md", "line_from": 1,
                                 "line_to": 2, "quote": "x"}},
                     cited={"a-judgment.md", "an-opinion.md"})


@pytest.mark.asyncio
async def test_the_re_read_asks_the_document_reader_not_the_judge():
    """A whole-document look goes to the extraction tier.

    This step looks for a passage; it does not judge one, and whatever comes
    back is checked against the document and dropped unless the quote is
    verbatim. It used to reach for the gate agent because the gate's reply
    shape suited it, and the whole document rides in the prompt — measured
    over one night, 19 of these calls averaged 173,000 tokens and cost $62,
    against $35 for the 75 calls that actually asked the gate to judge
    something. The collection holds documents up to 5.5MB.

    Pinned because the cost of this mistake is invisible in any test that
    only checks the answer: the wrong tier gives the same verdict.
    """
    from app.services.query_runner import _ask_document

    asked: list[str] = []

    class _Sinas:
        async def invoke(self, agent: str, prompt: str) -> str:
            asked.append(agent)
            return '{"found": false, "line_from": 0, "line_to": 0, "quote": ""}'

    await _ask_document(_Sinas(), reread_prompt(PARTS[1], CITED[0]),
                        CITED[0].filename, CITED[0].text)
    assert asked == ["sgr/passage-extractor-agent"]
