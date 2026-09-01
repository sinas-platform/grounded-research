"""Unit tests for the planner's reply parsing and its one repair attempt.

Pure: `_parse` and `_repair_prompt` take strings and return strings, and
`_invoke_json` takes anything with an `invoke` coroutine, so a stub stands in
for the client. No DB, no network, and no import of the private SDK.

Run from the backend directory: `python -m pytest tests/test_plan_parse.py`
"""

import json
import sys
import types
from contextlib import contextmanager

import pytest
from app.retrieval_first import (
    _ROUND1_FIELDS,
    _ROUND2_FIELDS,
    _invoke_json,
    _parse,
    _repair_prompt,
    _require,
)


class _Stub:
    """Returns each canned reply in turn and records the prompts it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    async def invoke(self, agent, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


# ── _parse ───────────────────────────────────────────────────────────────────


def test_plain_object_parses():
    assert _parse('{"websearch_queries": ["a", "b"]}') == {"websearch_queries": ["a", "b"]}


def test_fenced_reply_parses():
    """Models wrap JSON in a code fence often enough that stripping it is not
    a courtesy but the common path."""
    assert _parse('```json\n{"class_boost": []}\n```') == {"class_boost": []}


def test_prose_around_the_object_is_ignored():
    assert _parse('Here you go:\n{"anchor_entity_ids": ["x"]}\nHope that helps') == {
        "anchor_entity_ids": ["x"]
    }


def test_reply_with_no_object_raises_a_named_error():
    """An empty or prose-only reply used to raise from inside json.loads with
    an error about position 0 of an empty string, which named nothing."""
    with pytest.raises(json.JSONDecodeError) as e:
        _parse("I cannot answer that.")
    assert "no JSON object" in str(e.value)


def test_empty_reply_raises_rather_than_returning_nothing():
    with pytest.raises(json.JSONDecodeError):
        _parse("")


def test_none_reply_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse(None)


def test_malformed_object_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse('{"queries": ["a" "b"]}')


# ── _repair_prompt ───────────────────────────────────────────────────────────


def test_repair_prompt_names_what_broke_and_returns_the_reply():
    """A reviser told only that something is wrong cannot fix it."""
    try:
        _parse('{"a": ["x" "y"]}')
    except json.JSONDecodeError as exc:
        prompt = _repair_prompt(exc, '{"a": ["x" "y"]}', "ORIGINAL")
    assert "could not be used" in prompt
    assert "delimiter" in prompt
    assert '{"a": ["x" "y"]}' in prompt


def test_repair_prompt_survives_an_empty_reply():
    try:
        _parse("")
    except json.JSONDecodeError as exc:
        prompt = _repair_prompt(exc, "", "ORIGINAL")
    assert "PREVIOUS REPLY:" in prompt


# ── _invoke_json ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_reply_costs_one_call():
    stub = _Stub('{"websearch_queries": ["a"]}')
    assert await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS) == {
        "websearch_queries": ["a"]
    }
    assert len(stub.prompts) == 1


@pytest.mark.asyncio
async def test_a_malformed_reply_is_retried_once_and_succeeds():
    stub = _Stub('{"websearch_queries": ["a" "b"]}', '{"websearch_queries": ["a", "b"]}')
    assert await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS) == {
        "websearch_queries": ["a", "b"]
    }
    assert len(stub.prompts) == 2


@pytest.mark.asyncio
async def test_the_retry_carries_the_repair_prompt_not_the_original():
    stub = _Stub("not json at all", '{"websearch_queries": []}')
    await _invoke_json(stub, "agent", "ORIGINAL PROMPT", _ROUND1_FIELDS)
    assert stub.prompts[0] == "ORIGINAL PROMPT"
    assert "could not be used" in stub.prompts[1]
    assert "not json at all" in stub.prompts[1]


@pytest.mark.asyncio
async def test_a_second_failure_raises_rather_than_planning_on_nothing():
    """Two malformed replies is not a transient fault to paper over: an empty
    plan would read downstream as 'the corpus supports nothing'."""
    stub = _Stub("still not json", "and still not")
    with pytest.raises(json.JSONDecodeError):
        await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS)
    assert len(stub.prompts) == 2


@pytest.mark.asyncio
async def test_there_is_no_third_attempt():
    stub = _Stub("bad", "worse", '{"websearch_queries": []}')
    with pytest.raises(json.JSONDecodeError):
        await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS)
    assert len(stub.replies) == 1


# ── _require: the check that makes the retry safe ────────────────────────────


def test_a_field_the_round_asked_for_is_an_answer_even_when_empty():
    """An empty list is the planner reporting it found nothing, which is a
    result. Only a reply engaging with none of the fields has not answered."""
    data = {"websearch_queries": []}
    assert _require(data, _ROUND1_FIELDS) is data


def test_any_one_of_the_round_fields_suffices():
    assert _require({"class_boost": ["x"]}, _ROUND2_FIELDS)


def test_an_empty_object_is_rejected():
    """`{}` parses and would become an empty plan that retrieval turns into an
    empty published answer."""
    with pytest.raises(ValueError, match="answered none of"):
        _require({}, _ROUND1_FIELDS)


def test_an_object_of_unrelated_keys_is_rejected():
    with pytest.raises(ValueError, match="answered none of"):
        _require({"thoughts": "I am not sure"}, _ROUND2_FIELDS)


def test_the_rejection_names_the_fields_and_what_came_back():
    with pytest.raises(ValueError) as e:
        _require({"other": 1}, _ROUND2_FIELDS)
    assert "anchor_entity_ids" in str(e.value)
    assert "other" in str(e.value)


# ── the two failures compose ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_object_is_repaired_rather_than_accepted():
    """The failure this closes: without the check the repair may answer `{}`,
    turning a loud planning failure into a quiet empty result."""
    stub = _Stub("{}", '{"websearch_queries": ["a"]}')
    assert await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS) == {
        "websearch_queries": ["a"]
    }
    assert len(stub.prompts) == 2


@pytest.mark.asyncio
async def test_a_first_attempt_empty_object_is_repaired_not_passed_through():
    """The check runs on the first attempt too, so this narrows a hole that is
    open regardless of the retry."""
    stub = _Stub("{}", "{}")
    with pytest.raises(ValueError, match="answered none of"):
        await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS)
    assert len(stub.prompts) == 2


@pytest.mark.asyncio
async def test_a_repair_that_answers_nothing_raises():
    stub = _Stub("not json", "{}")
    with pytest.raises(ValueError):
        await _invoke_json(stub, "agent", "PROMPT", _ROUND2_FIELDS)


# ── the repair prompt carries the request ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_repair_prompt_carries_the_original_request():
    """A prose-only reply leaves nothing to repair from, so the repair has to
    carry the question or it asks for the correction of nothing."""
    stub = _Stub("I cannot help with that", '{"named_entities": []}')
    await _invoke_json(stub, "agent", "THE ORIGINAL REQUEST", _ROUND1_FIELDS)
    assert "THE ORIGINAL REQUEST" in stub.prompts[1]
    assert "I cannot help with that" in stub.prompts[1]


def test_repair_prompt_includes_both_the_request_and_the_reply():
    prompt = _repair_prompt(ValueError("broke"), "BAD REPLY", "THE REQUEST")
    assert "ORIGINAL REQUEST:" in prompt
    assert "THE REQUEST" in prompt
    assert "PREVIOUS REPLY:" in prompt
    assert "BAD REPLY" in prompt


# ── the repair leaves a record ───────────────────────────────────────────────
#
# `_invoke_json` imports `_tele` from query_runner inside the except branch,
# and query_runner needs the private SDK. A stub module under that name keeps
# these tests importable without it, which is the point of the local import.


@contextmanager
def _capture_tele():
    written = []

    async def fake_tele(run_id, stage, **detail):
        written.append((run_id, stage, detail))

    stub_module = types.ModuleType("app.services.query_runner")
    stub_module._tele = fake_tele
    previous = sys.modules.get("app.services.query_runner")
    sys.modules["app.services.query_runner"] = stub_module
    try:
        yield written
    finally:
        if previous is None:
            del sys.modules["app.services.query_runner"]
        else:
            sys.modules["app.services.query_runner"] = previous


@pytest.mark.asyncio
async def test_a_repair_is_recorded_against_the_run():
    """Without this the only trace is that a round cost two calls instead of
    one, which is an inference rather than a record."""
    with _capture_tele() as written:
        stub = _Stub("{}", '{"websearch_queries": []}')
        await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS, "run-1", "round 1")
    assert len(written) == 1
    run_id, stage, detail = written[0]
    assert (run_id, stage) == ("run-1", "retrieval")
    assert "round 1" in detail["plan_reparse"]
    assert "answered none of" in detail["plan_reparse"]


@pytest.mark.asyncio
async def test_nothing_is_recorded_when_the_first_reply_is_good():
    with _capture_tele() as written:
        stub = _Stub('{"websearch_queries": ["a"]}')
        await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS, "run-1", "round 1")
    assert written == []


@pytest.mark.asyncio
async def test_a_run_without_an_id_still_repairs():
    """The planner is callable outside a run, from the CLI, where there is no
    run to record against."""
    stub = _Stub("{}", '{"websearch_queries": []}')
    assert await _invoke_json(stub, "agent", "PROMPT", _ROUND1_FIELDS) == {"websearch_queries": []}


# ── _require is safe on a non-object ─────────────────────────────────────────
#
# Nothing reaches it today that is not a dict: `_parse` slices from the first
# brace to the last, so it returns an object or raises. These cover the
# function on its own terms rather than relying on that.


def test_a_null_reply_is_rejected_not_a_type_error():
    with pytest.raises(ValueError, match="not a JSON object"):
        _require(None, _ROUND1_FIELDS)


def test_a_number_reply_is_rejected_not_a_type_error():
    with pytest.raises(ValueError, match="not a JSON object"):
        _require(7, _ROUND1_FIELDS)


def test_an_array_reply_is_rejected_not_a_type_error():
    with pytest.raises(ValueError, match="not a JSON object"):
        _require([{"named_entities": []}], _ROUND1_FIELDS)


def test_the_rejection_names_the_type_that_came_back():
    with pytest.raises(ValueError) as e:
        _require([], _ROUND2_FIELDS)
    assert "list" in str(e.value)
