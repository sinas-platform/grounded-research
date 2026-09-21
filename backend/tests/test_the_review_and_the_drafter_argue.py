"""The completeness review and the drafter can now disagree, and stop.

The two used to talk in one direction: the review asked for something, the
drafter obeyed or changed nothing, and "changed nothing" was indistinguishable
from not having read the request. A drafter that had a reason wrote it into a
dropped claim, where nothing read it, and the review asked for the same source
again. On the measured run that produced this work the loop ran five gate
cycles and nine revision rounds and ended `partial` — telling the reader the
question could not be answered, when every part of it had been.

What these pin, in the order a run meets them:

- a reply is a move: a refusal with a reason is answered, not ignored;
- the review must rule, and only one ruling keeps the argument alive —
  a restatement carrying something it has not said before. Silence is an
  acceptance, and so is repetition;
- the argument is bounded at two exchanges whatever either side still thinks;
- what settles is never raised again and its document is never fed again;
- `essential` costs a justification, or it is read as `supporting`;
- an unresolved `supporting` point is a ledger note and changes nothing; an
  unresolved `essential` one puts a reservation in the answer the reader sees
  and gives the run a status of its own;
- and a source left uncited never makes a run `partial`, because `partial`
  means a part could not be answered.

No database and no model: the ledger's storage is an in-memory dict and the
runner's session is a stub, so the logic under test is the real one.

Run from the backend directory:
`python -m pytest tests/test_the_review_and_the_drafter_argue.py`
"""

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app.services import objections
from app.services import query_runner as qr
from app.services.answer_render import render_markdown

RUN = uuid.uuid4()


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


async def _ask(**over):
    """The review asks for a source. Returns the id a reply names."""
    return await objections.raise_objection(
        RUN, kind="source", subject=over.pop("subject", "a.md"),
        asked=over.pop("asked", "cite this for the point it settles"),
        cycle=over.pop("cycle", 1), **over)


# ── a reply is a move ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_refusal_with_a_reason_is_an_answer_the_review_must_rule_on(ledger):
    oid = await _ask()
    await objections.refused(
        RUN, oid, "the passage reports an argument, not a finding, so it "
                  "cannot carry the point asked of it", cycle=1)
    outstanding = await objections.outstanding(RUN)
    assert [o["id"] for o in outstanding] == [oid]
    assert "reports an argument" in outstanding[0]["reply"]["reason"]


@pytest.mark.asyncio
async def test_a_refusal_with_no_reason_is_a_silence_and_leaves_it_open(ledger):
    """The reason is the whole of what makes this a reply rather than the
    non-answer it replaces. Without one there is nothing to rule on."""
    oid = await _ask()
    await objections.refused(RUN, oid, "no", cycle=1)
    assert await objections.outstanding(RUN) == []
    assert ledger[oid]["state"] == objections.OPEN


# ── what the review may do about it ──────────────────────────────────────────

async def _refused(reason="the passage reports an argument, not a finding, "
                          "so it cannot carry the point", **over):
    oid = await _ask(**over)
    await objections.refused(RUN, oid, reason, cycle=1)
    return oid


@pytest.mark.asyncio
async def test_an_accepted_refusal_is_never_raised_again(ledger):
    """Enforced where the asking happens, not asked of the prompt. A gate that
    forgets is the failure the ledger exists to stop."""
    oid = await _refused()
    await objections.rule(RUN, oid, "accept", cycle=2)
    assert ledger[oid]["state"] == objections.ACCEPTED
    assert await _ask(cycle=3) is None


@pytest.mark.asyncio
async def test_the_document_of_a_settled_point_is_never_fed_again(ledger):
    """The other half: the gate filters what it feeds by this set, so the
    passages of a settled source are not extracted or sent."""
    oid = await _refused()
    await objections.rule(RUN, oid, "accept", cycle=2)
    assert await objections.settled_subjects(RUN) == {"a.md"}


