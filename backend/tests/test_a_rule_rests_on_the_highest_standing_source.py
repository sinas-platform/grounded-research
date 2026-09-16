"""A general proposition rests on the highest-standing source retrieved.

An expert review of published answers found claims stating a general
proposition resting on a source of a class the deployment had labelled as
describing rather than laying down, while a source of a class standing higher
— saying the same thing — sat uncited in the same retrieved set. The label was
already printed after the sentence. Labelling made the violation visible and
did nothing else about it.

What these pin, in the order a run meets them:

- STANDING COMES FROM THE DEPLOYMENT. A class declares a rank on itself; the
  engine compares two integers and holds no class name and no term of art. The
  fixture deployment below is a bicycle-repair corpus, and every rule works,
  which is the whole claim.
- THE RULE IS RELATIVE. A claim resting on the lowest-ranked class in the
  corpus is correct when nothing better came back. The comparison is against
  the documents RETRIEVED FOR THIS ANSWER, not against what exists.
- AN UNRANKED CLASS IS INERT. It neither satisfies the rule nor breaches it,
  and it is never named as the source a claim should have used.
- ONLY A GENERAL PROPOSITION IS JUDGED. A claim reporting what a source says,
  what happened or how a process ran is untouched.
- THE DOCUMENTS ARE READ BEFORE THE OBJECTION IS PUT. The drafter sees
  passages extracted for its own planned claims, so it may never have been
  shown the higher-standing source at all; a refusal from a drafter with
  nothing to check against settles a point that was never argued.
- A REFUSAL WITH A REASON IS AN ANSWER AND NOT A FAILURE. It is recorded, it
  is ruled on, and it never contests the run: there is no hard fail here.

No database and no model: the ledger's storage is an in-memory dict, the read
of a higher-standing document is a stub, and the logic under test is the real
one.

Run from the backend directory:
`python -m pytest tests/test_a_rule_rests_on_the_highest_standing_source.py`
"""

from __future__ import annotations

import uuid

import pytest
from app.services import objections, standing
from app.services import package as package_service
from app.services import query_runner as qr
from app.services.reread import Cited, standing_prompt

RUN = uuid.uuid4()
ANSWER = uuid.uuid4()

# ── the declaration, as a package writes it ──────────────────────────────────

#: A corpus that is not anybody's law. Two classes stand equally at the top,
#: one stands below them, and one declares nothing at all — which is every
#: case the rule has to handle, in four classes.
_PACKAGE = """
apiVersion: sgr.sinas.co/v1
kind: SgrPackage
metadata:
  name: demo
package:
  name: demo
  version: "0.1.0"
spec:
  document_classes:
    - name: Workshop Manual
      standing: {top}
    - name: Parts Specification
      standing: 1
    - name: Repair Column
      standing: 4
      authority_label: column
    - name: Loose Note
"""


def _validate(*, top: str = "1"):
    return package_service.validate(_PACKAGE.format(top=top))


def test_a_package_declaring_how_high_each_class_stands_validates():
    """The rank is the deployment's statement about its own kinds of source.
    The engine never writes one down."""
    result = _validate()
    assert result.valid, result.errors


def test_two_classes_may_stand_equally():
    """A rank rather than a list in order, and this is why: an ordering would
    have to break a tie that is not there."""
    assert _validate(top="1").valid


def test_a_class_may_decline_to_be_ranked():
    """`Loose Note` declares nothing. Absence of a declaration is absence of
    an opinion — never a default position at the bottom of the order."""
    result = _validate()
    assert result.valid, result.errors


def test_a_rank_below_one_is_refused_where_it_is_written():
    """1 is the top. A zero or a negative is a deployment meaning something
    the engine has no reading for, and guessing at it here is the defect this
    whole mechanism exists to remove."""
    assert not _validate(top="0").valid


# ── the rule, over two integers ──────────────────────────────────────────────

#: The retrieved set of one answer: what came back, against the rank each
#: document's class declared. `a-note.md` is of the unranked class.
RETRIEVED = {
    "a-manual.md": 1,
    "a-spec.md": 1,
    "a-column.md": 4,
    "a-note.md": None,
}


def _claim(kind: str = "rule", cites: tuple[str, ...] = ("a-column.md",),
           seq: int = 7, cid: str = "c-1") -> dict:
    return {"id": cid, "sequence": seq, "kind": kind,
            "text": "a bearing is replaced whenever its play exceeds the "
                    "service limit", "cites": list(cites)}


