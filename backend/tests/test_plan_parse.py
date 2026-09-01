"""Unit tests for the planner's reply parsing and its one repair attempt.

Pure: `_parse` and `_repair_prompt` take strings and return strings, and
`_invoke_json` takes anything with an `invoke` coroutine, so a stub stands in
for the client. No DB, no network, and no import of the private SDK.

Run from the backend directory: `python -m pytest tests/test_plan_parse.py`
"""

import json

import pytest
from app.retrieval_first import _invoke_json, _parse, _repair_prompt


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
        prompt = _repair_prompt(exc, '{"a": ["x" "y"]}')
    assert "not valid JSON" in prompt
    assert "delimiter" in prompt
    assert '{"a": ["x" "y"]}' in prompt


def test_repair_prompt_survives_an_empty_reply():
    try:
        _parse("")
    except json.JSONDecodeError as exc:
        prompt = _repair_prompt(exc, "")
    assert "PREVIOUS REPLY:" in prompt


# ── _invoke_json ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_reply_costs_one_call():
    stub = _Stub('{"websearch_queries": ["a"]}')
    assert await _invoke_json(stub, "agent", "PROMPT") == {"websearch_queries": ["a"]}
    assert len(stub.prompts) == 1


@pytest.mark.asyncio
async def test_a_malformed_reply_is_retried_once_and_succeeds():
    stub = _Stub('{"queries": ["a" "b"]}', '{"queries": ["a", "b"]}')
    assert await _invoke_json(stub, "agent", "PROMPT") == {"queries": ["a", "b"]}
    assert len(stub.prompts) == 2


@pytest.mark.asyncio
async def test_the_retry_carries_the_repair_prompt_not_the_original():
    stub = _Stub("not json at all", '{"queries": []}')
    await _invoke_json(stub, "agent", "ORIGINAL PROMPT")
    assert stub.prompts[0] == "ORIGINAL PROMPT"
    assert "not valid JSON" in stub.prompts[1]
    assert "not json at all" in stub.prompts[1]


@pytest.mark.asyncio
async def test_a_second_failure_raises_rather_than_planning_on_nothing():
    """Two malformed replies is not a transient fault to paper over: an empty
    plan would read downstream as 'the corpus supports nothing'."""
    stub = _Stub("still not json", "and still not")
    with pytest.raises(json.JSONDecodeError):
        await _invoke_json(stub, "agent", "PROMPT")
    assert len(stub.prompts) == 2


@pytest.mark.asyncio
async def test_there_is_no_third_attempt():
    stub = _Stub("bad", "worse", '{"queries": []}')
    with pytest.raises(json.JSONDecodeError):
        await _invoke_json(stub, "agent", "PROMPT")
    assert len(stub.replies) == 1