@pytest.mark.asyncio
async def test_a_restatement_with_nothing_new_is_an_acceptance(ledger):
    """Pressing a point costs a new document, new evidence or a narrower ask.
    A review that could win by repeating itself would never have to read the
    drafter's reason at all."""
    oid = await _refused(asked="cite this for the point it settles")
    await objections.rule(RUN, oid, "restate",
                          "cite this for the point it settles", cycle=2)
    assert ledger[oid]["state"] == objections.ACCEPTED
    assert ledger[oid]["rulings"][-1]["ruling"] == "accept"


@pytest.mark.asyncio
async def test_re_punctuating_the_same_words_is_still_nothing_new(ledger):
    oid = await _refused(asked="cite this for the point it settles")
    await objections.rule(RUN, oid, "restate",
                          "Cite this, for the point it settles!", cycle=2)
    assert ledger[oid]["state"] == objections.ACCEPTED


@pytest.mark.asyncio
async def test_padding_the_old_ask_with_filler_is_still_nothing_new(ledger):
    """Containment either way counts as the same point, so a restatement that
    is the original plus words does not buy an exchange."""
    oid = await _refused(asked="cite this for the point it settles")
    await objections.rule(
        RUN, oid, "restate",
        "please do cite this, for the point it settles, as I said", cycle=2)
    assert ledger[oid]["state"] == objections.ACCEPTED


@pytest.mark.asyncio
async def test_a_restatement_that_adds_something_buys_one_more_exchange(ledger):
    oid = await _refused(asked="cite this for the point it settles")
    await objections.rule(
        RUN, oid, "restate",
        "the later ruling at paragraph 40 says the opposite of the passage "
        "you relied on", cycle=2)
    assert ledger[oid]["state"] == objections.OPEN
    assert ledger[oid]["exchanges"] == 2
    # and it is askable again, which is what "one more exchange" means
    assert await _ask(cycle=3) == oid


@pytest.mark.asyncio
async def test_silence_on_a_listed_refusal_is_an_acceptance(ledger):
    """Reading silence as "still objecting" would let the review press every
    point forever by answering none of them — the one-way behaviour this
    replaces, wearing a ledger."""
    oid = await _refused()
    await objections.rule_all(RUN, [], cycle=2)
    assert ledger[oid]["state"] == objections.ACCEPTED


@pytest.mark.asyncio
async def test_two_exchanges_end_the_argument_however_good_the_next_thought(
        ledger):
    """Two readers who have each said their piece twice are not converging."""
    oid = await _refused(asked="cite this for the point it settles")
    await objections.rule(RUN, oid, "restate",
                          "the later ruling at paragraph 40 contradicts it",
                          cycle=2)
    await objections.refused(
        RUN, oid, "paragraph 40 is obiter and the holding is unchanged",
        cycle=2)
    await objections.rule(RUN, oid, "restate",
                          "a third source, the consolidated text, settles it",
                          cycle=3)
    assert ledger[oid]["state"] == objections.STALLED
    assert ledger[oid]["exchanges"] == objections.MAX_EXCHANGES
    # and stalled is settled: the run moves on rather than asking a fourth time
    assert await _ask(cycle=4) is None


@pytest.mark.asyncio
async def test_a_source_that_reached_the_answer_settles_its_own_argument(ledger):
    oid = await _refused()
    await objections.resolve(RUN, ["a.md"])
    assert ledger[oid]["state"] == objections.RESOLVED
    assert await _ask(cycle=3) is None


# ── what `essential` costs ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_review_cannot_mark_a_source_essential_without_saying_why(
        ledger):
    """An importance nobody had to justify is not a judgment, and this one can
    hold a reservation against a published answer."""
    oid = await _ask(importance="essential", why_essential="")
    assert ledger[oid]["importance"] == objections.SUPPORTING
    assert not objections.is_essential(ledger[oid])


@pytest.mark.asyncio
async def test_essential_with_a_line_that_earns_it_stands(ledger):
    oid = await _ask(importance="essential",
                     why_essential="the first part turns on whether the duty "
                                   "is stated in mandatory terms")
    assert objections.is_essential(ledger[oid])


