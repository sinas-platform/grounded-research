"""A run that ends partial keeps what was argued and did not settle.

`open_notes` used to be written only on the publish path. A stalled essential
objection is marked `caveat` so a reader sees it, and a partial run is where a
reader most needs to: but a stall that co-occurred with a failed gate went out
through `_mark_partial`, which never read the ledger, and the caveat was lost.

The partial path is stood up here with its storage and its model call stubbed:
the run and answer rows are plain objects, every query returns nothing, and the
note-writer replies with fixed text.

Run from the backend directory:
`python -m pytest tests/test_a_partial_run_keeps_its_caveats.py`
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models import Answer, QueryRun
from app.services import objections, query_runner as qr

ANSWER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

#: A stalled essential request, as `objections.notes` marks it. No `source`,
#: so `_open_notes` has no filename to resolve and returns it as it is.
STALLED = {"id": "o1", "asked": "use the harbour rules for part 2",
           "state": "stalled", "caveat": True}


@pytest.fixture
def partial_env(monkeypatch):
    state = {"tele": {}, "marked": [], "answer": SimpleNamespace(open_notes=None),
             "answer_id": ANSWER_ID, "notes": [STALLED]}

    class _Session:
        async def get(self, model, _ident):
            if model is QueryRun:
                return SimpleNamespace(question="Is the pass valid?",
                                       parent_result_id=None,
                                       answer_id=state["answer_id"])
            if model is Answer:
                return state["answer"]
            return None

        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: [])

        async def commit(self):
            return None

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    async def _tele(_run_id, stage, **detail):
        state["tele"].setdefault(stage, {}).update(detail)

    async def _mark(_run_id, **kw):
        state["marked"].append(kw)

    async def _compact(*_a, **_k):
        return None

    async def _notes(_run_id):
        if isinstance(state["notes"], Exception):
            raise state["notes"]
        return list(state["notes"])

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(qr, "_tele", _tele)
    monkeypatch.setattr(qr, "_mark", _mark)
    monkeypatch.setattr(qr, "_compact_claim_sequences", _compact)
    monkeypatch.setattr(objections, "notes", _notes)
    return state


class _Sinas:
    async def invoke(self, _agent, _message):
        return "This analysis could not establish part 2."


async def _partial():
    await qr._mark_partial(uuid.uuid4(), _Sinas(),
                           qr.PartialOutcome("coverage", "part 2 is not covered"))


@pytest.mark.asyncio
async def test_a_partial_run_keeps_its_open_notes(partial_env):
    await _partial()
    assert partial_env["answer"].open_notes == [STALLED]
    assert partial_env["tele"]["partial"]["open_notes"] == [STALLED]
    assert partial_env["marked"][-1]["status"] == "partial"


@pytest.mark.asyncio
async def test_a_partial_run_with_no_answer_still_records_them(partial_env):
    partial_env["answer_id"] = None
    await _partial()
    assert partial_env["tele"]["partial"]["open_notes"] == [STALLED]
    assert partial_env["answer"].open_notes is None
    assert partial_env["marked"][-1]["status"] == "partial"


@pytest.mark.asyncio
async def test_an_unreadable_ledger_does_not_strand_the_run(partial_env):
    """This function is what moves the run out of its in-flight status. A
    ledger that cannot be read costs the notes, not the run."""
    partial_env["notes"] = RuntimeError("ledger unavailable")
    await _partial()
    assert partial_env["marked"][-1]["status"] == "partial"
    assert partial_env["tele"]["partial"]["open_notes"] == []
    assert partial_env["answer"].open_notes is None


@pytest.mark.asyncio
async def test_nothing_open_is_an_empty_list_not_a_missing_key(partial_env):
    """As on the publish path: the telemetry says the ledger was read and held
    nothing, and the answer row carries no notes rather than an empty list."""
    partial_env["notes"] = []
    await _partial()
    assert partial_env["tele"]["partial"]["open_notes"] == []
    assert partial_env["answer"].open_notes is None
