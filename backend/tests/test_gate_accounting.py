"""Unit tests for what the answer has accounted for.

The gate does two jobs in one call: it judges each part of the question
covered, and it separately names sources that bear on those parts and sit
uncited. Nothing tied the two together, and its own prompt permits both at
once, so an answer could record the question as answered while a source the
gate itself called decisive was never opened. `unaccounted` is the tie, and
it is computed from the ledger rather than asked of the model.

Its two reads are stubbed, so no DB and no network.

Run from the backend directory:
`python -m pytest tests/test_gate_accounting.py`
"""

import pytest
from app.services import obligations

RUN = "run-1"
ANSWER = "answer-1"


@pytest.fixture
def ledger(monkeypatch):
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


def waiver(by):
    return {"by": by, "rationale": "r"}


async def unaccounted():
    return await obligations.unaccounted(RUN, ANSWER)


# -- what leaves a debt on the answer ----------------------------------------


@pytest.mark.asyncio
async def test_a_named_source_neither_cited_nor_waived_is_unaccounted(ledger):
    ledger["entries"] = {"a.md": entry()}
    assert await unaccounted() == ["a.md"]


@pytest.mark.asyncio
async def test_an_empty_ledger_owes_nothing(ledger):
    assert await unaccounted() == []


@pytest.mark.asyncio
async def test_several_debts_all_come_back(ledger):
    ledger["entries"] = {"a.md": entry(), "b.md": entry(), "c.md": entry()}
    assert sorted(await unaccounted()) == ["a.md", "b.md", "c.md"]


# -- what discharges it ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cited_source_is_accounted_for(ledger):
    ledger["entries"] = {"a.md": entry()}
    ledger["cited"] = {"a.md"}
    assert await unaccounted() == []


@pytest.mark.asyncio
async def test_a_reviser_waiver_accounts_for_it(ledger):
    """The mechanism working: a reviser that read the passages and said why
    the document does not carry a point this answer needs has answered for
    it as surely as citing it."""
    ledger["entries"] = {"a.md": entry(waived=waiver("reviser"))}
    assert await unaccounted() == []


# -- what does not ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_system_waiver_does_not_account_for_it(ledger):
    """MAX_FEEDS retires an obligation the run failed to ground after three
    attempts. That is a record of giving up, not a judgment about the
    document, and reading it as an account would let a run launder its own
    exhaustion into an answer that owes nothing."""
    ledger["entries"] = {"a.md": entry(fed=3, waived=waiver("system"))}
    assert await unaccounted() == ["a.md"]


@pytest.mark.asyncio
async def test_a_waiver_from_neither_does_not_account_for_it(ledger):
    """Only a reviser waiver discharges. An unrecognised `by` is not a
    reviser, and defaulting it to one would make the guard depend on a
    string nobody validates."""
    ledger["entries"] = {"a.md": entry(waived=waiver("someone-else"))}
    assert await unaccounted() == ["a.md"]


@pytest.mark.asyncio
async def test_a_waiver_with_no_author_does_not_account_for_it(ledger):
    ledger["entries"] = {"a.md": entry(waived={"rationale": "r"})}
    assert await unaccounted() == ["a.md"]


@pytest.mark.asyncio
async def test_being_fed_is_not_being_accounted_for(ledger):
    """Feeding is what the run tried, not what the answer did."""
    ledger["entries"] = {"a.md": entry(fed=2)}
    assert await unaccounted() == ["a.md"]


# -- the two kinds side by side ----------------------------------------------


@pytest.mark.asyncio
async def test_only_the_undischarged_come_back(ledger):
    ledger["entries"] = {
        "cited.md": entry(),
        "reviser.md": entry(waived=waiver("reviser")),
        "system.md": entry(fed=3, waived=waiver("system")),
        "owed.md": entry(fed=1),
    }
    ledger["cited"] = {"cited.md"}
    assert sorted(await unaccounted()) == ["owed.md", "system.md"]


# -- a read that fails --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_ledger_reports_no_debt(ledger):
    """Fail-open, like every other read on this ledger: bookkeeping must not
    fail the run it serves. It under-reports the defect rather than blocking
    an answer on a storage fault, which is the safe direction for a record
    that does not gate publication."""
    ledger["raise_on"] = "entries"
    assert await obligations.unaccounted(RUN, ANSWER) == []


@pytest.mark.asyncio
async def test_an_unreadable_claim_set_reports_no_debt(ledger):
    ledger["raise_on"] = "cited"
    ledger["entries"] = {"a.md": entry()}
    assert await obligations.unaccounted(RUN, ANSWER) == []


# -- the tie, in the runner ---------------------------------------------------
#
# The write itself needs a run row, so these pin the wiring the way the
# ledger's other runner-side contracts are pinned: the tie is computed from
# the ledger, it reaches the recorded cycle, and it reaches the reviser.

from pathlib import Path  # noqa: E402

SRC = Path(obligations.__file__).with_name("query_runner.py").read_text(
    encoding="utf-8")


def test_the_gate_asks_the_ledger_what_is_unaccounted():
    assert "obligations.unaccounted(run_id, answer_id)" in SRC


def test_the_cycle_records_it_beside_covered():
    """`covered: true, accounted: false` on one row is the contradiction this
    exists to make legible."""
    assert '"accounted": not unaccounted' in SRC
    assert "gate_unaccounted=unaccounted" in SRC


def test_the_record_is_still_one_write_per_cycle():
    """#106: two writes that have to stay synchronised is how the mirrored
    defect appeared. Moving the record after the ledger must not split it."""
    assert SRC.count("await _record_gate_cycle(") == 2  # parsed and unparseable


def test_the_reviser_is_told_the_question_is_not_fully_answered():
    """Its prompt licenses adding a claim only where the answer fails to
    address the question, and the gate has just said it does not. The
    per-source lines read as "a better source exists" and do not lift that."""
    assert "the question is not " in SRC and "fully answered" in SRC
    assert "Adding a claim that cites one is in scope" in SRC


def test_publication_is_not_tied_to_it():
    """Deliberate. With a measured 0% conversion after three feeds, blocking
    would turn most runs partial without getting anything cited."""
    assert "publishable = bool(data.get(\"publishable\")) and not uncovered" in SRC
    assert "and not unaccounted" not in SRC.split("publishable = ")[1][:120]
