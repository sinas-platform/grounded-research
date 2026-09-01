"""Unit tests for what the ledger decides to feed each round.

These exercise `obligations.to_feed` itself, not a copy of it: `obligations`
does not import the private SDK, which is why the decision lives there rather
than in the gate. Its two reads are stubbed, so no DB and no network.

Run from the backend directory:
`python -m pytest tests/test_obligation_fallback.py`
"""

import pytest
from app.services import obligations

RUN = "run-1"
ANSWER = "answer-1"


@pytest.fixture
def ledger(monkeypatch):
    """Stub the two reads `to_feed` makes, and let a test set what they say."""
    state = {"entries": {}, "cited": set(), "raise_on": None}

    async def fake_load(run_id):
        if state["raise_on"] == "entries":
            raise RuntimeError("ledger unreadable")
        return state["entries"]

    async def fake_cited(answer_id):
        if state["raise_on"] == "cited":
            raise RuntimeError("claims unreadable")
        return state["cited"]

    monkeypatch.setattr(obligations, "_load", fake_load)
    monkeypatch.setattr(obligations, "_cited", fake_cited)
    return state


def entry(note="n", fed=0, waived=None):
    return {"note": note, "fed": fed, "waived": waived}


async def feed(fresh=None):
    return await obligations.to_feed(RUN, ANSWER, fresh or {})


def docs(result):
    return [u["doc"] for u in result]


# ── the ledger's live debts ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_live_entry_is_fed(ledger):
    ledger["entries"] = {"a.md": entry(fed=1)}
    assert docs(await feed()) == ["a.md"]


@pytest.mark.asyncio
async def test_a_live_entry_carries_its_note_and_count(ledger):
    ledger["entries"] = {"a.md": entry(note="why it matters", fed=2)}
    got = (await feed())[0]
    assert got["note"] == "why it matters"
    assert got["fed"] == 2


@pytest.mark.asyncio
async def test_oldest_first_is_preserved(ledger):
    ledger["entries"] = {"a.md": entry(), "b.md": entry(), "c.md": entry()}
    assert docs(await feed()) == ["a.md", "b.md", "c.md"]


# ── what the ledger withholds ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_waived_entry_is_not_fed(ledger):
    ledger["entries"] = {"a.md": entry(waived={"by": "reviser", "rationale": "r"})}
    assert await feed() == []


@pytest.mark.asyncio
async def test_a_cited_entry_is_not_fed(ledger):
    """Satisfaction is computed: while a surviving claim cites it, it is met."""
    ledger["entries"] = {"a.md": entry()}
    ledger["cited"] = {"a.md"}
    assert await feed() == []


@pytest.mark.asyncio
async def test_a_waived_document_named_again_is_not_revived(ledger):
    """A waiver is the reviser deciding not to cite, so the document keeps
    qualifying and would otherwise come back every round."""
    ledger["entries"] = {"a.md": entry(fed=2, waived={"by": "reviser", "rationale": "r"})}
    assert await feed({"a.md": "the gate names it again"}) == []


@pytest.mark.asyncio
async def test_a_cited_document_named_again_is_not_revived(ledger):
    """Telling the reviser to cite what the answer already cites spends a
    round on nothing."""
    ledger["entries"] = {"a.md": entry()}
    ledger["cited"] = {"a.md"}
    assert await feed({"a.md": "the gate names it again"}) == []


# ── this round's findings ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_document_is_fed(ledger):
    assert docs(await feed({"new.md": "why"})) == ["new.md"]


@pytest.mark.asyncio
async def test_a_document_already_owed_is_not_duplicated(ledger):
    ledger["entries"] = {"a.md": entry(fed=1)}
    got = await feed({"a.md": "named again"})
    assert docs(got) == ["a.md"]
    assert got[0]["fed"] == 1


@pytest.mark.asyncio
async def test_a_known_document_carries_the_ledger_count_not_zero(ledger):
    """The cap compares against this. A hard-coded zero meant an entry added
    from this round's findings was never capped however often it was fed."""
    ledger["entries"] = {"a.md": entry(fed=2)}
    got = await feed({"a.md": "named again"})
    assert got[0]["fed"] == 2


# ── a read that fails ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unreadable_ledger_still_feeds_this_round(ledger):
    """The pre-ledger behaviour the ledger must degrade to rather than fail
    into: with nothing known, this round's findings pass through."""
    ledger["raise_on"] = "entries"
    assert docs(await feed({"a.md": "why", "b.md": "why"})) == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_an_unreadable_claim_set_still_feeds_this_round(ledger):
    ledger["raise_on"] = "cited"
    assert docs(await feed({"a.md": "why"})) == ["a.md"]


@pytest.mark.asyncio
async def test_a_failed_read_does_not_fail_the_round(ledger):
    ledger["raise_on"] = "entries"
    assert await feed() == []


@pytest.mark.asyncio
async def test_losing_the_ledger_keeps_what_is_cited(ledger):
    """One read failing is no reason to discard the other. Without the
    entries, a document a surviving claim cites is still not owed."""
    ledger["raise_on"] = "entries"
    ledger["cited"] = {"cited.md"}
    got = await feed({"cited.md": "why", "new.md": "why"})
    assert docs(got) == ["new.md"]


@pytest.mark.asyncio
async def test_losing_the_claims_keeps_what_is_waived(ledger):
    """And without the claims, a waived document is still retired. Knowing one
    of the two beats knowing neither."""
    ledger["raise_on"] = "cited"
    ledger["entries"] = {"waived.md": entry(waived={"by": "reviser", "rationale": "r"})}
    got = await feed({"waived.md": "why", "new.md": "why"})
    assert docs(got) == ["new.md"]
