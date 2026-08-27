"""Every stage records when it started, when it ended and how long it took.

A measured 52-minute run accounted for only 932s of model latency; roughly
2,800s sat between logged calls and nothing persisted said which stage was
holding it. Stages recorded timings inconsistently — `draft` had
started/completed, `retrieval` only completed, `extract` neither — so the
telemetry could not answer "where did the wall clock go".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services import query_runner

SOURCE = Path(query_runner.__file__).read_text()


def _tele_calls_for(stage: str) -> list[ast.Call]:
    """Every _tele(..., "<stage>", ...) call in the module."""
    tree = ast.parse(SOURCE)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_tele"):
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            if node.args[1].value == stage:
                found.append(node)
    return found


def _keywords(calls: list[ast.Call]) -> set[str]:
    return {kw.arg for call in calls for kw in call.keywords if kw.arg}


@pytest.mark.parametrize("stage", ["retrieval", "extract", "draft", "validate"])
def test_each_stage_records_a_start(stage: str) -> None:
    calls = _tele_calls_for(stage)
    assert calls, f"no telemetry written for stage {stage!r}"
    assert "started" in _keywords(calls), f"{stage} never records when it started"


@pytest.mark.parametrize("stage", ["retrieval", "extract", "draft"])
def test_each_stage_records_completion(stage: str) -> None:
    assert "completed" in _keywords(_tele_calls_for(stage)), (
        f"{stage} never records when it finished"
    )


def test_extract_records_its_duration() -> None:
    # The stage most likely to hold unexplained wall clock: it reads whole
    # documents and verifies every quote against them.
    assert "elapsed_s" in _keywords(_tele_calls_for("extract"))


def test_the_stage_timer_records_elapsed_even_on_failure() -> None:
    # A stage that dies slowly is exactly the one worth timing, so the
    # completion write belongs in a finally block.
    start = SOURCE.index("async def _timed(")
    body = SOURCE[start : SOURCE.index("\ndef ", start)]
    assert "finally:" in body
    assert "elapsed_s" in body.split("finally:")[1]
