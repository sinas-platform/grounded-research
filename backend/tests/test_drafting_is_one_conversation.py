"""Drafting is one conversation per answer, and a round is only what is new.

Every revision used to be a fresh stateless call re-sending the whole world:
the passages, the playbook, the structure rules, the standing revision
instructions, and every current claim with the evidence bound to it. Two
things were measured on live runs and both follow from that shape.

The drafter could not argue. A refusal — "this source cannot carry the point,
and here is why" — is a legal move with an id to name, and on the run that
motivated this work 7 objections were raised, 0 answered, 0 ruled on, and
`kept_with_reason` read 0 in all ten revision rounds. A call with no record of
its own reasoning has nothing to refuse with.

And it could not cache. 14 calls, 131,710 prompt tokens, 131,685 written to
cache, 0 read back — against a provider that already sets a rolling cache
breakpoint on the last message so that consecutive calls reuse the prefix. A
freshly assembled body has no prefix to reuse.

What these pin, in the order a run meets them:

- turn one is the brief, it carries the durable material, and it is sent once;
- a round carries the findings and the ids — not the passages, not the
  playbook, not the claims the drafter can already see;
- the chat id is written to the run row and a resumed run rejoins it;
- at the cap the conversation restarts from the SAME BYTES of turn one, with
  a summary standing in for the rounds it drops;
- a refusal reaches the ledger, and a keep with a reason counts as work.

No database and no model: the client is a stub that records what it was sent,
and the runner's session is a stub, so the logic under test is the real one.

Run from the backend directory:
`python -m pytest tests/test_drafting_is_one_conversation.py`
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app.services import drafting_chat as dc
from app.services import objections
from app.services import query_runner as qr

RUN = uuid.uuid4()
BRIEF = "THE BRIEF.\nPASSAGE GROUP 1\n  [a.md lines 1-2]\n  The passage."


class _Client:
    """Enough of the Sinas client to hold a conversation, and a record of it.

    A turn asking for an acknowledgement gets one; anything else gets the next
    scripted reply. That is the real split: turn one asks for nothing, so the
    scripted replies line up with the turns that ask for work.
    """

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.opened: list[str] = []
        #: (chat_id, content) for every message sent
        self.sent: list[tuple[str, str]] = []
        self.asks: list[str] = []

    async def chat_create(self, agent: str, title: str) -> str:
        self.opened.append(f"chat-{len(self.opened) + 1}")
        return self.opened[-1]

    async def chat_send(self, chat_id: str, content: str,
                        agent: str = "") -> str:
        self.sent.append((chat_id, content))
        if content.rstrip().endswith(dc.ACK):
            return dc.ACK
        if content.endswith(dc.ACK_INSTRUCTION):
            return dc.ACK
        self.asks.append(content)
        return self.replies[len(self.asks) - 1] if (
            len(self.asks) <= len(self.replies)) else "{}"

    async def chat_messages(self, chat_id: str) -> list[dict]:
        return [{"role": "user", "content": c}
                for cid, c in self.sent if cid == chat_id]

    @property
    def contents(self) -> list[str]:
        return [c for _cid, c in self.sent]


def _chat(client, **over) -> dc.DraftingChat:
    return dc.DraftingChat(client=client, agent="sgr/an-agent", **over)


# ── turn one ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_turn_one_is_the_brief_and_it_is_sent_once():
    """The whole economy of the thing. Turn one is the expensive message — the
    passages, the playbook, the rules — and it is written to cache once and
    read back by every turn after it."""
    client = _Client()
    chat = _chat(client)
    await chat.start(BRIEF)
    await chat.ask("draft it")
    await chat.turn("round one findings")
    await chat.turn("round two findings")
    assert sum(1 for c in client.contents if BRIEF in c) == 1
    assert client.contents[0].startswith(BRIEF)
    assert len(client.opened) == 1


@pytest.mark.asyncio
async def test_turn_one_asks_for_an_acknowledgement_not_for_work():
    """Which is what makes it replayable: a brief that asks for a draft cannot
    be re-sent to restart a conversation without paying for a whole draft that
    is then thrown away."""
    client = _Client('{"claims": []}')
    chat = _chat(client)
    await chat.start(BRIEF)
    assert client.contents[0].endswith(dc.ACK_INSTRUCTION)
    assert dc.ACK in client.contents[0]
    # and the brief consumed none of the scripted replies
    assert client.asks == []


@pytest.mark.asyncio
async def test_a_round_carries_the_findings_and_nothing_it_can_already_see():
    client = _Client()
    chat = _chat(client)
    await chat.start(BRIEF)
    await chat.turn("Claim 3 asserts more than its passages establish.")
    round_turn = client.contents[-1]
    assert "Claim 3" in round_turn
    assert "The passage." not in round_turn
    assert "PASSAGE GROUP" not in round_turn


# ── rejoining ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_resumed_run_rejoins_rather_than_opening_a_second_chat():
    """A second conversation about the same answer would pay for the brief
    twice and give the drafter no memory of the first."""
    client = _Client()
    chat = _chat(client)
    assert await chat.start(BRIEF, "chat-from-before") == "chat-from-before"
    assert client.opened == []
    assert client.sent == []
    assert chat.resumed
    await chat.turn("findings")
    assert client.sent[0][0] == "chat-from-before"


@pytest.mark.asyncio
async def test_a_restored_chat_id_is_rejoined_without_being_passed_again():
    """How a later stage joins: it rebuilds the object from the run row, and
    the id it restored is the id it speaks into."""
    client = _Client()
    chat = _chat(client)
    chat.chat_id = "chat-stored"
    await chat.start(BRIEF)
    assert client.opened == [] and chat.chat_id == "chat-stored"


@pytest.mark.asyncio
async def test_the_rounds_survive_being_rebuilt_from_the_run_row():
    """The cap counts rounds, so a stage that rebuilds the object and counts
    from zero is a conversation that grows without bound."""
    client = _Client()
    chat = _chat(client, max_exchanges=2)
    await chat.start(BRIEF)
    await chat.turn("one")
    chat.note("revised 1")
    state = chat.state()

    later = _chat(_Client()).restore(state)
    assert later.chat_id == chat.chat_id
    assert [e.note for e in later.exchanges] == ["revised 1"]
    assert later.rounds == 2


# ── the cap ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_at_the_cap_the_conversation_restarts_from_the_same_bytes():
    """Byte-identical, because the cache is keyed on the request prefix: the
    same system prompt followed by the same first message reads back what the
    previous chat wrote instead of paying for it again."""
    client = _Client()
    chat = _chat(client, max_exchanges=2)
    await chat.start(BRIEF)
    for i in range(3):
        await chat.turn(f"round {i} findings")
        chat.note(f"revised {i}")
    assert len(client.opened) == 2
    briefs = [c for c in client.contents if c.startswith(BRIEF)]
    assert len(briefs) == 2
    assert briefs[0] == briefs[1], "turn one must be replayed exactly"


@pytest.mark.asyncio
async def test_the_summary_stands_in_for_the_rounds_it_replaces():
    client = _Client()
    chat = _chat(client, max_exchanges=2)
    await chat.start(BRIEF)
    for i, note in enumerate(("revised 2, dropped 1", "refused obj-abc")):
        await chat.turn(f"round {i} findings")
        chat.note(note)
    await chat.turn("round three findings")

    summary = next(c for c in client.contents if "EARLIER IN THIS" in c)
    assert "revised 2, dropped 1" in summary and "refused obj-abc" in summary
    # the rounds themselves are gone, and the round after them is not
    assert "round 0 findings" not in client.contents[-1]
    assert "round three findings" in client.contents[-1]
    # the summary sits after turn one in the new chat, not before it
    new_chat = client.opened[-1]
    in_new = [c for cid, c in client.sent if cid == new_chat]
    assert in_new[0].startswith(BRIEF) and "EARLIER IN THIS" in in_new[1]


@pytest.mark.asyncio
async def test_the_cap_counts_only_the_rounds_not_every_turn():
    """New passages and a retry are turns too. Counting them would restart the
    conversation for reasons that have nothing to do with how much argument it
    is carrying."""
    client = _Client()
    chat = _chat(client, max_exchanges=2)
    await chat.start(BRIEF)
    await chat.ask("draft it")
    await chat.ask("new passages")
    await chat.turn("round one")
    assert len(client.opened) == 1


@pytest.mark.asyncio
async def test_room_is_made_before_a_round_starts_not_in_the_middle_of_one():
    """A round is not always one message — new passages go first, in a turn of
    their own. Compacting after them would open a chat the passages had never
    reached, and the findings would name evidence the drafter cannot see."""
    client = _Client()
    chat = _chat(client, max_exchanges=1)
    await chat.start(BRIEF)
    await chat.turn("round one")
    chat.note("revised 1")

    await chat.prepare()
    await chat.ask("NEW VERIFIED PASSAGES: [b.md lines 4-5] A new passage.")
    await chat.turn("round two findings")

    new_chat = client.opened[-1]
    in_new = [c for cid, c in client.sent if cid == new_chat]
    assert in_new[0].startswith(BRIEF)
    assert "A new passage." in " ".join(in_new)
    assert "round two findings" in in_new[-1]


@pytest.mark.asyncio
async def test_a_conversation_whose_brief_cannot_be_recovered_is_not_restarted():
    """A restart that paraphrased the brief would be a drafter asked to patch
    an answer it can no longer see. Carrying the rounds whole costs tokens;
    this would cost the answer."""
    class _Amnesiac(_Client):
        async def chat_messages(self, chat_id: str) -> list[dict]:
            return []

    client = _Amnesiac()
    chat = _chat(client, max_exchanges=1)
    await chat.start(BRIEF, "chat-from-before")  # no brief composed here
    chat.opening = ""
    await chat.turn("round one")
    await chat.turn("round two")
    assert client.opened == []
    assert all("EARLIER IN THIS" not in c for c in client.contents)


# ── what a round says ────────────────────────────────────────────────────────

def test_an_outstanding_request_reaches_the_round_with_the_id_that_answers_it():
    """It used to reach the drafter buried in the prose of one feedback line
    among ten, in a call that had no memory of ever being asked. Nothing ever
    refused."""
    block = dc.objections_block([
        {"id": "obj-12ab34cd", "subject": "a.md", "asked": "cite it for the rule",
         "why_essential": "part one turns on it"}])
    assert "obj-12ab34cd" in block
    assert "cite it for the rule" in block
    assert "essential" in block
    assert '"refuse"' in block and '"waive"' in block


def test_no_outstanding_request_means_no_block_at_all():
    assert dc.objections_block([]) == ""


def test_the_numbering_key_is_a_key_and_not_the_answer_read_back():
    """The drafter can see every claim it wrote. What it cannot see is the
    number the engine gave it, and a patch keys on that number."""
    key = dc.numbering_key([(1, "A claim about something."), (2, "B" * 300)])
    assert "1. A claim about something." in key
    assert len(key) < 400, "a key, not a second copy of the answer"


def test_a_claim_the_engine_removed_is_said_so():
    """The one thing the transcript genuinely cannot show: a drafter reading
    its own turns would still believe the claim is there."""
    out = dc.removed_by_the_engine([{"sequence": 4, "why": "no span held"}])
    assert "claim 4" in out and "no span held" in out


def test_a_finding_offers_abandonment_and_presses_it_the_second_time():
    """"Narrow it to what the passages say" is an instruction to adjust, and a
    model that has just written a claim will adjust it — reword the same
    assertion and send it back for the next round to find again."""
    verdict = {"failed": [{"claim_sequence": 3, "reason": "the span does not say it."}],
               "overreaching": [{"claim_sequence": 7, "uncovered": "the whole period"}]}
    first = qr._round_feedback(verdict, set())
    assert all("ABANDON" in line for line in first)
    assert all("second round" not in line for line in first)

    again = qr._round_feedback(verdict, {3, 7})
    assert all("second round that has failed this claim" in line for line in again)
    assert all("Do not reword it again" in line for line in again)


def test_a_finding_about_no_claim_in_particular_still_reads():
    """A verdict entry whose sequence is missing or unparseable must not turn
    the repeat check into a crash."""
    verdict = {"failed": [{"claim_sequence": None, "reason": "r"}],
               "overreaching": [{"uncovered": "u"}]}
    assert len(qr._round_feedback(verdict, {3})) == 2


# ── the runner's end of it ───────────────────────────────────────────────────

@pytest.fixture
def ledger(monkeypatch):
    """The objection ledger's real logic over a dict instead of a run row."""
    store: dict = {}

    async def _load(_run_id):
        return dict(store)

    async def _store(_run_id, entries):
        store.clear()
        store.update(entries)

    monkeypatch.setattr(objections, "_load", _load)
    monkeypatch.setattr(objections, "_store", _store)
    return store