def test_the_word_alone_never_promotes_a_request():
    assert objections.importance_of("essential", "") == ("supporting", "")
    assert objections.importance_of("ESSENTIAL", " because ")[0] == "essential"
    assert objections.importance_of("anything else", "why")[0] == "supporting"


def test_the_gate_reply_reads_a_part_number_only_inside_the_decomposition():
    """The parts the gate is shown are numbered from 1 and a claim's
    `part_index` counts from 0. A number outside the decomposition names no
    part and becomes one, because a reservation pinned to a part that is never
    rendered would not be printed at all."""
    data = {"unused_sources": [
        {"filename": "a.md", "point": "p", "part": 1},
        {"filename": "b.md", "point": "p", "part": 2},
        {"filename": "c.md", "point": "p", "part": 9},
        {"filename": "d.md", "point": "p", "part": 0},
        {"filename": "e.md", "point": "p", "part": True},
        {"filename": "f.md", "point": "p", "part": None},
    ]}
    assert [s["part"] for s in qr._unused_sources(data, 2)] == [
        0, 1, None, None, None, None]


def test_with_no_decomposition_no_named_source_claims_a_part():
    """The gate derives its own split in that case and its numbering refers to
    nothing the answer renders."""
    data = {"unused_sources": [{"filename": "a.md", "point": "p", "part": 1}]}
    assert qr._unused_sources(data, 0)[0]["part"] is None


def test_the_old_string_shape_is_read_as_supporting():
    """A verdict written before importance existed named no essential source,
    and promoting one would put a request on the run's verdict that no gate
    ever marked."""
    out = qr._unused_sources({"unused_sources": ["a.md: it settles the point"]}, 2)
    assert out == [{"filename": "a.md", "point": "it settles the point",
                    "importance": "supporting", "essential_because": "",
                    "part": None}]


# ── what an unresolved point does to the answer ──────────────────────────────

async def _stalled(importance, why, part=0):
    oid = await _ask(importance=importance, why_essential=why, part=part)
    await objections.refused(
        RUN, oid, "the passage reports an argument, not a finding, so it "
                  "cannot carry the point", cycle=1)
    await objections.rule(RUN, oid, "restate",
                          "the later ruling at paragraph 40 contradicts it",
                          cycle=2)
    await objections.refused(
        RUN, oid, "paragraph 40 is obiter and the holding is unchanged",
        cycle=2)
    await objections.rule(RUN, oid, "restate",
                          "the consolidated text settles it either way",
                          cycle=3)
    return oid


@pytest.mark.asyncio
async def test_a_stalled_essential_point_becomes_a_caveat_the_reader_sees(ledger):
    await _stalled("essential", "the first part is not answered without it")
    notes = await objections.notes(RUN)
    assert [n["caveat"] for n in notes] == [True]
    assert notes[0]["source"] == "a.md" and notes[0]["part"] == 0
    assert await objections.contested(RUN)


@pytest.mark.asyncio
async def test_a_stalled_supporting_point_is_a_ledger_note_and_nothing_else(
        ledger):
    """A warning on an answer nobody thinks is incomplete teaches a reader to
    ignore the warnings that mean something."""
    await _stalled("supporting", "")
    notes = await objections.notes(RUN)
    assert [n["caveat"] for n in notes] == [False]
    assert await objections.contested(RUN) == []


@pytest.mark.asyncio
async def test_an_essential_point_the_drafter_talked_the_review_out_of_is_not_one(
        ledger):
    """Accepted is settled. The review read the reason and agreed; there is
    nothing left to warn anyone about."""
    oid = await _ask(importance="essential",
                     why_essential="the first part turns on it")
    await objections.refused(
        RUN, oid, "the passage reports an argument, not a finding", cycle=1)
    await objections.rule(RUN, oid, "accept", cycle=2)
    assert [n["caveat"] for n in await objections.notes(RUN)] == [False]
    assert await objections.contested(RUN) == []


