"""Two findings on one claim, and rewording stops being a move.

A revision round finds a defect and asks for it to be fixed. The model that
just wrote the claim fixes it the cheapest way available: it rewords the same
assertion, keeps the same citation, and sends it back. The next round finds
the same defect, because the defect was never in the wording.

Measured on one live run: seven revision rounds, each changing one to three
claims. One claim failed the evidence judge in the first validation round, was
revised in round five, revised again in round six, and dropped in round seven
with the drafter's own reason — that the claim asserted a step follows
automatically where the real trigger is discretionary. That reason was
available at the second round. Two rounds bought nothing.

A sentence in the prompt already asked for this and did not bite. These pin
the three reasons it could not, and the rule that replaces it:

- the count is kept against the claim's ROW ID, so it survives renumbering;
- the count lives in the run's telemetry, so it survives the validation loop
  restarting itself once per gate cycle;
- and it is enforced on the patch rather than requested in prose: a revision
  of a struck claim that returns the citation the claim already has is not
  applied, and the refusal is recorded.

A claim with one finding is untouched by any of it.

No database and no model: the client is a stub that records what it was sent,
the runner's session is a stub, and the strike ledger's real logic runs over a
dict. Run from the backend directory:
`python -m pytest tests/test_a_claim_gets_two_strikes.py`
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app.services import drafting_chat as dc
from app.services import query_runner as qr
from app.services import strikes

RUN = uuid.uuid4()

#: Long enough to be a rationale the patch parser accepts.
A_REASON = "the cited passage is about a different matter entirely"


# ── what counts as different evidence ────────────────────────────────────────


def test_the_same_citation_written_differently_is_the_same_citation():
    """The fingerprint is about the passage, not about how the span was typed."""
    twice = strikes.fingerprint([{"filename": "a.md", "line_from": 4, "line_to": 9},
                                 {"filename": "a.md", "line_from": 4, "line_to": 9}])
    once = strikes.fingerprint([{"filename": "a.md", "line_from": "4",
                                 "line_to": "9"}])
    assert twice == once == ["a.md:4-9"]


def test_a_span_with_no_document_or_no_line_is_not_a_citation():
    assert strikes.fingerprint(
        [{"line_from": 2}, {"filename": "a.md"},
         {"filename": "", "line_from": 1}, "not a mapping", None]) == []


def test_a_rebinding_reaches_past_what_the_claim_already_stands_on():
    before = ["a.md:4-9"]
    assert strikes.rebinds(before, ["a.md:4-9"]) is False
    assert strikes.rebinds(before, ["a.md:40-52"]) is True, "another passage"
    assert strikes.rebinds(before, ["b.md:4-9"]) is True, "another document"
    assert strikes.rebinds(before, ["a.md:4-9", "b.md:1-3"]) is True


def test_a_revision_carrying_no_evidence_is_not_a_defence():
    """The reword in its purest form: the same assertion, now resting on
    nothing a judge can read."""
    assert strikes.rebinds(["a.md:4-9"], []) is False


# ── the rule, applied to a patch ─────────────────────────────────────────────


def _struck(seq=16, evidence=("a.md:4-9",), findings=2, rejected=0) -> dict:
    return {seq: {"claim_id": f"claim-{seq}", "sequence": seq,
                  "findings": findings, "rejected_rewords": rejected,
                  "evidence": list(evidence)}}


def _patch(**over) -> dict:
    base = {"revise": [], "add": [], "drop": [], "keep": [], "drop_reasons": {},
            "drop_objections": {}, "drop_unexplained": [], "waive": [],
            "refuse": []}
    base.update(over)
    return base


def test_a_reword_of_a_struck_claim_does_not_survive_the_patch():
    patch, rejected = strikes.screen_patch(
        _patch(revise=[{"seq": 16, "text": "t" * 40,
                        "evidence": [{"filename": "a.md", "line_from": 4,
                                      "line_to": 9}]}]),
        _struck())
    assert patch["revise"] == []
    assert [r["sequence"] for r in rejected] == [16]
    assert rejected[0]["claim_id"] == "claim-16"
    assert rejected[0]["cited"] == ["a.md:4-9"]
    assert rejected[0]["offered"] == ["a.md:4-9"]


def test_a_defence_on_different_evidence_is_applied():
    patch, rejected = strikes.screen_patch(
        _patch(revise=[{"seq": 16, "text": "t" * 40,
                        "evidence": [{"filename": "b.md", "line_from": 2,
                                      "line_to": 7}]}]),
        _struck())
    assert rejected == []
    assert [c["seq"] for c in patch["revise"]] == [16]


def test_the_other_two_moves_and_every_unstruck_claim_pass_through():
    """Drop, keep and add are permitted moves; a claim with one finding is
    not the rule's business at all."""
    patch, rejected = strikes.screen_patch(
        _patch(revise=[{"seq": 3, "text": "t" * 40,
                        "evidence": [{"filename": "a.md", "line_from": 4,
                                      "line_to": 9}]}],
               drop=[16], keep=[{"seq": 16, "rationale": A_REASON}],
               add=[{"text": "t" * 40, "evidence": []}]),
        _struck())
    assert rejected == []
    assert [c["seq"] for c in patch["revise"]] == [3]
    assert patch["drop"] == [16] and len(patch["keep"]) == 1
    assert len(patch["add"]) == 1


