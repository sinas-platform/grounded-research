"""The answer opens with its conclusion.

The instruction used to be that the FINAL claim states the overall
conclusion, and it was followed: across every answer produced, no claim
typed as a conclusion has ever been first, and they sit on average 84% of
the way through. A reader who wants the answer has to read to the end for
it.

Two prompts have to agree or the drafter is asked for one shape and planned
into another, so both are asserted here.

Run from the backend directory:
`python -m pytest tests/test_conclusion_is_first.py`
"""

import ast
import inspect
import textwrap

from app.services import query_runner as qr


def _prompt(fn):
    """The prompt as the model receives it, not as the source wraps it.

    The instruction is spread over adjacent string literals, so a substring
    that reads as one sentence is split by whatever column the line broke at.
    Joining the literals asserts on the sentence rather than on the wrapping.
    """
    src = inspect.getsource(fn)
    return "".join(
        node.value
        for node in ast.walk(ast.parse(textwrap.dedent(src)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_the_drafter_is_asked_for_the_conclusion_first():
    src = _prompt(qr._draft_from_extracts)
    assert "The FIRST claim states the answer to the question" in src
    assert "Do not repeat the conclusion at the end" in src


def test_the_plan_puts_the_conclusion_first_too():
    """The drafter follows the plan. Asking the drafter for claim 1 while the
    plan still puts the conclusion last gives it two instructions and no way
    to satisfy both."""
    src = _prompt(qr._argument_plan)
    assert "the FIRST claim" in src
    assert "no later claim restates" in src


def test_neither_prompt_still_asks_for_a_final_conclusion():
    """The old instruction is the thing being replaced. Leaving it anywhere
    in either prompt is how a run gets told both."""
    for fn in (qr._draft_from_extracts, qr._argument_plan):
        src = _prompt(fn)
        assert "final claim states the overall conclusion" not in src, fn.__name__
