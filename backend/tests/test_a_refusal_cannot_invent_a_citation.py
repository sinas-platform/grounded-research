"""A refusal closes an objection, so a refusal that asserts a fact is checked.

The review asks for a source, the drafter may refuse with a reason, and the
next cycle rules on it. Nothing read the reason. Most reasons are judgements
about a source and the engine has no standing to rule on those, but one kind
asserts something the engine can settle by looking: that the document is
already cited.

Measured on one run: an objection named a retrieved and uncited source; the
drafter refused, saying a named claim already cited it for both conditions of
a test; no evidence row existed against that document anywhere in the answer.
The objection closed, the document was never opened, and a condition it
states never reached the answer.

What these pin:

- a reason naming no document is not judged, because judging it would need a
  reading rather than a lookup;
- a reason naming a document the answer does not cite fails, and the
  objection goes back to the drafter rather than into a block;
- the rejection costs an exchange, so a drafter cannot hold a point open by
  asserting the same absent citation every round;
- at the bound the objection stalls, where the existing escalation takes it;
- the drafter is shown what was rejected, so the second exchange is not spent
  repeating the first;
- the match is on filenames the engine issued, never on words, so a corpus in
  one language reads like a corpus in another.

No database and no model: the ledger's storage is an in-memory dict.

Run from the backend directory:
`python -m pytest tests/test_a_refusal_cannot_invent_a_citation.py`
"""

import uuid

import pytest
from app.services import objections
from app.services import query_runner as qr
from app.services.drafting_chat import objections_block

RUN = uuid.uuid4()

#: The shape of the measured refusal, with this repository's own names. It
#: names a document and offers that as the reason the request is met.
ASSERTS_A_CITATION = ("Claim 5 already cites a-judgment.md for both conditions "
                      "of the test, so the founding source is cited for the "
                      "point it carries.")
#: The refusal the gate actually asked for on that objection, which names
#: nothing and is not the engine's to rule on.
ARGUES_ABOUT_THE_SOURCE = ("The document was opened for this point and no "
                           "passage stating it came back.")

SUBJECTS = {"obj-1": "a-judgment.md", "obj-2": "b-judgment.md"}


@pytest.fixture
def ledger(monkeypatch):
    """The ledger's real logic over a dict instead of a run row."""
    store: dict = {}

    async def _load(_run_id):
        return dict(store)

    async def _store(_run_id, entries):
        store.clear()
        store.update(entries)

    monkeypatch.setattr(objections, "_load", _load)
    monkeypatch.setattr(objections, "_store", _store)
    return store


# -- what the engine may rule on ----------------------------------------------

def test_a_reason_naming_no_document_is_not_judged():
    """The ordinary refusal. Whether a source can carry a point is an argument
    about the source, and the engine cannot read one; returning None keeps it
    out of the check rather than guessing at it."""
    assert qr.named_citations_hold(
        ARGUES_ABOUT_THE_SOURCE, SUBJECTS, set()) is None


def test_a_reason_naming_an_uncited_document_fails():
    assert qr.named_citations_hold(
        ASSERTS_A_CITATION, SUBJECTS, {"something-else.md"}) is False


def test_a_reason_naming_a_document_the_answer_does_cite_holds():
    """Naming a document is not itself the defect. A refusal may point at one
    the answer cites, to argue by contrast or to say the point is already
    covered, and that assertion is true."""
    assert qr.named_citations_hold(
        ASSERTS_A_CITATION, SUBJECTS, {"a-judgment.md"}) is True


def test_every_document_named_has_to_hold():
    """One true citation does not carry a second that is absent."""
    reason = "a-judgment.md and b-judgment.md both already cover this point."
    assert qr.named_citations_hold(reason, SUBJECTS, {"a-judgment.md"}) is False


