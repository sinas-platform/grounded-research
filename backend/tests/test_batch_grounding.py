"""Batched grounding: the last per-document LLM stage leaves the middle loop.

In batch mode, extract and relationships already ran as provider-batched
passes; grounding still made one rate-limited call per document — alone
worth one to two days of wall-clock at a 100k-document load. These tests
pin the wiring and the transaction discipline, both of which are invisible
to a green run and expensive to get wrong.

Run from the backend directory: `python -m pytest tests/test_batch_grounding.py`
"""

from __future__ import annotations

import ast
import inspect
import textwrap


def test_batch_mode_pulls_grounding_out_of_the_middle_loop():
    from app.services import ingestion_runner as ir

    src = inspect.getsource(ir)
    block = src[src.index('if batch and "ground" in parts'):]
    # grounding is batched, then removed from the per-document parts
    assert "batch_grounding_pass" in block.split("batch_relationships")[0]
    assert 'parts = tuple(p for p in parts if p != "ground")' in \
        block.split("batch_relationships")[0]
    # and it runs BEFORE the relationships batch and the middle loop:
    # hallucinated names must not reach the resolver
    assert src.index("batch_grounding_pass") < src.index("batch_relationships =")
    assert src.index("batch_grounding_pass") < src.index("_middle_one")


def test_grounding_errors_flow_into_the_per_document_error_channel():
    from app.services import ingestion_runner as ir

    src = inspect.getsource(ir)
    block = src[src.index('if batch and "ground" in parts'):
                src.index("batch_relationships =")]
    assert 'extract_errors[did] = f"grounding:' in block


def test_no_session_is_held_across_the_batch_wait():
    """The wave client parks invoke() until the whole provider batch
    settles — minutes to hours. A session opened around that await pins a
    connection for the duration, and a 2000-doc slice would pin 2000. The
    collect/apply split exists so the invoke happens OUTSIDE any session
    block; this pins that shape structurally."""
    from app.services.ingestion_batch import batch_grounding_pass

    fn = ast.parse(textwrap.dedent(inspect.getsource(batch_grounding_pass)))
    invokes_in_session = [
        node
        for outer in ast.walk(fn) if isinstance(outer, ast.AsyncWith)
        for node in ast.walk(outer)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "invoke"
    ]
    assert invokes_in_session == [], (
        "client.invoke awaited inside an AsyncSessionLocal block — a "
        "connection would be pinned for the whole batch turnaround")