def test_a_rule_resting_low_while_a_higher_source_was_retrieved_is_a_gap():
    """The finding the review made, reduced to arithmetic."""
    gaps = standing.gaps([_claim()], RETRIEVED)
    assert len(gaps) == 1
    assert gaps[0].cited_standing == 4
    assert gaps[0].better_standing == 1
    # Best-standing first, and the document the claim already cites is never
    # named back to it.
    assert gaps[0].better == ("a-manual.md", "a-spec.md")
    assert "a-column.md" not in gaps[0].better


def test_a_rule_citing_the_highest_source_available_raises_nothing():
    assert standing.gaps([_claim(cites=("a-manual.md",))], RETRIEVED) == []


def test_the_best_source_a_claim_cites_is_what_counts():
    """A claim resting on the higher source is not in breach because it also
    cites a lower one beside it. Citing more is not citing worse."""
    assert standing.gaps(
        [_claim(cites=("a-column.md", "a-manual.md"))], RETRIEVED) == []


def test_the_comparison_is_against_what_was_retrieved_and_nothing_else():
    """The rule is relative. With only the lower-standing class in the
    retrieved set the claim is correct, and the answer says so by raising
    nothing — no note, no caveat, no cycle spent."""
    assert standing.gaps([_claim()], {"a-column.md": 4}) == []


def test_an_unranked_source_neither_satisfies_the_rule_nor_breaches_it():
    """Both halves, because only having one is how a default creeps in. A
    claim resting on the unranked class raises nothing even with ranked
    documents beside it, and the unranked document is never named as the
    source some other claim should have used."""
    assert standing.gaps([_claim(cites=("a-note.md",))], RETRIEVED) == []
    gaps = standing.gaps([_claim()], RETRIEVED)
    assert "a-note.md" not in gaps[0].better


def test_a_deployment_that_ranks_nothing_switches_the_rule_off():
    assert standing.gaps([_claim()], {"a-column.md": None,
                                      "a-manual.md": None}) == []


@pytest.mark.parametrize(
    "kind", ["application", "fact", "procedure", "conclusion", "abstention",
             "label", "inference", "test"])
def test_a_claim_that_is_not_a_general_proposition_is_unaffected(kind):
    """A claim reporting what a source SAYS is not weakened by where that
    source stands; a conclusion rests on the claims it follows from. Only
    `rule` — "a general proposition the source lays down" — is judged."""
    assert standing.gaps([_claim(kind=kind)], RETRIEVED) == []
    assert standing.RULE_KINDS == ("rule",)


def test_a_claim_resting_on_no_passage_at_all_raises_nothing():
    """A derived claim cites nothing, and nothing has no standing."""
    assert standing.gaps([_claim(cites=())], RETRIEVED) == []


def test_the_engine_compares_ranks_and_never_a_class_name():
    """Rename every class in the corpus and the finding is identical. That is
    the property that makes this deployable anywhere; a rule keyed on what a
    class is CALLED works for one collection and is silent for every other."""
    renamed = {"ein-handbuch.md": 1, "eine-kolumne.md": 4}
    gaps = standing.gaps(
        [_claim(cites=("eine-kolumne.md",))], renamed)
    assert [g.better for g in gaps] == [("ein-handbuch.md",)]


# ── what is put to the drafter ───────────────────────────────────────────────

FOUND = {"filename": "a-manual.md", "line_from": 112, "line_to": 115,
         "quote": "replace the bearing once play exceeds the service limit"}


def test_the_ask_names_the_gap_and_the_passage_when_one_was_found():
    gap = standing.gaps([_claim()], RETRIEVED)[0]
    asked = standing.objection(gap, FOUND)
    assert "a-column.md" in asked and "a-manual.md" in asked
    assert "standing 4" in asked and "higher at 1" in asked
    assert "lines 112-115" in asked
    assert "replace the bearing" in asked


def test_the_ask_says_the_documents_were_read_and_held_nothing():
    """The half that makes a refusal answerable. One pass over whole
    documents is not proof and the wording does not claim it is — but a
    drafter told what was looked for can refuse on the record."""
    gap = standing.gaps([_claim()], RETRIEVED)[0]
    asked = standing.objection(gap, None)
    assert "read in full" in asked
    assert "no passage stating it came back" in asked


def test_the_ask_fits_what_the_ledger_stores():
    """The ledger truncates an ask at 400 characters and the drafter is shown
    what it stored. An ask that overran would reach the drafter as a sentence
    cut in half, which is how a request becomes unanswerable."""
    long_gap = standing.Gap(
        claim_id="c-1", sequence=7, text="x" * 900,
        cited=("a-column.md",) * 3, cited_standing=4,
        better=("a-manual.md",) * 3, better_standing=1)
    for found in (None, {**FOUND, "quote": "y" * 900}):
        assert len(standing.objection(long_gap, found)) <= standing.ASK_CHARS