class _Claim:
    def __init__(self, seq):
        self.id = uuid.uuid4()
        self.sequence = seq
        self.claim_text = f"Claim {seq} says a thing."
        self.rationale = None
        self.section = "analysis"
        self.part_index = 0
        self.claim_kind = "legal_principle"


@pytest.fixture
def round_(monkeypatch):
    """`_revise_answer` over a stub session and a recording client."""
    tele: dict = {}
    run_row = SimpleNamespace(parent_result_id=None, telemetry=tele,
                              synthesis_chat_id="chat-1")
    claims = [_Claim(1), _Claim(2)]
    by_id = {c.id: c for c in claims}
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

    async def run(patch: dict, feedback=None) -> int:
        client.replies.append(json.dumps(patch))
        return await qr._revise_answer(
            client, RUN, uuid.uuid4(),
            feedback or ["Claim 1: the named source is more direct."])

    return SimpleNamespace(run=run, client=client, tele=tele, claims=claims,
                           run_row=run_row,
                           cycle=lambda: tele["validate"]["revision_1"])


@pytest.mark.asyncio
async def test_a_revision_is_a_turn_in_the_chat_the_draft_opened(round_, ledger):
    await round_.run({"keep": [{"seq": 1, "rationale": "the cited source "
                                "carries the deciding body's own reasoning"}]})
    assert [cid for cid, _c in round_.client.sent] == ["chat-1"]
    turn = round_.client.contents[0]
    assert "REVIEW FINDINGS" in turn
    assert "the named source is more direct" in turn
    # the numbering travels; the answer does not
    assert "1. Claim 1 says a thing." in turn
    assert "PASSAGE GROUP" not in turn


