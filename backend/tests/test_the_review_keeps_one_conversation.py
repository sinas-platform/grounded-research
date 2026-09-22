"""The review judges in one conversation, not a fresh call per cycle.

THE DEFECT. Every judging cycle was a one-shot call carrying the question, its
parts, the entire retrieved document set and every rule for judging it.
Measured over one night of benchmark runs: 40,000 tokens a call, 39,266 of
them written to the prompt cache and 732 read back. Sinas' Anthropic provider
sets a rolling cache breakpoint on the last message precisely so that a
sequence of calls reuses the previous one's prefix — a freshly assembled body
each cycle gives it nothing to roll onto. The drafting conversation was built
for this exact defect; this is the review's half of it.

There is a second reason, and it is not about money. The gate had no memory of
its own rulings, so every cycle had to read them back to it. A reviewer that
cannot remember what it already accepted is one that re-raises settled points,
which is the failure the objection ledger exists to stop.

WHAT IS PINNED HERE. That the brief goes once and the draft goes every time,
that the chat is rejoined rather than reopened, and that judging still happens
when no conversation can be opened at all — because this stage decides whether
an answer may publish and does not get to fail on a chat.
"""

from contextlib import asynccontextmanager

import pytest
from app.services import query_runner as qr

VERDICT = '{"publishable": true, "parts": [], "unused_sources": []}'


class _Chatty:
    """A client that keeps chats, recording every message it is sent."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.invoked: list[str] = []

    async def chat_create(self, agent: str, title: str) -> str:
        self.created.append(agent)
        return f"chat-{len(self.created)}"

    async def chat_send(self, chat_id: str, content: str, agent: str = "") -> str:
        self.sent.append((chat_id, content))
        return VERDICT

    async def chat_messages(self, chat_id: str) -> list[dict]:
        return [{"role": "user", "content": self.sent[0][1]}] if self.sent else []

    async def invoke(self, agent: str, message: str) -> str:
        self.invoked.append(message)
        return VERDICT


def _stub_run(monkeypatch, store: dict) -> None:
    """A query_run row whose gate_chat_id persists across calls."""

    class _Run:
        def __init__(self) -> None:
            self.gate_chat_id = store.get("id")

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "gate_chat_id":
                store["id"] = value

    class _Session:
        async def get(self, _model, _ident):
            return _Run()

        async def commit(self):
            return None

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)


@pytest.mark.asyncio
async def test_the_brief_is_sent_once_and_the_draft_every_cycle(monkeypatch):
    store: dict = {}
    _stub_run(monkeypatch, store)
    client = _Chatty()
    run_id = "11111111-1111-1111-1111-111111111111"

    await qr._gate_turn(client, run_id, "THE BRIEF", "DRAFT ONE")
    await qr._gate_turn(client, run_id, "THE BRIEF", "DRAFT TWO")
    await qr._gate_turn(client, run_id, "THE BRIEF", "DRAFT THREE")

    # one chat for the life of the answer
    assert client.created == ["sgr/answer-gate-agent"]
    assert store["id"] == "chat-1"

    bodies = [c for _, c in client.sent]
    assert sum("THE BRIEF" in b for b in bodies) == 1, bodies
    assert [b for b in bodies if "DRAFT" in b] == [
        "DRAFT ONE", "DRAFT TWO", "DRAFT THREE"]
    # the working set never rides along with a draft
    assert not any("THE BRIEF" in b and "DRAFT" in b for b in bodies)


@pytest.mark.asyncio
async def test_a_second_run_of_the_same_answer_rejoins_its_chat(monkeypatch):
    """A resumed run continues the argument instead of starting a new one."""
    store = {"id": "chat-from-before"}
    _stub_run(monkeypatch, store)
    client = _Chatty()

    await qr._gate_turn(client, "22222222-2222-2222-2222-222222222222",
                        "THE BRIEF", "DRAFT ONE")

    assert client.created == []
    assert [c for _, c in client.sent] == ["DRAFT ONE"]


@pytest.mark.asyncio
async def test_judging_still_happens_when_no_chat_can_be_opened(monkeypatch):
    """The stage that decides publication does not fail on a chat."""
    store: dict = {}
    _stub_run(monkeypatch, store)

    class _NoChats(_Chatty):
        async def chat_create(self, agent: str, title: str) -> str:
            raise RuntimeError("chats are down")

    client = _NoChats()
    reply = await qr._gate_turn(client, "33333333-3333-3333-3333-333333333333",
                                "THE BRIEF", "DRAFT ONE")

    assert reply == VERDICT
    assert len(client.invoked) == 1
    # the fallback is the old behaviour exactly: both halves, one call
    assert "THE BRIEF" in client.invoked[0] and "DRAFT ONE" in client.invoked[0]
