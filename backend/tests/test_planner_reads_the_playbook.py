"""The planner is held to the rules the drafter is held to.

The drafter receives the deployment's house rules and is told to skip a
passage group that establishes nothing usable. The argument planner received
nothing, so it could plan claims the drafter would then correctly decline --
and each one costs four extraction calls to read documents for a claim that
cannot be written.

Measured over 28 runs that record extraction: of the planned claims whose
anchor documents reached no published claim, 23% read as commentary about the
literature rather than propositions of law, against 2% of the claims the
answer used.
"""

from __future__ import annotations

import inspect

import pytest
import re

from app.services import query_runner as qr


def test_the_planner_is_given_the_house_rules():
    src = inspect.getsource(qr._argument_plan)
    assert "_synthesis_playbook" in src, (
        "the argument planner designs claims the drafter must execute, and was "
        "planning against no rules at all")


def test_the_rules_reach_the_planner_before_the_question():
    """Order matters only in that the rules must be inside the prompt, not
    appended after the documents where a long manifest can push them out."""
    src = inspect.getsource(qr._argument_plan)
    i_rules = src.index("_synthesis_playbook")
    i_docs = src.index("DOCUMENTS:")
    assert i_rules < i_docs, "rules must precede the document list"


def test_the_playbook_says_which_step_it_is_heading():
    """One playbook, two readers. A header naming the drafter in front of a
    planning prompt is the kind of thing that reads as correct and is not."""
    sig = inspect.signature(qr._synthesis_playbook)
    assert "role" in sig.parameters
    assert sig.parameters["role"].default == "drafting", (
        "the existing callers must keep the wording they had")


def test_every_caller_names_a_role_or_takes_the_default():
    src = inspect.getsource(qr)
    calls = re.findall(r"_synthesis_playbook\(([^)]*)\)", src)
    calls = [c for c in calls if "role" not in c or "=" not in c]
    assert calls, "expected callers"
    for c in calls:
        assert c == "" or c.startswith('"'), f"unexpected call shape: {c!r}"


@pytest.mark.asyncio
async def test_an_unreadable_playbook_costs_the_rules_and_nothing_else(monkeypatch, caplog):
    """Argument planning promises to fail open. Adding a database read to it
    made a style guide able to kill a run, which is a worse failure than the
    one it fixes."""
    import logging

    def boom(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(qr, "AsyncSessionLocal", boom)
    with caplog.at_level(logging.ERROR):
        assert await qr._synthesis_playbook("planning the argument") == ""
    assert any("playbook unreadable" in r.message for r in caplog.records), (
        "an unreadable playbook and an absent one return the same empty "
        "string; only the log tells them apart")

