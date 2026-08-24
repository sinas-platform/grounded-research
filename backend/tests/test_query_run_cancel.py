"""Cancellation of a query run: the flag, the checkpoint, the terminal mark.

These exercise the pieces without a live pipeline — starting a real run costs
money, and none of the behaviour worth pinning here needs one.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.models.query import QUERY_RUN_STATUSES
from app.services import query_runner as qr


class _Run:
    """The two fields the cancel helpers read off a QueryRun."""

    def __init__(self, telemetry: dict | None) -> None:
        self.telemetry = telemetry


class _Session:
    """An AsyncSessionLocal stand-in returning one canned run."""

    def __init__(self, run: _Run) -> None:
        self._run = run

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, _model: object, _pk: object) -> _Run:
        return self._run


@pytest.fixture
def run_id() -> uuid.UUID:
    return uuid.uuid4()


def _with_telemetry(monkeypatch: pytest.MonkeyPatch, telemetry: dict | None) -> None:
    monkeypatch.setattr(
        qr, "AsyncSessionLocal", lambda: _Session(_Run(telemetry)), raising=True
    )


def test_cancelled_is_a_terminal_status_the_model_knows() -> None:
    """A status the pipeline can write must be one the column accepts."""
    assert "cancelled" in QUERY_RUN_STATUSES


def test_cancellation_is_not_an_asyncio_cancellation() -> None:
    """The outcome must be catchable by `except Exception`.

    `asyncio.CancelledError` is a BaseException, so it would bypass
    `run_pipeline`'s handler and strand the run in its in-flight status with
    its chats never torn down. This is the guard against someone "simplifying"
    it back to that.
    """
    assert issubclass(qr.CancelledOutcome, Exception)
    assert not issubclass(qr.CancelledOutcome, asyncio.CancelledError)

    # And concretely: an `except Exception` ladder catches it.
    try:
        raise qr.CancelledOutcome(requested_by="u-1")
    except Exception as exc:  # noqa: BLE001 - that is precisely the assertion
        assert isinstance(exc, qr.CancelledOutcome)


@pytest.mark.anyio
async def test_no_telemetry_means_no_cancel_pending(
    monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    """The overwhelmingly common case: nobody asked, nothing raises."""
    _with_telemetry(monkeypatch, None)

    assert await qr._cancel_requested(run_id) is False
    await qr._check_cancel(run_id)  # must not raise


@pytest.mark.anyio
async def test_an_unrelated_telemetry_entry_does_not_cancel(
    monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    """Only the cancel entry cancels — a stage entry must never trip it."""
    _with_telemetry(monkeypatch, {"retrieval": {"completed": "2026-08-20T10:00:00Z"}})

    assert await qr._cancel_requested(run_id) is False


@pytest.mark.anyio
async def test_a_malformed_cancel_entry_does_not_cancel(
    monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    """Telemetry is free-form JSON; a scalar there must not stop a paid run."""
    _with_telemetry(monkeypatch, {"cancel": "yes please"})

    assert await qr._cancel_requested(run_id) is False


@pytest.mark.anyio
async def test_a_recorded_request_trips_the_checkpoint(
    monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    """The whole point: a recorded request stops the next unit of work."""
    _with_telemetry(monkeypatch, {"cancel": {"requested": True, "requested_by": "u-1"}})

    assert await qr._cancel_requested(run_id) is True
    with pytest.raises(qr.CancelledOutcome) as excinfo:
        await qr._check_cancel(run_id)
    assert excinfo.value.requested_by == "u-1"


@pytest.mark.anyio
async def test_a_withdrawn_request_does_not_trip(
    monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    """`requested: false` is not a cancel — the key's presence is not the flag."""
    _with_telemetry(monkeypatch, {"cancel": {"requested": False}})

    assert await qr._cancel_requested(run_id) is False


@pytest.mark.anyio
async def test_marking_cancelled_records_the_fact_and_clears_no_error(
    monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID
) -> None:
    """Terminal mark: status cancelled, error null — nothing went wrong."""
    marks: dict[str, object] = {}
    teles: list[tuple[str, dict]] = []

    async def _fake_mark(_rid: uuid.UUID, **fields: object) -> None:
        marks.update(fields)

    async def _fake_tele(_rid: uuid.UUID, stage: str, **detail: object) -> None:
        teles.append((stage, dict(detail)))

    monkeypatch.setattr(qr, "_mark", _fake_mark, raising=True)
    monkeypatch.setattr(qr, "_tele", _fake_tele, raising=True)

    await qr._mark_cancelled(run_id, qr.CancelledOutcome(requested_by="u-1"))

    assert marks["status"] == "cancelled"
    assert marks["error"] is None
    assert marks["completed_at"] is not None
    stage, detail = teles[0]
    assert stage == "cancel"
    assert detail["requested_by"] == "u-1"
    assert detail["message"]