@pytest.mark.asyncio
async def test_a_request_that_was_met_leaves_no_note_at_all(ledger):
    oid = await _ask(importance="essential", why_essential="the part turns on it")
    await objections.resolve(RUN, ["a.md"])
    assert await objections.notes(RUN) == []
    assert [r["id"] for r in await objections.record(RUN)] == [oid]


# ── the ledger as a record ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_ledger_records_every_outcome_and_what_each_side_said(ledger):
    """Accepted, stalled and resolved all reach the record, with what was
    asked, how much it mattered, the drafter's reason, the review's ruling and
    the cycle each happened in."""
    accepted = await _ask(subject="accepted.md", asked="cite the accepted one")
    await objections.refused(
        RUN, accepted, "it reports an argument, not a finding", cycle=1)
    await objections.rule(RUN, accepted, "accept", cycle=2)

    resolved = await _ask(subject="resolved.md", asked="cite the resolved one")
    await objections.resolve(RUN, ["resolved.md"])

    stalled = await _stalled("essential", "the first part turns on it")

    by_id = {r["id"]: r for r in await objections.record(RUN)}
    assert set(by_id) == {accepted, resolved, stalled}

    a = by_id[accepted]
    assert a["state"] == objections.ACCEPTED
    assert a["asked"] == "cite the accepted one"
    assert a["importance"] == "supporting"
    assert a["reason"] == "it reports an argument, not a finding"
    assert a["ruling"] == "accept" and a["ruled_cycle"] == 2
    assert a["raised_cycle"] == 1 and a["answered_cycle"] == 1

    assert by_id[resolved]["state"] == objections.RESOLVED
    assert by_id[resolved]["ruling"] == ""

    s = by_id[stalled]
    assert s["state"] == objections.STALLED and s["importance"] == "essential"
    assert s["why_essential"] == "the first part turns on it"
    assert s["exchanges"] == objections.MAX_EXCHANGES
    assert [r["cycle"] for r in s["rulings"]] == [2, 3]


@pytest.mark.asyncio
async def test_a_storage_fault_degrades_the_loop_rather_than_failing_the_run(
        monkeypatch):
    """Bookkeeping must never fail the run it serves. Without a ledger the
    loop is the one-way behaviour this replaces, which is worse and not
    broken."""
    async def _boom(*_a, **_k):
        raise RuntimeError("no database")

    monkeypatch.setattr(objections, "_load", _boom)
    monkeypatch.setattr(objections, "_store", _boom)
    assert await objections.raise_objection(
        RUN, kind="source", subject="a.md", asked="cite it") is None
    assert await objections.outstanding(RUN) == []
    assert await objections.settled_subjects(RUN) == set()
    assert await objections.record(RUN) == []
    assert await objections.notes(RUN) == []


# ── what the run's verdict says ──────────────────────────────────────────────

def _run_telemetry(monkeypatch, validate: dict):
    @asynccontextmanager
    async def _session_local():
        class _S:
            async def get(self, _model, _ident):
                return SimpleNamespace(telemetry={"validate": validate})
        yield _S()

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)


@pytest.mark.asyncio
async def test_a_stalled_essential_point_gives_the_run_its_own_status(monkeypatch):
    """Not `partial`, which would say a part could not be answered — every
    part was. Not `published` either, which would hide the disagreement. A
    human is being asked to read the reservation and decide."""
    _run_telemetry(monkeypatch, {"contested": [{"id": "obj-1", "caveat": True}]})
    assert await qr._final_status(RUN) == "published_contested"


@pytest.mark.asyncio
async def test_a_run_with_nothing_left_contested_publishes(monkeypatch):
    _run_telemetry(monkeypatch, {"contested": []})
    assert await qr._final_status(RUN) == "published"


@pytest.mark.asyncio
async def test_a_run_from_before_the_loop_publishes(monkeypatch):
    """A missing key is a run that predates the argument, not a contested one."""
    _run_telemetry(monkeypatch, {})
    assert await qr._final_status(RUN) == "published"


# ── what the reader is shown ─────────────────────────────────────────────────

