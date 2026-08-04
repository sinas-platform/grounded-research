"""Unit tests for ingestion-runner failure detection and the
worker-driven secondary-stage handoff.

Run from the backend directory: `python -m pytest tests/test_ingestion_runner.py`
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.ingestion_runner as ingestion_runner
from app.services.ingestion_runner import (
    _agent_reply_is_rate_limited,
    _maybe_submit_secondary,
    _run_oneshot_inprocess,
)


class _FakeChats:
    """Stand-in for SinasClient.chats with a synchronous get()."""

    def __init__(self, chat: dict):
        self._chat = chat

    def get(self, chat_id: str) -> dict:
        return self._chat


class _FakeClient:
    def __init__(self, chat: dict):
        self.chats = _FakeChats(chat)


# The assistant reply Sinas stores when Anthropic rejects the call with a 400
# spend/usage cap (captured from a real ingestion run). The 429 markers do not
# match this, so before the fix the unit was marked succeeded with no data.
_USAGE_CAP_REPLY = (
    "An error occurred while processing your message. Please try again. "
    "Error: Error code: 400 - {'type': 'error', 'error': "
    "{'type': 'invalid_request_error', 'message': "
    "'You have reached your specified API usage limits. ...'}}"
)


@pytest.mark.asyncio
async def test_usage_cap_error_is_detected_as_failure():
    chat = {
        "messages": [
            {"role": "user", "content": "Classify document abc."},
            {"role": "assistant", "content": _USAGE_CAP_REPLY},
        ]
    }
    assert await _agent_reply_is_rate_limited(_FakeClient(chat), "chat-1") is True


@pytest.mark.asyncio
async def test_rate_limit_error_still_detected():
    chat = {
        "messages": [
            {"role": "assistant", "content": "Error code: 429 - rate_limit_error"},
        ]
    }
    assert await _agent_reply_is_rate_limited(_FakeClient(chat), "chat-1") is True


@pytest.mark.asyncio
async def test_clean_reply_is_not_a_failure():
    chat = {
        "messages": [
            {"role": "assistant", "content": "Classification Complete. Bulletin article."},
        ]
    }
    assert await _agent_reply_is_rate_limited(_FakeClient(chat), "chat-1") is False


# ─────────────────────────────────────────────────────────────
# Worker-driven secondary-stage submission.
#
# Regression: secondary stages used to be submitted ONLY from the
# progress() path (GET /ingestion/runs/{id}). A multi-stage run nobody
# polled sat at "running" with the secondary units pending forever once
# the oneshot pass finished (observed live 2026-08-04, run ea5edcba).
# The in-process worker must fire the secondary wave itself.
# ─────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def all(self):
        return []


class _FakeSession:
    """Session double shared through a mutable `state` dict — routes the
    runner's few statement shapes by their SQL text."""

    def __init__(self, state: dict):
        self._state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, pk):
        return self._state["run"]

    async def execute(self, stmt, params=None):
        s = str(stmt)
        if "count" in s:
            return _Result(scalar=self._state.get("pending_count", 0))
        if "ingestion_run_unit" in s:
            return _Result(scalar=self._state.get("unit"))
        return _Result()

    async def commit(self):
        self._state["commits"] = self._state.get("commits", 0) + 1

    async def flush(self):
        pass


def _make_run(run_id, stages, batch_ids):
    return SimpleNamespace(
        id=run_id,
        status="running",
        stages=stages,
        sinas_batch_ids=batch_ids,
        done_units=0,
        failed_units=0,
    )


