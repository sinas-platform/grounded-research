"""Bulk pipeline: an unreachable dependency is a wait, not a failure.

Three manual restarts in two days (14 Sep overnight, 16 Sep morning and
evening) had one shape: a round in flight lost the provider (and its database
connections), spent its six submit attempts inside the outage, and raised.
These tests hold the line on the four things that fix must keep true — the
wait, the fast failure for a real refusal, the untouched checkpoint, and one
log line per outage — plus the pacing that stops causing the burst.
"""

import asyncio
import json
import logging

import httpx
import pytest
from app import bulk_pipeline as bp
from sqlalchemy.exc import OperationalError

# The failure as it actually arrives: the platform runs the provider call
# itself and wraps its own name-resolution failure as a 400.
PROVIDER_400 = json.dumps({
    "detail": "Provider batch submission failed: "
              "[Errno -5] No address associated with hostname"})
BAD_REQUEST_400 = json.dumps({"detail": "inputs must not be empty"})


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    """No real waiting, and no outage state leaking between tests."""
    bp._OPEN_OUTAGES.clear()
    monkeypatch.setattr(bp, "OUTAGE_BACKOFF_START", 0.01)
    monkeypatch.setattr(bp, "OUTAGE_BACKOFF_MAX", 0.01)
    monkeypatch.setattr(bp, "SUBMIT_STAGGER_SECONDS", 0.0)
    monkeypatch.setattr(bp, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(bp, "SUBMIT_MAX", 1)
    yield
    bp._OPEN_OUTAGES.clear()


def _client(tmp_path, posts):
    """A BatchClient whose three HTTP calls are stubs. `posts` is a list of
    callables — one per submission attempt — each returning a payload or
    raising."""
    c = bp.BatchClient(tmp_path)
    calls = {"post": 0}

    async def post(agent, round_key, chunk):
        i = calls["post"]
        calls["post"] += 1
        return posts[min(i, len(posts) - 1)]()

    async def get_batch(batch_id):
        return {"completed": 1, "failed": 0, "cancelled": 0, "total": 1,
                "status": "completed"}

    async def get_chat(chat_id):
        return [{"role": "assistant", "content": f"reply-for-{chat_id}"}]

    c._post_batch = post
    c._get_batch = get_batch
    c._get_chat = get_chat
    return c, calls


def _ok(n: int):
    return lambda: {"batch_id": f"batch-{n}", "chat_ids": [f"chat-{n}"]}


def _paused_lines(caplog):
    return [r for r in caplog.records if "PAUSED" in r.getMessage()]


def _back_lines(caplog):
    return [r for r in caplog.records if "reachable again" in r.getMessage()]


# ── what the failure looks like ─────────────────────────────────────────────
def test_transport_cause_in_a_400_is_told_from_a_bad_request():
    assert bp.response_outage(400, PROVIDER_400) == "provider"
    assert bp.response_outage(400, BAD_REQUEST_400) is None
    assert bp.response_outage(422, json.dumps({"detail": "bad field"})) is None
    # a gateway that cannot reach upstream needs no body at all
    assert bp.response_outage(503, "") == "provider"
    # glibc and BSD spell the same failure differently; both are transport
    assert bp.response_outage(400, json.dumps(
        {"detail": "[Errno 111] Connection refused"})) == "provider"
    assert bp.response_outage(400, json.dumps(
        {"detail": "nodename nor servname provided, or not known"})) == "provider"


def test_database_connection_loss_is_told_from_a_bad_statement():
    import asyncpg.exceptions as ae

    lost = OperationalError("SELECT 1", {}, ae.ConnectionDoesNotExistError(
        "connection was closed in the middle of operation"))
    assert bp.classify_outage(lost) == "database"

    refused = OperationalError("SELECT 1", {}, ConnectionRefusedError(
        61, "Connection refused"))
    assert bp.classify_outage(refused) == "database"

    # a real query failure is a failure, whatever wrapper it arrives in
    bad_sql = OperationalError("SELECT 1", {}, ae.UndefinedTableError(
        'relation "nope" does not exist'))
    assert bp.classify_outage(bad_sql) is None


def test_a_whole_group_failing_on_transport_is_the_database():
    """Persist reports carry failures as text. A group that failed to a man
    is an outage; one document failing that way is one failed document."""
    lost = ("(asyncpg.exceptions.ConnectionDoesNotExistError) connection was "
            "closed in the middle of operation")
    assert bp.group_outage([lost] * 25, 25)
    assert not bp.group_outage([lost], 25)
    assert not bp.group_outage([lost] * 24 + ["no stored reply for chunk"], 25)
    assert not bp.group_outage([], 25)


def test_a_post_that_may_already_have_been_accepted_is_not_an_outage():
    """The no-double-submission rule. A connect failure never reached the
    platform; a read failure may have, so it stays on the ordinary budget."""
    assert bp.classify_outage(httpx.ConnectError("no address"),
                              pre_send_only=True) == "provider"
    assert bp.classify_outage(httpx.ReadTimeout("slow"),
                              pre_send_only=True) is None
    assert bp.classify_outage(httpx.ReadTimeout("slow")) == "provider"


# ── the wait ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unreachable_provider_pauses_then_succeeds(tmp_path, caplog):
    attempts = [
        lambda: (_ for _ in ()).throw(httpx.ConnectError(
            "[Errno -5] No address associated with hostname")),
        lambda: (_ for _ in ()).throw(bp.DependencyDown(
            "provider", "submit rejected (400): " + PROVIDER_400)),
        _ok(0),
    ]
    client, calls = _client(tmp_path, attempts)
    with caplog.at_level(logging.INFO, logger="bulk"):
        replies = await client.run_round("extract-front", "agent", ["p0"])

    assert replies == ["reply-for-chat-0"]
    assert calls["post"] == 3            # two outage retries, then through
    # the outage spent none of the six submit attempts
    assert not [r for r in caplog.records
                if "submit attempt" in r.getMessage()]
    assert len(_paused_lines(caplog)) == 1
    assert len(_back_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_lost_database_connection_pauses_then_recovers(caplog):
    import asyncpg.exceptions as ae

    seen = {"n": 0}

    async def op():
        seen["n"] += 1
        if seen["n"] < 3:
            raise OperationalError("UPDATE mention", {},
                                   ae.ConnectionDoesNotExistError(
                                       "connection was closed in the middle "
                                       "of operation"))
        return "applied"

    with caplog.at_level(logging.INFO, logger="bulk"):
        assert await bp.await_dependency("ground-apply doc-1", op) == "applied"
    assert seen["n"] == 3
    paused = _paused_lines(caplog)
    assert len(paused) == 1
    assert "DATABASE" in paused[0].getMessage()
    assert "not failed" in paused[0].getMessage()
    assert len(_back_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_one_log_line_per_outage_however_many_callers_hit_it(caplog):
    state = {"down": True}

    async def op():
        if state["down"]:
            raise httpx.ConnectError("[Errno -5] No address associated")
        return "ok"

    async def unblock():
        await asyncio.sleep(0.05)
        state["down"] = False

    with caplog.at_level(logging.INFO, logger="bulk"):
        results = await asyncio.gather(
            unblock(),
            *(bp.await_dependency(f"fetch chat-{i}", op) for i in range(25)))

    assert results[1:] == ["ok"] * 25
    assert len(_paused_lines(caplog)) == 1
    assert len(_back_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_the_cap_is_honoured(caplog):
    async def op():
        raise httpx.ConnectError("[Errno -5] No address associated")

    with caplog.at_level(logging.INFO, logger="bulk"):
        with pytest.raises(bp.DependencyDown) as err:
            await bp.await_dependency("extract-front submit chunk 0", op,
                                      cap=0.05)

    assert err.value.dependency == "provider"
    assert isinstance(err.value.__cause__, httpx.ConnectError)
    assert len(_paused_lines(caplog)) == 1
    assert [r for r in caplog.records if "giving up" in r.getMessage()]
    # the outage is closed out, so a later one still logs its own line
    assert not bp._OPEN_OUTAGES


# ── the refusal ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_genuine_400_fails_fast(tmp_path, caplog, monkeypatch):
    """Neither mechanism absorbs a bad request: no wait, no six attempts."""
    posts = {"n": 0}

    class _Response:
        status_code = 400
        text = BAD_REQUEST_400
        is_error = True

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            posts["n"] += 1
            return _Response()

    monkeypatch.setattr(bp.httpx, "AsyncClient", _FakeAsyncClient)
    slept: list[float] = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bp.asyncio, "sleep",
                        lambda d: (slept.append(d), real_sleep(0))[1])

    client = bp.BatchClient(tmp_path)
    with caplog.at_level(logging.INFO, logger="bulk"):
        with pytest.raises(bp.SubmitRejected) as err:
            await client.run_round("extract-front", "agent", ["p0"])

    assert "submit rejected (400)" in str(err.value)
    assert posts["n"] == 1               # no retry, no pause
    assert not _paused_lines(caplog)
    assert not [d for d in slept if d >= 60]
    assert not (tmp_path / "batches.json").exists()


# ── the checkpoint ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_pause_never_resubmits_and_leaves_the_checkpoint_alone(
        tmp_path):
    attempts = [
        lambda: (_ for _ in ()).throw(httpx.ConnectError("no address")),
        _ok(0),
        lambda: pytest.fail("a resumed round must not resubmit"),
    ]
    client, calls = _client(tmp_path, attempts)
    await client.run_round("extract-front", "agent", ["p0"])

    state = json.loads((tmp_path / "batches.json").read_text())
    assert state["extract-front"]["chunks"] == [
        {"batch_id": "batch-0", "chat_ids": ["chat-0"], "n": 1}]
    assert calls["post"] == 2            # the pause cost a retry, not a batch

    # a fresh client over the same job dir resumes by polling, not resubmitting
    resumed, resumed_calls = _client(tmp_path, attempts)
    replies = await resumed.run_round("extract-front", "agent", ["p0"])
    assert replies == ["reply-for-chat-0"]
    assert resumed_calls["post"] == 0
    assert json.loads((tmp_path / "batches.json").read_text()) == state


# ── the burst ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_submissions_are_spaced_and_never_overlap(tmp_path,
                                                        monkeypatch):
    """A round of many chunks must not fire many simultaneous uploads: that
    burst of name resolutions is what fails inside the container."""
    monkeypatch.setattr(bp, "SUBMIT_STAGGER_SECONDS", 0.05)
    starts: list[float] = []
    ends: list[float] = []

    async def post(agent, round_key, chunk):
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.01)
        ends.append(asyncio.get_running_loop().time())
        return {"batch_id": f"b{len(starts)}", "chat_ids": [f"c{len(starts)}"]}

    client, _ = _client(tmp_path, [_ok(0)])
    client._post_batch = post
    await client.run_round("extract-front", "agent", ["p0", "p1", "p2"])

    assert len(starts) == 3
    for i in range(1, 3):
        assert starts[i] >= ends[i - 1], "submissions overlapped"
        assert starts[i] - ends[i - 1] >= 0.04, "submissions were not spaced"