PARTS = [{"index": 0, "label": "Whether it applies", "text": "..."},
         {"index": 1, "label": "Whether it is mandatory", "text": "..."}]

CLAIMS = [
    {"id": "c1", "sequence": 1, "section": "conclusion", "part_index": None,
     "position": 1, "claim_kind": "conclusion",
     "claim_text": "It applies and is mandatory."},
    {"id": "c2", "sequence": 2, "section": "analysis", "part_index": 0,
     "position": 1, "claim_kind": "rule",
     "claim_text": "The duty reaches the respondent."},
    {"id": "c3", "sequence": 3, "section": "analysis", "part_index": 1,
     "position": 1, "claim_kind": "rule",
     "claim_text": "The duty is stated in mandatory terms."},
]

DOCS = {"d1": {"title": "A Decision", "class": "Court Decision",
               "properties": {"decision_date": "2021-05-04"}}}
EVIDENCE = [{"claim_id": "c2", "document_id": "d1", "span": {}},
            {"claim_id": "c3", "document_id": "d1", "span": {}}]


def _note(**over):
    return {"id": "obj-1", "kind": "source", "state": "stalled",
            "source": "a.md", "source_citation": "A Second Decision, 4 May 2021",
            "part": 1, "importance": "essential",
            "why_essential": "whether the duty is mandatory turns on it",
            "asked": "cite it", "reason": "the passage reports an argument, "
                                          "not a finding",
            "exchanges": 2, "caveat": True, **over}


def _md(notes, claims=None):
    return render_markdown(
        {"question": "Does it apply, and is it mandatory?",
         "question_parts": PARTS, "open_notes": notes},
        claims if claims is not None else CLAIMS, EVIDENCE, DOCS).markdown


def test_a_reservation_prints_under_the_part_it_bears_on():
    """Where a reader deciding whether to rely on this part will meet it,
    rather than in a footnote reached after deciding."""
    lines = _md([_note()]).splitlines()
    heading = lines.index("## Whether it is mandatory")
    reservation = next(i for i, ln in enumerate(lines)
                       if ln.startswith("> **Reservation.**"))
    assert reservation > heading
    assert lines.index("## Authorities") > reservation


def test_a_reservation_names_the_source_the_way_the_answer_names_one():
    """By what it is, never by a storage name — like every other reference in
    the rendered answer."""
    md = _md([_note()])
    assert "A Second Decision, 4 May 2021" in md
    assert "a.md" not in md
    assert "whether the duty is mandatory turns on it" in md
    assert "the passage reports an argument, not a finding" in md


def test_a_reservation_the_review_did_not_tie_to_a_part_sits_with_the_conclusion():
    lines = _md([_note(part=None)]).splitlines()
    reservation = next(i for i, ln in enumerate(lines)
                       if ln.startswith("> **Reservation.**"))
    assert lines.index("## Conclusion") < reservation
    assert reservation < lines.index("## Whether it applies")


def test_a_note_that_is_not_a_caveat_prints_nothing():
    """An accepted refusal says nothing about the answer, and a supporting
    source left unused is a record rather than a warning."""
    assert "Reservation" not in _md([_note(caveat=False, importance="supporting")])
    assert "Reservation" not in _md([])


def test_a_reservation_still_prints_when_the_answer_has_no_parts_to_hang_it_on():
    """An answer drafted before the structure existed renders as a flat list.
    The one thing a reservation must never do is go unprinted."""
    flat = [{"id": "c1", "sequence": 1, "claim_kind": "rule",
             "claim_text": "The duty reaches the respondent."}]
    md = _md([_note()], claims=flat)
    assert "> **Reservation.**" in md
    assert md.index("Reservation") < md.index("## Authorities")


def test_a_reservation_naming_a_part_the_answer_never_renders_still_prints():
    """The index is clamped where it is read, so this should be unreachable.
    "Should be" is not a guarantee, and an answer that swallowed the warning
    while recording that the reader had been warned is the worse failure."""
    md = _md([_note(part=7)])
    assert "> **Reservation.**" in md