def test_with_nothing_struck_the_patch_is_returned_as_it_came():
    original = _patch(revise=[{"seq": 16, "text": "t" * 40, "evidence": []}])
    patch, rejected = strikes.screen_patch(original, {})
    assert patch is original and rejected == []


# ── counting, against the row id ─────────────────────────────────────────────


@pytest.fixture
def ledger(monkeypatch):
    """The strike ledger's real logic over a dict instead of a run row."""
    store: dict = {}

    async def _load(_run_id):
        return {k: dict(v) for k, v in store.items()}

    async def _store(_run_id, entries):
        store.clear()
        store.update(entries)

    monkeypatch.setattr(strikes, "_load", _load)
    monkeypatch.setattr(strikes, "_store", _store)
    return store


@pytest.mark.asyncio
async def test_three_failing_spans_on_one_claim_are_one_finding(ledger):
    """Otherwise a claim whose every passage failed would be struck out in the
    round that first looked at it."""
    cid = str(uuid.uuid4())
    tally = await strikes.record_findings(
        RUN, [(cid, 16), (cid, 16), (cid, 16)], round_no=1)
    assert tally[cid] == 1
    assert await strikes.struck(RUN) == set()


@pytest.mark.asyncio
async def test_one_finding_strikes_nothing_and_a_second_strikes_the_claim(ledger):
    cid = str(uuid.uuid4())
    await strikes.record_findings(RUN, [(cid, 16)], round_no=1)
    assert await strikes.struck(RUN) == set()
    await strikes.record_findings(RUN, [(cid, 16)], round_no=2)
    assert await strikes.struck(RUN) == {cid}


@pytest.mark.asyncio
async def test_a_claim_is_the_same_claim_after_it_is_renumbered(ledger):
    """The whole reason the count is not kept on the sequence. Drops close up
    the numbering and additions take the next free number, so claim 16 in
    round one and claim 16 in round five need not be the same proposition —
    and the same proposition need not keep the number."""
    moved, other = str(uuid.uuid4()), str(uuid.uuid4())
    await strikes.record_findings(RUN, [(moved, 16)], round_no=1)
    # Two claims ahead of it were dropped; it is claim 14 now, and something
    # else has been added at 16.
    await strikes.record_findings(RUN, [(moved, 14), (other, 16)], round_no=2)
    assert await strikes.struck(RUN) == {moved}, "identity is the row id"
    entry = (await strikes.entries(RUN))[moved]
    assert entry["findings"] == 2
    assert entry["sequence"] == 14, "found under the number it wears now"
    assert [h["sequence"] for h in entry["history"]] == [16, 14]