@pytest.mark.asyncio
async def test_the_conversation_is_written_back_to_the_run_row(round_, ledger):
    await round_.run({"keep": [{"seq": 1, "rationale": "the cited source "
                                "carries the deciding body's own reasoning"}]})
    assert round_.run_row.synthesis_chat_id == "chat-1"
    stored = round_.run_row.telemetry[qr.DRAFT_CHAT_KEY]
    assert stored["chat_id"] == "chat-1"
    assert [e["note"] for e in stored["exchanges"]] == [
        "revised 0, added 0, dropped 0, kept 1 with a reason, 0 refusal(s)"]


@pytest.mark.asyncio
async def test_a_round_with_no_conversation_behind_it_is_not_sent(round_, ledger):
    """A model asked to patch an answer it has never seen is worse than a
    round that does not happen."""
    round_.run_row.synthesis_chat_id = None
    assert await round_.run({"drop": [1]}) == 0
    assert round_.client.sent == []


@pytest.mark.asyncio
async def test_an_outstanding_request_is_put_to_the_drafter_with_its_id(
        round_, ledger):
    oid = await objections.raise_objection(
        RUN, kind="source", subject="a.md", asked="cite it for the rule",
        cycle=1)
    await round_.run({"keep": [{"seq": 1, "rationale": "the cited source "
                                "carries the deciding body's own reasoning"}]})
    assert oid in round_.client.contents[0]