def test_the_source_of_a_reservation_that_resolved_to_nothing_is_left_unnamed():
    """A note that vanished because a document row moved would hide the
    disagreement rather than the filename."""
    md = _md([_note(source_citation=None)])
    assert "a source in the working set" in md
    assert "a.md" not in md


# ── a keep is work, and is counted ───────────────────────────────────────────

class _Claim:
    def __init__(self, seq):
        self.id = uuid.uuid4()
        self.sequence = seq
        self.claim_text = f"Claim {seq}."
        self.rationale = None
        self.section = "analysis"
        self.part_index = 0
        self.claim_kind = "rule"


@pytest.fixture
def reviser(monkeypatch):
    """`_revise_answer` over a stub session, so a patch really is applied."""
    tele: dict = {}
    claims = [_Claim(1), _Claim(2)]
    by_id = {c.id: c for c in claims}
    reply = {"text": "{}"}

    class _Session:
        async def get(self, model, ident):
            name = getattr(model, "__name__", "")
            if name == "QueryRun":
                # `synthesis_chat_id` is the run row's pointer at the drafting
                # conversation. A revision is a turn in it, so the stub has to
                # carry one or `_revise_answer` has no chat to speak into.
                return SimpleNamespace(parent_result_id=None, telemetry=tele,
                                       synthesis_chat_id="chat-1")
            if name == "Answer":
                return SimpleNamespace(question_parts=PARTS)
            return by_id.get(ident)

        async def execute(self, *_a, **_k):
            return SimpleNamespace(
                all=lambda: [(c, None, None, None) for c in claims],
                scalar=lambda: 2,
                scalars=lambda: SimpleNamespace(all=lambda: []))

        async def commit(self):
            return None

        async def flush(self):
            return None

        def add(self, _row):
            return None

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    async def _tele(_run_id, stage, **detail):
        tele.setdefault(stage, {}).update(detail)

    class _Sinas:
        """The conversation's half of the client: a revision is one turn."""

        async def chat_create(self, _agent, _title):
            return "chat-1"

        async def chat_send(self, _chat_id, _content, _agent=""):
            return reply["text"]

    async def _none(*_a, **_k):
        return None

    async def _rows(*_a, **_k):
        return []

    async def _zero(*_a, **_k):
        return 0

    async def _playbook(*_a, **_k):
        return ""

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(qr, "_tele", _tele)
    monkeypatch.setattr(qr, "_manifest_rows", _rows)
    monkeypatch.setattr(qr, "_synthesis_playbook", _playbook)
    monkeypatch.setattr(qr, "_removal_record", _rows)
    monkeypatch.setattr(qr, "_record_refusals", _zero)
    monkeypatch.setattr(qr, "_cap_refusals_last_cycle", _zero)
    monkeypatch.setattr(qr, "_bind_spans", _none)

    async def run(patch: dict) -> int:
        reply["text"] = json.dumps(patch)
        return await qr._revise_answer(
            _Sinas(), RUN, uuid.uuid4(),
            ["Claim 1: the named source is more direct."])

    return SimpleNamespace(run=run, tele=tele, claims=claims,
                           cycle=lambda: tele["validate"]["revision_1"])


REASON = ("the cited source carries the deciding body's own reasoning on this "
          "point; the named one only restates it")


@pytest.mark.asyncio
async def test_a_keep_with_a_reason_counts_as_work_and_is_recorded(reviser):
    """It read 0 on every round of nine on the measured run. `keep` was
    parsed, prompted and applied, and absent from the condition deciding
    whether the patch did anything — so a reply whose only content was "this
    citation stands, and here is why" returned early and was discarded. The
    one disposition built for the drafter to answer back with was the one the
    early return threw away."""
    touched = await reviser.run({"keep": [{"seq": 1, "rationale": REASON}]})
    assert touched == 1
    assert reviser.cycle()["kept_with_reason"] == 1
    assert reviser.claims[0].rationale == REASON
    # and it is a cycle that did something, so the loop reaches the next gate
    assert not reviser.tele["validate"].get("revision_yielded_no_change")


