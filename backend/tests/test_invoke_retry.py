"""Unit tests for waiting out a transient upstream failure.

Two runs died on the same day partway through validation, after the retrieval,
the draft and most of the validation rounds had been paid for: $2.69 between
them, both on an Anthropic overload that Sinas surfaced as a 500. Nothing in
the pipeline retried a network failure — the drafter and the gate each retry a
malformed reply, which is a different thing.

The waits are long on purpose. The Anthropic SDK below already retries twice
with sub-second backoff on anything >= 500, so what reaches us has survived
that; a fast retry here would repeat what it just exhausted.

The transport and the two bookkeeping writes are stubbed, so no DB and no
network.

Run from the backend directory:
`python -m pytest tests/test_invoke_retry.py`
"""

import httpx
import pytest
from app.services import query_runner as qr


def status(code: int) -> httpx.HTTPStatusError:
    r = httpx.Response(code, request=httpx.Request("POST", "http://x"))
    return httpx.HTTPStatusError("boom", request=r.request, response=r)


# -- which failures are worth waiting out --------------------------------------


def test_a_500_is_transient():
    """The shape an upstream provider overload arrives in."""
    assert qr._is_transient(status(500))


def test_any_5xx_is_transient():
    assert all(qr._is_transient(status(c)) for c in (500, 502, 503, 529))


def test_a_connection_failure_is_transient():
    """It never carries a judgment about the request."""
    assert qr._is_transient(httpx.ConnectError("refused"))
    assert qr._is_transient(httpx.ReadTimeout("slow"))


def test_a_4xx_is_not():
    """Ours. Retrying hides the defect and pays for it twice."""
    assert not qr._is_transient(status(400))
    assert not qr._is_transient(status(404))


def test_429_is_deliberately_not_transient():
    """The SDK below already honours `retry-after`. A rate limit still hit
    after that wants a smaller run, not a slower one."""
    assert not qr._is_transient(status(429))


def test_an_unrelated_exception_is_not():
    assert not qr._is_transient(ValueError("parse"))


# -- the waits -----------------------------------------------------------------


def test_two_attempts_beyond_the_first():
    assert len(qr.INVOKE_RETRY_WAITS) == 2


def test_the_waits_are_seconds_not_sub_second():
    """Sized past what the SDK already exhausted below. Sub-second here would
    repeat its work and recover nothing."""
    assert all(w >= 1.0 for w in qr.INVOKE_RETRY_WAITS)


def test_the_waits_increase():
    assert list(qr.INVOKE_RETRY_WAITS) == sorted(qr.INVOKE_RETRY_WAITS)


# -- the loop ------------------------------------------------------------------


@pytest.fixture
def transport(monkeypatch):
    """Stub the POST, the sleep and the two bookkeeping writes."""
    state = {"replies": [], "calls": 0, "slept": [], "retry_notes": []}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            i = state["calls"]
            state["calls"] += 1
            out = state["replies"][min(i, len(state["replies"]) - 1)]
            if isinstance(out, Exception):
                raise out
            return out

    async def fake_sleep(s):
        state["slept"].append(s)

    async def fake_record(*a, **k):
        return None

    async def fake_note(run_id, agent, attempts, exc, *, recovered):
        state["retry_notes"].append({"attempts": attempts, "recovered": recovered})

    monkeypatch.setattr(qr.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(qr.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(qr, "record_llm_call", fake_record)
    monkeypatch.setattr(qr, "_tele_invoke_retry", fake_note)
    return state


def ok(reply: str = "done") -> httpx.Response:
    return httpx.Response(200, json={"reply": reply, "chat_id": "c1"},
                          request=httpx.Request("POST", "http://x"))


def boom(code: int = 500) -> httpx.Response:
    return httpx.Response(code, json={}, request=httpx.Request("POST", "http://x"))


@pytest.mark.asyncio
async def test_a_clean_call_does_not_sleep(transport):
    transport["replies"] = [ok()]
    assert await qr._Sinas().invoke("sgr/a", "m") == "done"
    assert transport["calls"] == 1
    assert transport["slept"] == []
    assert transport["retry_notes"] == []


@pytest.mark.asyncio
async def test_a_500_then_success_recovers(transport):
    """The measured case: the run continues instead of dying with the
    retrieval and the draft already paid for."""
    transport["replies"] = [boom(500), ok()]
    assert await qr._Sinas().invoke("sgr/a", "m") == "done"
    assert transport["calls"] == 2
    assert transport["slept"] == [qr.INVOKE_RETRY_WAITS[0]]
    assert transport["retry_notes"] == [{"attempts": 1, "recovered": True}]


@pytest.mark.asyncio
async def test_it_gives_up_after_the_last_wait(transport):
    """`failed`, not `partial`. Partial means the corpus cannot answer, and an
    overload is not that; failed is the resumable state and stays correct."""
    transport["replies"] = [boom(500)]
    with pytest.raises(httpx.HTTPStatusError):
        await qr._Sinas().invoke("sgr/a", "m")
    assert transport["calls"] == 3
    assert transport["slept"] == list(qr.INVOKE_RETRY_WAITS)
    assert transport["retry_notes"] == [{"attempts": 2, "recovered": False}]


@pytest.mark.asyncio
async def test_a_4xx_is_not_retried(transport):
    transport["replies"] = [boom(400)]
    with pytest.raises(httpx.HTTPStatusError):
        await qr._Sinas().invoke("sgr/a", "m")
    assert transport["calls"] == 1
    assert transport["slept"] == []


@pytest.mark.asyncio
async def test_a_connection_error_is_retried(transport):
    transport["replies"] = [httpx.ConnectError("refused"), ok()]
    assert await qr._Sinas().invoke("sgr/a", "m") == "done"
    assert transport["calls"] == 2


@pytest.mark.asyncio
async def test_the_spend_is_recorded_once_on_the_call_that_worked(transport,
                                                                 monkeypatch):
    """`record_llm_call` keys a run's spend by the chat the invoke opened. A
    failed attempt opened none, so only the successful one is booked."""
    booked = []

    async def counting(run_id, chat_id, agent):
        booked.append(chat_id)

    monkeypatch.setattr(qr, "record_llm_call", counting)
    transport["replies"] = [boom(500), ok()]
    await qr._Sinas().invoke("sgr/a", "m")
    assert booked == ["c1"]