@pytest.mark.asyncio
async def test_a_refusal_reaches_the_ledger_and_the_review_must_rule_on_it(
        round_, ledger):
    """The move that was legal and never made. Nothing else about the loop
    changed — the ledger, the ruling, the bound are all as they were — only
    the drafter's ability to remember what it argued."""
    oid = await objections.raise_objection(
        RUN, kind="source", subject="a.md", asked="cite it for the rule",
        cycle=1)
    reason = ("the passage reports an argument, not a finding, so it cannot "
              "carry the point asked of it")
    await round_.run({"refuse": [{"objection": oid, "rationale": reason}]})

    assert ledger[oid]["state"] == objections.ANSWERED
    assert [o["id"] for o in await objections.outstanding(RUN)] == [oid]
    assert round_.tele["validate"]["revision_1"]["refusals"] == 1


@pytest.mark.asyncio
async def test_kept_with_reason_is_reachable_in_this_shape(round_, ledger):
    """It read 0 in all ten revision rounds of the measured run. A keep is the
    drafter answering the feedback rather than obeying it, which is precisely
    what a stateless call had nothing to do it with."""
    reason = ("the cited source carries the deciding body's own reasoning on "
              "this point; the named one only restates it")
    oid = await objections.raise_objection(
        RUN, kind="source", subject="a.md", asked="cite it for the rule",
        cycle=1)
    touched = await round_.run(
        {"keep": [{"seq": 1, "rationale": reason, "objection": oid}]})
    assert touched == 1
    assert round_.cycle()["kept_with_reason"] == 1
    assert round_.claims[0].rationale == reason
    # and a keep that answers a request is a reply, so it reaches the ledger
    assert ledger[oid]["state"] == objections.ANSWERED