def test_the_match_is_on_filenames_and_never_on_words():
    """The filenames come from the ledger, so this is a token the engine
    issued. Nothing here is English, and a reason in another language behaves
    the same way."""
    reason = "La demande est satisfaite : a-judgment.md est deja cite."
    assert qr.named_citations_hold(reason, SUBJECTS, set()) is False
    assert qr.named_citations_hold(
        "Le document a ete ouvert et aucun passage ne l'affirme.",
        SUBJECTS, set()) is None


# -- where the objection goes -------------------------------------------------

def test_the_first_rejection_reopens_and_the_bound_stalls():
    """Back to the drafter once, then stopped. Decided on the count before the
    rejection spends one, as the review's own ruling is."""
    assert objections.state_after_rejection({"exchanges": 1}) == objections.OPEN
    assert (objections.state_after_rejection({"exchanges": 2})
            == objections.STALLED)


@pytest.mark.asyncio
async def test_a_rejected_refusal_leaves_the_objection_with_the_drafter(ledger):
    oid = await objections.raise_objection(
        RUN, kind=objections.SOURCE, subject="a-judgment.md",
        asked="cite it for the rule", cycle=1)
    await objections.refused(RUN, oid, ASSERTS_A_CITATION, cycle=1,
                             citation_holds=False)
    entry = ledger[oid]
    assert entry["state"] == objections.OPEN
    assert entry["exchanges"] == 2
    # Not recorded as an answer: nothing for the next cycle to rule on, which
    # is what closed it on the measured run.
    assert entry["reply"] is None
    assert entry["rejected"][0]["reason"] == ASSERTS_A_CITATION
    assert "a-judgment.md" in entry["rejected"][0]["why"]


@pytest.mark.asyncio
async def test_asserting_it_twice_stalls_rather_than_looping(ledger):
    """A drafter that could reopen for free would hold a point open forever by
    repeating the same absent citation, which is the failure the exchange
    bound stops on the review's side."""
    oid = await objections.raise_objection(
        RUN, kind=objections.SOURCE, subject="a-judgment.md",
        asked="cite it for the rule", cycle=1)
    for _ in (1, 2):
        await objections.refused(RUN, oid, ASSERTS_A_CITATION, cycle=1,
                                 citation_holds=False)
    entry = ledger[oid]
    assert entry["state"] == objections.STALLED
    assert len(entry["rejected"]) == 2


@pytest.mark.asyncio
async def test_a_refusal_the_engine_cannot_judge_is_recorded_as_before(ledger):
    oid = await objections.raise_objection(
        RUN, kind=objections.SOURCE, subject="a-judgment.md",
        asked="cite it for the rule", cycle=1)
    await objections.refused(RUN, oid, ARGUES_ABOUT_THE_SOURCE, cycle=1,
                             citation_holds=None)
    assert ledger[oid]["state"] == objections.ANSWERED
    assert ledger[oid]["reply"]["reason"] == ARGUES_ABOUT_THE_SOURCE
    assert not ledger[oid].get("rejected")


# -- what the two readers see -------------------------------------------------

@pytest.mark.asyncio
async def test_the_drafter_is_shown_what_was_rejected(ledger):
    """Otherwise the second exchange is spent making the first one's mistake
    again, and the objection stalls having never been answered."""
    oid = await objections.raise_objection(
        RUN, kind=objections.SOURCE, subject="a-judgment.md",
        asked="cite it for the rule", cycle=1)
    await objections.refused(RUN, oid, ASSERTS_A_CITATION, cycle=1,
                             citation_holds=False)
    block = objections_block(await objections.open_points(RUN))
    assert "was not accepted" in block
    assert "a-judgment.md" in block


@pytest.mark.asyncio
async def test_the_ledger_shows_an_argument_withdrawn_not_won(ledger):
    oid = await objections.raise_objection(
        RUN, kind=objections.SOURCE, subject="a-judgment.md",
        asked="cite it for the rule", cycle=1)
    await objections.refused(RUN, oid, ASSERTS_A_CITATION, cycle=1,
                             citation_holds=False)
    row = objections.as_record(ledger[oid])
    assert len(row["rejected"]) == 1
    assert row["reason"] == ""