@pytest.mark.asyncio
async def test_two_keeps_count_twice(reviser):
    await reviser.run({"keep": [{"seq": 1, "rationale": REASON},
                                {"seq": 2, "rationale": REASON}]})
    assert reviser.cycle()["kept_with_reason"] == 2


@pytest.mark.asyncio
async def test_a_keep_naming_a_claim_that_is_not_there_counts_nothing(reviser):
    """Keeps APPLIED, not keeps proposed. Counting a keep that changed no row
    would put the same defect back one layer down: a number saying the drafter
    answered when nothing recorded the answer."""
    touched = await reviser.run({"keep": [{"seq": 99, "rationale": REASON}]})
    assert touched == 0
    assert reviser.cycle()["kept_with_reason"] == 0


@pytest.mark.asyncio
async def test_a_keep_without_a_reason_is_not_a_keep(reviser):
    """A bare refusal to act is not a decision, and the reason is the whole of
    what distinguishes the two."""
    touched = await reviser.run({"keep": [{"seq": 1, "rationale": "no"}]})
    assert touched == 0
    assert reviser.tele["validate"]["revision_1"]["yielded_no_change"] is True
    assert reviser.claims[0].rationale is None


# -- a request met by a citation the answer later lost -------------------------


async def _state(oid):
    return (await objections._load(RUN))[oid]


@pytest.mark.asyncio
async def test_a_request_met_by_a_citation_reopens_when_the_citation_goes(ledger):
    """The claim that cited it can be deleted afterwards. The request then
    stayed resolved and was never put again, while the document was owed."""
    oid = await _ask(subject="a.md")
    await objections.resolve(RUN, ["a.md"])
    assert (await _state(oid))["state"] == objections.RESOLVED
    assert "a.md" in await objections.settled_subjects(RUN)

    assert await objections.reopen_uncited(RUN, set(), cycle=3) == ["a.md"]
    entry = await _state(oid)
    assert entry["state"] == objections.OPEN
    assert entry["reopened"] == [3]
    assert entry["exchanges"] == 1, "the argument's history is not reset"
    assert "a.md" not in await objections.settled_subjects(RUN)


@pytest.mark.asyncio
async def test_a_citation_still_standing_keeps_it_resolved(ledger):
    oid = await _ask(subject="a.md")
    await objections.resolve(RUN, ["a.md"])
    assert await objections.reopen_uncited(RUN, {"a.md"}, cycle=3) == []
    assert (await _state(oid))["state"] == objections.RESOLVED


@pytest.mark.asyncio
async def test_a_request_settled_by_argument_stays_settled(ledger):
    """An accepted refusal was settled by the drafter's reason and the
    review's ruling, not by a claim, so losing a claim does not reopen it."""
    oid = await _ask(subject="a.md")
    await objections.refused(RUN, oid, "the passages read do not state the point")
    await objections.rule(RUN, oid, "accept")
    assert (await _state(oid))["state"] == objections.ACCEPTED
    assert await objections.reopen_uncited(RUN, set(), cycle=3) == []
    assert (await _state(oid))["state"] == objections.ACCEPTED


@pytest.mark.asyncio
async def test_only_source_requests_reopen(ledger):
    """A standing request is resolved when its claim stops being the defect,
    which includes the claim being dropped. That is not a lost citation."""
    oid = await objections.raise_objection(
        RUN, kind="standing", subject="claim-1", asked="rests on commentary",
        cycle=1)
    await objections.resolve(RUN, ["claim-1"], kind="standing")
    assert await objections.reopen_uncited(RUN, set(), cycle=3) == []
    assert (await _state(oid))["state"] == objections.RESOLVED


def test_the_gate_reopens_before_it_decides_what_is_settled():
    """Reopened after `settled` is read, the request would still be skipped
    for the whole cycle in which the loss is first seen."""
    import inspect

    src = inspect.getsource(qr._gate_answer)
    assert src.index("objections.reopen_uncited(") < src.index(
        "objections.settled_subjects(")
