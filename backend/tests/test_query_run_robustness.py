"""Unit tests for query-run robustness: failed-run chat teardown and the
chat-based decompose stage.

Pure helpers plus a fake sinas client; no DB, no network — the wiring into
run_pipeline is exercised against the real stack elsewhere, same convention
as the other runner tests.

Run from the backend directory: `python -m pytest tests/test_query_run_robustness.py`
"""

import pytest
from app.services.query_runner import (
    _await_reply,
    _chat_ids_for_cleanup,
    _parse_subqueries,
    _teardown_chats,
)


class _FakeSinas:
    def __init__(self, message_batches=None, fail_deletes=()):
        self._batches = list(message_batches or [])
        self.deleted = []
        self._fail_deletes = set(fail_deletes)

    async def chat_delete(self, chat_id):
        if chat_id in self._fail_deletes:
            raise RuntimeError("delete failed")
        self.deleted.append(chat_id)

    async def chat_messages(self, chat_id):
        if not self._batches:
            return []
        if len(self._batches) == 1:
            return self._batches[0]
        return self._batches.pop(0)


# ── _chat_ids_for_cleanup ────────────────────────────────────────────────────


def test_cleanup_ids_collect_from_telemetry_and_searches():
    telemetry = {
        "decompose": {"chat_id": "c-dec", "completed": "t"},
        "draft": {"chat_id": "c-draft", "claims": 12},
        "discovery": {"chat_id": "c-disc"},
        "validate": {"round_1": {"passed": 3}},  # no chat_id: ignored
        "search": {"started": "t"},
    }
    searches = {
        "sub one": {"chat_id": "c-s1", "nudges": 0},
        "sub two": {"chat_id": "c-s2"},
    }
    assert _chat_ids_for_cleanup(telemetry, searches) == [
        "c-dec",
        "c-draft",
        "c-disc",
        "c-s1",
        "c-s2",
    ]


def test_cleanup_ids_dedupe_and_tolerate_missing_state():
    assert _chat_ids_for_cleanup(None, None) == []
    telemetry = {"draft": {"chat_id": "c-1"}, "retry": {"chat_id": "c-1"}}
    assert _chat_ids_for_cleanup(telemetry, {"q": {"chat_id": "c-1"}}) == ["c-1"]
    # malformed entries never raise
    assert _chat_ids_for_cleanup({"x": 3, "y": {"chat_id": 7}}, {"q": "nope"}) == []


# ── _teardown_chats ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_teardown_deletes_every_chat():
    sinas = _FakeSinas()
    await _teardown_chats(sinas, ["a", "b", "c"])
    assert sinas.deleted == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_teardown_survives_a_failing_delete():
    sinas = _FakeSinas(fail_deletes={"b"})
    await _teardown_chats(sinas, ["a", "b", "c"])  # must not raise
    assert sinas.deleted == ["a", "c"]


# ── _parse_subqueries ────────────────────────────────────────────────────────


def test_parse_accepts_plain_and_fenced_json_arrays():
    assert _parse_subqueries('["a", "b"]', "q?", 3) == (["a", "b"], True)
    assert _parse_subqueries('```json\n["a"]\n```', "q?", 3) == (["a"], True)


def test_parse_caps_at_max_fanout():
    subs, ok = _parse_subqueries('["a", "b", "c"]', "q?", 2)
    assert ok and subs == ["a", "b"]


def test_parse_falls_back_to_the_question():
    assert _parse_subqueries("I'll search first...", "q?", 2) == (["q?"], False)
    assert _parse_subqueries('{"not": "a list"}', "q?", 2) == (["q?"], False)
    assert _parse_subqueries("[]", "q?", 2) == (["q?"], False)


# ── _await_reply ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_await_reply_returns_first_assistant_content():
    sinas = _FakeSinas(
        message_batches=[
            [],  # first poll: nothing yet
            [
                {"role": "user", "content": "decompose this"},
                {"role": "assistant", "content": '["a"]'},
            ],
        ]
    )
    out = await _await_reply(sinas, "c", window_s=1.0, poll_s=0.01)
    assert out == '["a"]'


@pytest.mark.asyncio
async def test_await_reply_times_out_to_none():
    sinas = _FakeSinas(message_batches=[[{"role": "tool", "content": "{}"}]])
    out = await _await_reply(sinas, "c", window_s=0.05, poll_s=0.01)
    assert out is None