@pytest.mark.asyncio
async def test_a_finding_the_round_never_sent_is_not_a_strike(ledger):
    """The caller slices the subjects by the same cap it slices the feedback
    by. A claim the drafter was not asked about cannot have failed to act."""
    verdict = {"failed": [{"claim_id": f"c{i}", "claim_sequence": i,
                           "reason": "no"} for i in range(1, 14)]}
    subjects = qr._finding_subjects(verdict)[:qr.MAX_FEEDBACK_ITEMS]
    await strikes.record_findings(RUN, subjects, round_no=1)
    assert set(await strikes.entries(RUN)) == {f"c{i}" for i in range(1, 11)}


@pytest.mark.asyncio
async def test_the_record_says_what_each_claim_attracted_and_how_it_ended(ledger):
    cid = str(uuid.uuid4())
    await strikes.record_findings(RUN, [(cid, 16)], round_no=1)
    await strikes.record_findings(RUN, [(cid, 16)], round_no=2)
    await strikes.rejected_reword(RUN, cid, 16, round_no=3)
    await strikes.settle(RUN, cid, strikes.DROPPED, round_no=4)
    [row] = await strikes.record(RUN)
    assert row["findings"] == 2 and row["struck"] is True
    assert row["rejected_rewords"] == 1
    assert row["disposition"] == strikes.DROPPED and row["settled_round"] == 4