def test_the_gap_reaches_the_extractor_as_a_point_naming_the_document():
    """Revision may cite only passages it is shown, so a request naming a
    document without opening it can be obeyed by doing nothing. The marker is
    the one the extraction planner already reads."""
    gap = standing.gaps([_claim()], RETRIEVED)[0]
    assert "[obligated document: a-manual.md]" in standing.point(gap)


def test_the_subject_is_the_claim_and_never_a_filename():
    """Two properties, and only the claim's row id has both. A sequence is a
    position in a list the run is still editing, so a drop renumbers it and a
    settled point returns wearing a new name. And a filename would collide
    with the source obligations, whose subjects ARE filenames and whose
    settled set is one flat set of strings across every kind."""
    gap = standing.gaps([_claim()], RETRIEVED)[0]
    assert gap.subject == "claim c-1"
    assert not gap.subject.endswith(".md")


def test_an_argument_the_answer_ended_is_closed_and_one_still_running_is_not():
    gaps = standing.gaps([_claim()], RETRIEVED)
    assert standing.closed({"claim c-1", "claim c-9"}, gaps) == ["claim c-9"]


def test_the_three_counts_come_off_the_ledger():
    """Raised, resolved, refused — and the third is the one that was never
    observable. Across three live runs the drafter refused nothing, because
    evidence findings are usually correct and there was nothing to argue."""
    ledger = [
        {"kind": "standing", "state": "open", "reason": ""},
        {"kind": "standing", "state": "resolved", "reason": ""},
        {"kind": "standing", "state": "accepted", "reason": "nothing in the "
                                                            "manual says it"},
        {"kind": "source", "state": "resolved", "reason": ""},
    ]
    assert standing.summary(ledger) == {"raised": 3, "resolved": 1,
                                        "refused": 1}


def test_the_read_asks_one_document_about_one_proposition_whole():
    """The same read the uncovered-part re-read makes, pointed at a document
    the answer did not cite. Whole is the point: reading a window is what
    produced the defect, in both directions."""
    src = Cited(filename="a-manual.md", text="z" * 400)
    prompt = standing_prompt("a bearing is replaced when play exceeds it", src)
    assert "a bearing is replaced when play exceeds it" in prompt
    assert src.text in prompt and src.filename in prompt
    assert "Quote verbatim or answer none" in prompt


# ── the argument, through the ledger ─────────────────────────────────────────

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


def _rows() -> list[dict]:
    """The retrieved set as `_manifest_rows` hands it on."""
    return [{"filename": fn, "class": "K", "class_standing": s,
             "title": fn, "props": {}, "annotation_values": {}}
            for fn, s in RETRIEVED.items()]


@pytest.fixture
def looked(monkeypatch):
    """The read of the higher-standing documents, stubbed, recording when it
    ran so the ORDER can be asserted — which is the whole design."""
    calls: list[str] = []
    hit: dict = {"value": None}

    async def _look_higher(_sinas, gap, counts):
        calls.append(gap.subject)
        counts["looked"] += 1
        if hit["value"]:
            counts["found"] += 1
        return hit["value"]

    monkeypatch.setattr(qr, "_look_higher", _look_higher)
    return type("L", (), {"calls": calls, "hit": hit})()


async def _gate(claims: list[dict] | None = None):
    return await qr._standing_objections(
        None, RUN, ANSWER, _rows(), claims or [_claim()], cycle_no=1)


@pytest.mark.asyncio
async def test_the_higher_standing_documents_are_read_before_the_objection(
        ledger, looked):
    """Order, not merely presence. The drafter only ever sees passages
    extracted for its own planned claims, so an objection put before the read
    is one the drafter has nothing to check against — and a refusal made in
    ignorance settles a point that was never argued."""
    looked.hit["value"] = FOUND
    assert ledger == {}
    issues, points, counts = await _gate()
    assert looked.calls == ["claim c-1"]
    assert counts == {"gaps": 1, "raised": 1, "looked": 1, "found": 1,
                      "resolved": 0}
    entry = next(iter(ledger.values()))
    assert entry["kind"] == standing.KIND
    assert "lines 112-115" in entry["asked"]
    assert entry["state"] == objections.OPEN
    # The passage found is a passage the reviser must be shown, or the
    # request can be obeyed by doing nothing.
    assert points == [standing.point(standing.gaps([_claim()], RETRIEVED)[0])]
    assert len(issues) == 1