@pytest.mark.asyncio
async def test_oneshot_worker_submits_secondary_without_polling(monkeypatch):
    """The worker itself fires the secondary wave when the last oneshot
    unit completes — no GET required."""
    run_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    unit = SimpleNamespace(status="running", error=None, completed_at=None)
    state = {
        "run": _make_run(
            run_id, ["oneshot", "relationship_extractor"], {"oneshot": "inprocess"}
        ),
        "unit": unit,
        "pending_count": 1,  # the still-pending relationship units
    }
    monkeypatch.setattr(ingestion_runner, "AsyncSessionLocal", lambda: _FakeSession(state))

    async def _noop_pipeline(doc_ids, write=True, **kw):
        return [{"document_id": str(doc_ids[0])}]

    import app.services.entity_resolver as entity_resolver
    import app.services.grounding_gate as grounding_gate
    import app.services.ingestion_oneshot as ingestion_oneshot

    monkeypatch.setattr(ingestion_oneshot, "oneshot_ingest", _noop_pipeline)
    monkeypatch.setattr(grounding_gate, "ground_documents", _noop_pipeline)
    monkeypatch.setattr(entity_resolver, "resolve_unlinked", _noop_pipeline)

    claimed: list[uuid.UUID] = []
    submitted: list[uuid.UUID] = []

    async def _claim(rid):
        claimed.append(rid)
        return True

    async def _submit(rid, client):
        submitted.append(rid)

    monkeypatch.setattr(ingestion_runner, "_claim_secondary_submission", _claim)
    monkeypatch.setattr(ingestion_runner, "_submit_secondary_stages", _submit)

    await _run_oneshot_inprocess(run_id, [doc_id], client=object())

    assert unit.status == "succeeded"
    assert claimed == [run_id]
    assert submitted == [run_id]


@pytest.mark.asyncio
async def test_worker_survives_secondary_submission_failure(monkeypatch):
    """A failed submission must not crash the worker (the progress poll is
    the fallback) — and must release the claim so the fallback can win it."""
    run_id = uuid.uuid4()
    state = {
        "run": _make_run(
            run_id, ["oneshot", "relationship_extractor"], {"oneshot": "inprocess"}
        ),
        "pending_count": 1,
    }
    monkeypatch.setattr(ingestion_runner, "AsyncSessionLocal", lambda: _FakeSession(state))

    released: list[uuid.UUID] = []

    async def _claim(rid):
        return True

    async def _submit(rid, client):
        raise RuntimeError("sinas is down")

    async def _release(rid):
        released.append(rid)

    monkeypatch.setattr(ingestion_runner, "_claim_secondary_submission", _claim)
    monkeypatch.setattr(ingestion_runner, "_submit_secondary_stages", _submit)
    monkeypatch.setattr(ingestion_runner, "_release_secondary_claim", _release)

    # No docs: the pass is trivially complete; only the handoff runs.
    await _run_oneshot_inprocess(run_id, [], client=object())

    assert released == [run_id]


@pytest.mark.asyncio
async def test_maybe_submit_secondary_skips_when_no_dependent_stages(monkeypatch):
    run_id = uuid.uuid4()
    state = {"run": _make_run(run_id, ["oneshot"], {"oneshot": "inprocess"})}
    monkeypatch.setattr(ingestion_runner, "AsyncSessionLocal", lambda: _FakeSession(state))

    async def _claim(rid):  # pragma: no cover — must not be reached
        raise AssertionError("claim attempted for a run without secondary stages")

    monkeypatch.setattr(ingestion_runner, "_claim_secondary_submission", _claim)

    assert await _maybe_submit_secondary(run_id, client=object()) is False


@pytest.mark.asyncio
async def test_maybe_submit_secondary_skips_when_already_submitted(monkeypatch):
    run_id = uuid.uuid4()
    state = {
        "run": _make_run(
            run_id,
            ["oneshot", "relationship_extractor"],
            {"oneshot": "inprocess", "relationship_extractor": "batch-123"},
        )
    }
    monkeypatch.setattr(ingestion_runner, "AsyncSessionLocal", lambda: _FakeSession(state))

    assert await _maybe_submit_secondary(run_id, client=object()) is False


@pytest.mark.asyncio
async def test_maybe_submit_secondary_loses_claim_race(monkeypatch):
    """The claim CAS keeps worker + poll from double-submitting."""
    run_id = uuid.uuid4()
    state = {
        "run": _make_run(
            run_id, ["oneshot", "relationship_extractor"], {"oneshot": "inprocess"}
        )
    }
    monkeypatch.setattr(ingestion_runner, "AsyncSessionLocal", lambda: _FakeSession(state))

    async def _claim(rid):
        return False  # the other caller won

    async def _submit(rid, client):  # pragma: no cover — must not be reached
        raise AssertionError("submitted despite losing the claim race")

    monkeypatch.setattr(ingestion_runner, "_claim_secondary_submission", _claim)
    monkeypatch.setattr(ingestion_runner, "_submit_secondary_stages", _submit)

    assert await _maybe_submit_secondary(run_id, client=object()) is False
