"""Unit tests for query-run robustness: failed-run chat teardown.

Pure helpers plus a fake sinas client; no DB, no network — the wiring into
run_pipeline is exercised against the real stack elsewhere, same convention
as the other runner tests.

Run from the backend directory: `python -m pytest tests/test_query_run_robustness.py`
"""

import pytest
from app.services.query_runner import (
    _chat_ids_for_cleanup,
    _teardown_chats,
)


class _FakeSinas:
    def __init__(self, fail_deletes=()):
        self.deleted = []
        self._fail_deletes = set(fail_deletes)

    async def chat_delete(self, chat_id):
        if chat_id in self._fail_deletes:
            raise RuntimeError("delete failed")
        self.deleted.append(chat_id)


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