@pytest.mark.asyncio
async def test_a_storage_fault_costs_the_count_and_not_the_run(monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("no telemetry today")

    monkeypatch.setattr(strikes, "_load", _boom)
    assert await strikes.record_findings(RUN, [("c", 1)], round_no=1) == {}
    assert await strikes.struck(RUN) == set()
    assert await strikes.record(RUN) == []


# ── what the drafter is told ─────────────────────────────────────────────────


def test_the_findings_and_their_subjects_stay_in_step():
    """`_finding_subjects` must name exactly the claims `_round_feedback`
    writes a line about, in the same order: an entry here that is not a line
    there would strike a claim nobody was asked about."""
    verdict = {"failed": [{"claim_id": "a", "claim_sequence": 3, "reason": "r"},
                          {"claim_id": "a", "claim_sequence": 3, "reason": "r2"}],
               "overreaching": [{"claim_id": "b", "claim_sequence": 7,
                                 "uncovered": "the whole period"}]}
    subjects = qr._finding_subjects(verdict)
    assert subjects == [("a", 3), ("a", 3), ("b", 7)]
    assert len(subjects) == len(qr._round_feedback(verdict, set()))


def test_a_second_finding_offers_three_moves_and_rewording_is_not_one():
    verdict = {"failed": [{"claim_id": "a", "claim_sequence": 16,
                           "reason": "the span does not say it."}]}
    [line] = qr._round_feedback(verdict, {16})
    assert "Claim 16" in line
    assert "second round that has failed this claim" in line
    assert "DROP it" in line and "DEFEND it" in line and "REFUSE" in line
    assert "rewording is no longer one of your moves" in line
    assert "Do not reword it again" in line


def test_a_claim_with_one_finding_hears_nothing_about_a_second():
    verdict = {"failed": [{"claim_id": "a", "claim_sequence": 16,
                           "reason": "the span does not say it."}],
               "overreaching": [{"claim_id": "b", "claim_sequence": 7,
                                 "uncovered": "the whole period"}]}
    assert all("second round" not in line
               for line in qr._round_feedback(verdict, set()))


def test_a_finding_whose_sequence_is_not_a_number_names_no_struck_claim():
    verdict = {"failed": [{"claim_sequence": None, "reason": "r"},
                          {"claim_sequence": True, "reason": "r"}],
               "overreaching": [{"uncovered": "u"}]}
    assert qr._finding_subjects(verdict) == [("", None), ("", None), ("", None)]
    assert all("second round" not in line
               for line in qr._round_feedback(verdict, {1, 16}))


def test_the_standing_block_names_the_claims_and_the_three_moves():
    block = dc.strike_block([{"sequence": 16, "findings": 2,
                              "rejected_rewords": 1}])
    assert "claim 16" in block and "2 findings" in block
    assert "already refused" in block
    assert "DROP it" in block and "DEFEND it" in block and "REFUSE" in block


def test_nothing_struck_means_no_block_at_all():
    assert dc.strike_block([]) == ""


# ── the runner's end of it ───────────────────────────────────────────────────


class _Client:
    """Enough of the Sinas client to hold a conversation, and a record of it."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.sent: list[tuple[str, str]] = []
        self.asks: list[str] = []

    async def chat_create(self, agent: str, title: str) -> str:
        return "chat-1"

    async def chat_send(self, chat_id: str, content: str,
                        agent: str = "") -> str:
        self.sent.append((chat_id, content))
        if content.rstrip().endswith(dc.ACK) or content.endswith(dc.ACK_INSTRUCTION):
            return dc.ACK
        self.asks.append(content)
        return self.replies[len(self.asks) - 1] if (
            len(self.asks) <= len(self.replies)) else "{}"

    async def chat_messages(self, chat_id: str) -> list[dict]:
        return [{"role": "user", "content": c} for _cid, c in self.sent]

    @property
    def contents(self) -> list[str]:
        return [c for _cid, c in self.sent]


class _Claim:
    def __init__(self, seq):
        self.id = uuid.uuid4()
        self.sequence = seq
        self.claim_text = f"Claim {seq} says a thing."
        self.rationale = None
        self.section = "analysis"
        self.part_index = 0
        self.claim_kind = "rule"
        # The model has this column and `_apply_structure` reads it to report
        # a kind that moved without its test. A stub is only as complete as
        # the paths it happened to reach.
        self.test = None


@pytest.fixture
def round_(monkeypatch, ledger):
    """`_revise_answer` over a stub session, a recording client, and the real
    strike ledger. Claim 16 cites a.md lines 4-9 and nothing else."""
    tele: dict = {}
    run_row = SimpleNamespace(parent_result_id=None, telemetry=tele,
                              synthesis_chat_id="chat-1")
    claims = [_Claim(16), _Claim(17)]
    by_id = {c.id: c for c in claims}
    evidence = SimpleNamespace(span={"line_from": 4, "line_to": 9})
    rows = [(claims[0], evidence, "a.md", "content"),
            (claims[1], None, None, None)]
    client = _Client()

    class _Session:
        async def get(self, model, ident):
            name = getattr(model, "__name__", "")
            if name == "QueryRun":
                return run_row
            if name == "Answer":
                return SimpleNamespace(question_parts=[])
            return by_id.get(ident)

        async def execute(self, *_a, **_k):
            return SimpleNamespace(
                all=lambda: list(rows),
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

    async def _rows(*_a, **_k):
        return []

    async def _zero(*_a, **_k):
        return 0

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(qr, "_tele", _tele)
    monkeypatch.setattr(qr, "_manifest_rows", _rows)
    monkeypatch.setattr(qr, "_removal_record", _rows)
    monkeypatch.setattr(qr, "_cap_refusals_last_cycle", _zero)
    monkeypatch.setattr(qr, "_bind_spans", _none)
    monkeypatch.setattr(qr, "flag_modified", lambda *_a, **_k: None)

    async def strike(claim, times=2):
        for n in range(1, times + 1):
            await strikes.record_findings(
                RUN, [(str(claim.id), claim.sequence)], round_no=n)

    async def run(patch: dict, feedback=None) -> int:
        client.replies.append(json.dumps(patch))
        return await qr._revise_answer(
            client, RUN, uuid.uuid4(),
            feedback or ["Claim 16: the span does not carry it."])

    return SimpleNamespace(run=run, client=client, tele=tele, claims=claims,
                           strike=strike,
                           cycle=lambda: tele["validate"]["revision_1"])


@pytest.mark.asyncio
async def test_the_round_tells_the_struck_claim_it_has_three_moves_left(round_):
    await round_.strike(round_.claims[0])
    # The feedback the validation loop would build for a struck claim, so the
    # turn carries both the standing block and the sentence in the finding.
    feedback = qr._round_feedback(
        {"failed": [{"claim_id": str(round_.claims[0].id), "claim_sequence": 16,
                     "reason": "the span does not say it."}]}, {16})
    await round_.run({"drop": [{"seq": 16, "rationale": A_REASON}]}, feedback)
    turn = round_.client.contents[0]
    assert "CLAIMS ON THEIR SECOND FINDING" in turn
    assert "claim 16: 2 findings" in turn
    assert "DROP it" in turn and "DEFEND it" in turn and "REFUSE" in turn
    assert "rewording them is no longer one of your moves" in turn
    # and again where the decision is actually made
    assert "second round that has failed this claim" in turn
    entry = (await strikes.entries(RUN))[str(round_.claims[0].id)]
    assert entry["disposition"] == strikes.DROPPED


@pytest.mark.asyncio
async def test_a_reword_of_a_struck_claim_is_refused_and_recorded(round_):
    """The measured waste, stopped: round five reworded, round six reworded
    the same claim again on the same citation, round seven dropped it."""
    await round_.strike(round_.claims[0])
    touched = await round_.run({"revise": [
        {"seq": 16, "text": "The same assertion, differently put, at length.",
         "evidence": [{"filename": "a.md", "line_from": 4, "line_to": 9}]}]})
    assert touched == 0, "nothing was applied"
    assert round_.claims[0].claim_text == "Claim 16 says a thing."
    cycle = round_.cycle()
    assert cycle["yielded_no_change"] is True
    assert [r["sequence"] for r in cycle["rewords_refused"]] == [16]
    entry = (await strikes.entries(RUN))[str(round_.claims[0].id)]
    assert entry["rejected_rewords"] == 1
    assert entry["findings"] == 2, "a refused reword is not a fresh finding"


@pytest.mark.asyncio
async def test_a_defence_on_a_different_passage_is_applied(round_):
    await round_.strike(round_.claims[0])
    touched = await round_.run({"revise": [
        {"seq": 16, "text": "The trigger is discretionary, on this passage.",
         "evidence": [{"filename": "b.md", "line_from": 30, "line_to": 38}],
         "rationale": A_REASON}]})
    assert touched == 1
    assert round_.claims[0].claim_text.startswith("The trigger is")
    cycle = round_.cycle()
    assert cycle["revised"] == 1 and cycle["rewords_refused"] == []
    assert cycle["struck_claims"] == [16]
    entry = (await strikes.entries(RUN))[str(round_.claims[0].id)]
    assert entry["disposition"] == strikes.DEFENDED


@pytest.mark.asyncio
async def test_a_drop_and_a_keep_are_the_other_two_moves(round_):
    await round_.strike(round_.claims[0])
    await round_.run({"keep": [{"seq": 16, "rationale": A_REASON}]})
    entry = (await strikes.entries(RUN))[str(round_.claims[0].id)]
    assert entry["disposition"] == strikes.REFUSED


@pytest.mark.asyncio
async def test_a_claim_with_one_finding_may_still_be_reworded(round_):
    """The rule is about the second finding. A first one is a defect to fix
    however the drafter sees fit, and nothing here narrows that."""
    await round_.strike(round_.claims[0], times=1)
    touched = await round_.run({"revise": [
        {"seq": 16, "text": "The same assertion, differently put, at length.",
         "evidence": [{"filename": "a.md", "line_from": 4, "line_to": 9}],
         "rationale": A_REASON}]})
    assert touched == 1
    assert round_.claims[0].claim_text.startswith("The same assertion")
    assert round_.cycle()["rewords_refused"] == []
    assert round_.cycle()["struck_claims"] == []
    assert "CLAIMS ON THEIR SECOND FINDING" not in round_.client.contents[0]


@pytest.mark.asyncio
async def test_an_unstruck_claim_in_the_same_patch_is_untouched_by_the_rule(round_):
    """One claim struck, one not, one patch."""
    await round_.strike(round_.claims[0])
    touched = await round_.run({"revise": [
        {"seq": 16, "text": "The same assertion, differently put, at length.",
         "evidence": [{"filename": "a.md", "line_from": 4, "line_to": 9}]},
        {"seq": 17, "text": "A narrower statement of what the passage says.",
         "evidence": [{"filename": "a.md", "line_from": 4, "line_to": 9}],
         "rationale": A_REASON}]})
    assert touched == 1
    assert round_.claims[0].claim_text == "Claim 16 says a thing."
    assert round_.claims[1].claim_text.startswith("A narrower statement")
    assert [r["sequence"] for r in round_.cycle()["rewords_refused"]] == [16]