@pytest.mark.asyncio
async def test_the_request_tells_the_drafter_how_to_refuse_it(ledger, looked):
    """The two-way argument has never once been used in a live run. It is
    reachable only if the move is spelled out where the drafter is looking,
    with the id a reply names."""
    looked.hit["value"] = None
    issues, points, counts = await _gate()
    oid = next(iter(ledger))
    assert f'"objection": "{oid}"' in issues[0]
    assert "REFUSE" in issues[0]
    # Nothing was found, so there is no passage to extract and no point.
    assert points == []
    assert counts["found"] == 0
    # And the refusal commits the drafter to saying so in the claim itself —
    # in its own words. The engine writes no sentence for it.
    assert "its own sentence" in issues[0]
    assert "your words" in issues[0]


@pytest.mark.asyncio
async def test_a_refusal_with_a_reason_is_recorded_and_does_not_contest_the_run(
        ledger, looked):
    """There is no hard fail. A reasoned refusal is the correct outcome when
    nothing higher carries the point, and it leaves the run's verdict alone:
    only a justified `essential` request pressed to a standstill may contest
    one, and a standing request is always supporting."""
    looked.hit["value"] = None
    await _gate()
    oid = next(iter(ledger))
    await objections.refused(
        RUN, oid, "the manual describes the tool and states no rule about "
                  "when a bearing is replaced", cycle=1)
    record = next(e for e in await objections.record(RUN) if e["id"] == oid)
    assert record["state"] == objections.ANSWERED
    assert "states no rule" in record["reason"]
    assert record["importance"] == objections.SUPPORTING
    assert await objections.contested(RUN) == []
    note = next(n for n in await objections.notes(RUN) if n["id"] == oid)
    assert note["caveat"] is False
    assert standing.summary(await objections.record(RUN))["refused"] == 1


@pytest.mark.asyncio
async def test_the_documents_are_not_read_again_for_an_argument_already_open(
        ledger, looked):
    """Every look is a whole document through a model. Re-reading the same
    documents each cycle to reach the same answer, for a drafter that has
    been shown nothing new, is the cost that gets a gate switched off."""
    looked.hit["value"] = FOUND
    await _gate()
    await _gate()
    assert looked.calls == ["claim c-1"]
    assert len(ledger) == 1


@pytest.mark.asyncio
async def test_a_claim_that_stopped_resting_low_ends_its_own_argument(
        ledger, looked):
    """Settled by the answer rather than by the argument. The gaps are
    recomputed every cycle from the claims as they now stand, so a claim
    re-cited, revised into another kind or dropped stops appearing — and is
    not asked about a sentence it no longer makes."""
    looked.hit["value"] = FOUND
    await _gate()
    _issues, _points, counts = await _gate(
        [_claim(cites=("a-column.md", "a-manual.md"))])
    assert counts["resolved"] == 1
    assert next(iter(ledger.values()))["state"] == objections.RESOLVED
    assert standing.summary(await objections.record(RUN))["resolved"] == 1


@pytest.mark.asyncio
async def test_a_point_the_drafter_won_is_never_reopened(ledger, looked):
    """A refusal the review accepted is settled by the ruling. Marking it
    resolved when the claim later changes would rewrite a point the drafter
    won as a point the answer fixed — which is the count this rule exists to
    measure."""
    looked.hit["value"] = None
    await _gate()
    oid = next(iter(ledger))
    await objections.refused(RUN, oid, "nothing higher states this at all, "
                                       "having read both", cycle=1)
    await objections.rule(RUN, oid, "accept", cycle=2)
    assert ledger[oid]["state"] == objections.ACCEPTED
    issues, _points, counts = await _gate()
    assert issues == [] and counts["raised"] == 0
    assert ledger[oid]["state"] == objections.ACCEPTED
    # And it is not read again either: a settled point costs nothing.
    assert looked.calls == ["claim c-1"]


@pytest.mark.asyncio
async def test_an_unranked_corpus_costs_nothing_and_says_nothing(
        ledger, looked):
    """Inert means inert: no read, no request, no ledger entry. A deployment
    that has not ranked its classes gets the engine it had before this."""
    rows = [{"filename": fn, "class": "K", "class_standing": None,
             "title": fn, "props": {}, "annotation_values": {}}
            for fn in RETRIEVED]
    issues, points, counts = await qr._standing_objections(
        None, RUN, ANSWER, rows, [_claim()], cycle_no=1)
    assert (issues, points) == ([], [])
    assert counts == {"gaps": 0, "raised": 0, "looked": 0, "found": 0,
                      "resolved": 0}
    assert looked.calls == [] and ledger == {}
