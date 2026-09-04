"""Unit tests for a cancel surviving the generic exception handlers.

`CancelledOutcome` is control flow, not a failure: it is raised at checkpoints
and is meant to reach `run_pipeline`, which records the run as `cancelled` and
tears down its sinas chats. Any `except Exception` sitting over a call that can
reach an invoke will catch it first and turn a stopped run into something else.

This is the third time that shape has been fixed in this file, so the last test
here is a sweep rather than another example: it walks the module and fails on a
new unguarded handler, which is the only way to stop a fourth.

No DB and no network: the transport is a stub.

Run from the backend directory:
`python -m pytest tests/test_cancellation_propagation.py`
"""

import ast
import inspect
import json
import pathlib
import uuid

import httpx
import pytest
from app.services import query_runner as qr
from app.services.query_runner import CancelledOutcome


class FakeSinas:
    """A `_Sinas` whose invoke does whatever the test needs."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def invoke(self, agent: str, message: str) -> str:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def _no_tele(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(qr, "_tele", noop)


# -- _argument_plan ------------------------------------------------------------
#
# The catch here turns anything that is not a JSON problem into
# RuntimeError("argument planning failed"), which `run_pipeline` records as
# `failed` with that text as the run's error. A run somebody stopped read, to
# anyone looking at it later, as a run whose planner broke.


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_tele")
async def test_a_cancel_during_planning_travels_out():
    sinas = FakeSinas(CancelledOutcome(requested_by="lumi"))
    with pytest.raises(CancelledOutcome) as e:
        await qr._argument_plan(sinas, uuid.uuid4(), "q", "manifest")
    assert e.value.requested_by == "lumi"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_tele")
async def test_reaching_the_planner_failing_is_still_a_run_failure():
    """The reason the generic catch exists, and it has to keep working: an
    empty plan would otherwise read as "the corpus supports no claims", a
    verdict the run then reports as its outcome."""
    sinas = FakeSinas(httpx.ConnectError("refused"))
    with pytest.raises(RuntimeError, match="argument planning failed"):
        await qr._argument_plan(sinas, uuid.uuid4(), "q", "manifest")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_tele")
async def test_an_unparseable_plan_is_still_fail_open():
    sinas = FakeSinas("not json at all")
    assert await qr._argument_plan(sinas, uuid.uuid4(), "q", "m") == ("", [])


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_tele")
async def test_an_empty_plan_returns_both_halves():
    """A planner replying `{"claims": []}` used to return a bare string, and
    the caller unpacks into two names, so the run died on a ValueError and was
    recorded as failed. The caller's own handling of an empty plan, which
    treats it as a judgment about the corpus, could never be reached this way.
    """
    sinas = FakeSinas(json.dumps({"claims": []}))
    assert await qr._argument_plan(sinas, uuid.uuid4(), "q", "m") == ("", [])


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_tele")
async def test_every_exit_returns_a_pair():
    """What the ValueError above was really about. Pinned across all three
    non-raising exits so a fourth cannot go back to a bare string."""
    for outcome in ("not json", json.dumps({"claims": []}),
                    json.dumps({"claims": [{"n": 1, "establishes": "x",
                                            "anchors": ["a.md"]}]})):
        got = await qr._argument_plan(FakeSinas(outcome), uuid.uuid4(), "q", "m")
        assert isinstance(got, tuple) and len(got) == 2, outcome
        assert isinstance(got[0], str) and isinstance(got[1], list)


# -- _mark_partial: the one that must NOT re-raise -----------------------------


def test_the_partial_note_swallows_a_cancel_on_purpose():
    """The opposite of every other case here, for a structural reason.

    `_mark_partial` is called from inside `run_pipeline`'s
    `except PartialOutcome` handler. An exception raised there does not reach
    the sibling `except CancelledOutcome`, because sibling handlers do not
    catch each other, so it would leave `run_pipeline` with the run row never
    marked and the run stranded in its in-flight status. That is the exact
    failure `CancelledOutcome`'s docstring exists to avoid.

    Asserted on the source because the alternative is standing up the whole
    partial path; what matters is that the handler exists, is explicit, and
    contains no raise.
    """
    handler = cancel_handler_in("_mark_partial")
    assert handler is not None, "the cancel must be caught by name, not by accident"
    assert not [x for x in ast.walk(handler) if isinstance(x, ast.Raise)]


def test_the_planner_does_re_raise():
    """The same shape, opposite answer, so the contrast is pinned too."""
    handler = cancel_handler_in("_argument_plan")
    assert handler is not None
    assert [x for x in ast.walk(handler) if isinstance(x, ast.Raise)]


# -- the sweep -----------------------------------------------------------------
#
# Three instances of one shape is a pattern, not three accidents. This walks
# every generic handler in the module, works out which of them sit over a call
# that can reach an invoke, and fails on any that would swallow a cancel.


def module_tree() -> ast.Module:
    return ast.parse(pathlib.Path(inspect.getfile(qr)).read_text(encoding="utf-8"))


def functions_in(tree):
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def called_names(node) -> set[str]:
    out = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            f = x.func
            out.add(f.attr if isinstance(f, ast.Attribute)
                    else (f.id if isinstance(f, ast.Name) else ""))
    return out


def cancellable(tree) -> set[str]:
    """Every function that can propagate a cancel, by transitive closure over
    the call graph. `_check_cancel` raises it; `invoke` calls that."""
    out = {"_check_cancel", "invoke"}
    funcs = functions_in(tree)
    changed = True
    while changed:
        changed = False
        for name, node in funcs.items():
            if name not in out and called_names(node) & out:
                out.add(name)
                changed = True
    return out


def cancel_handler_in(func_name: str):
    """The `except CancelledOutcome` handler inside one function, if any."""
    tree = module_tree()
    fn = functions_in(tree).get(func_name)
    assert fn is not None, func_name
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for h in node.handlers:
                if h.type is not None and "CancelledOutcome" in ast.unparse(h.type):
                    return h
    return None


def test_no_generic_handler_swallows_a_cancel():
    tree = module_tree()
    risky = cancellable(tree)
    unguarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not called_names(ast.Module(body=node.body, type_ignores=[])) & risky:
            continue
        for i, h in enumerate(node.handlers):
            kind = "bare" if h.type is None else ast.unparse(h.type)
            if kind not in ("bare", "Exception", "BaseException"):
                continue
            guarded = any(
                hh.type is not None and "CancelledOutcome" in ast.unparse(hh.type)
                for hh in node.handlers[:i])
            if not guarded:
                unguarded.append(f"line {h.lineno}: except {kind}")
    assert not unguarded, (
        "a generic handler sits over a call that can raise CancelledOutcome "
        "without catching it first, which turns a stopped run into something "
        "else. Catch it by name above the generic handler and decide "
        "deliberately whether to re-raise:\n  " + "\n  ".join(unguarded))


def test_the_sweep_would_actually_catch_one():
    """The sweep is only worth having if it fails on the thing it looks for.
    Same walk over a module built to contain the defect."""
    tree = ast.parse(
        "async def _check_cancel(r):\n"
        "    raise CancelledOutcome()\n"
        "async def helper(r):\n"
        "    await _check_cancel(r)\n"
        "async def caller(r):\n"
        "    try:\n"
        "        await helper(r)\n"
        "    except Exception:\n"
        "        pass\n")
    assert "helper" in cancellable(tree)
    risky = cancellable(tree)
    found = [h.lineno for n in ast.walk(tree) if isinstance(n, ast.Try)
             if called_names(ast.Module(body=n.body, type_ignores=[])) & risky
             for h in n.handlers
             if h.type is not None and ast.unparse(h.type) == "Exception"]
    assert found, "the sweep missed a handler it was written to find"
