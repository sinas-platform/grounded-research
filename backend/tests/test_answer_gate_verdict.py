"""The answer gate's verdict must reach the caller.

The gate had been returning "treated as pass" for every answer regardless of
what the judge said, because a NameError inside a wide `try` was caught by
the same except that handles an unparseable reply. Nothing distinguished a
gate that objected from a gate that had crashed.

These tests exercise `_gate_answer` against a fake Sinas with the DB reads
stubbed, and assert the three things that were silently lost: a not-
publishable verdict blocks, an uncovered part of the question blocks even
when the judge says publishable, and a reply that cannot be read is repaired
once and then stops the run rather than passing it.

Run from the backend directory:
`python -m pytest tests/test_answer_gate_verdict.py`
"""

import ast
import inspect
import json
import textwrap
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import query_runner as qr


class _FakeSinas:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def invoke(self, agent, message):
        self.calls.append((agent, message))
        return self.reply


@pytest.fixture
def gate_env(monkeypatch):
    """Stub the gate's two DB reads and capture its telemetry."""
    tele: dict = {}

    class _Session:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
                scalar_one_or_none=lambda: None,
                all=lambda: [(1, "The Commission may inspect business premises.")],
            )

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    async def _tele(run_id, stage, **detail):
        tele.setdefault(stage, {}).update(detail)

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
    monkeypatch.setattr(qr, "_tele", _tele)
    return tele


async def _gate(reply, tele_unused=None):
    return await qr._gate_answer(_FakeSinas(reply), "Q?", uuid.uuid4(), uuid.uuid4())


@pytest.mark.asyncio
async def test_not_publishable_blocks(gate_env):
    ok, missing, _issues, _corr, uncovered = await _gate(
        json.dumps({"publishable": False, "missing": "no conclusion is drawn"})
    )
    assert ok is False
    assert missing == "no conclusion is drawn"
    assert uncovered == []


@pytest.mark.asyncio
async def test_uncovered_part_blocks_even_when_judge_says_publishable(gate_env):
    ok, missing, _issues, _corr, uncovered = await _gate(
        json.dumps(
            {
                "publishable": True,
                "parts": [
                    {"asks": "what the conditions are", "covered": True},
                    {
                        "asks": "whether it is mandatory",
                        "covered": False,
                        "gap": "nothing addresses whether the step is mandatory",
                    },
                ],
            }
        )
    )
    assert ok is False
    assert uncovered == ["nothing addresses whether the step is mandatory"]
    assert "mandatory" in missing


@pytest.mark.asyncio
async def test_correctness_defects_are_separated_from_quality_issues(gate_env):
    _ok, _missing, issues, correctness, _unc = await _gate(
        json.dumps(
            {
                "publishable": True,
                "tension": "claims 1 and 6 state different liability standards",
                "unused_sources": ["C-89-11P.md: the judgment on the point in claim 3"],
            }
        )
    )
    assert any("liability standards" in c for c in correctness)
    assert any("Owed source unused" in i for i in issues)
    # quality issues must never appear in the blocking list
    assert not any("Stronger source unused" in c for c in correctness)


@pytest.mark.asyncio
async def test_a_named_stronger_source_becomes_a_point_to_ground(gate_env):
    """Naming the document in prose is not enough.

    Revision may cite only passages it is shown, and it is shown passages for
    the points it is handed. A run was told to use 32025M11936.md, given no
    line of it, and correctly changed nothing — which read as the reviser
    ignoring the gate.
    """
    _ok, _missing, issues, _corr, points = await _gate(
        json.dumps(
            {
                "publishable": True,
                "parts": [
                    {
                        "asks": "which market definition applies",
                        "covered": False,
                        "gap": "no market definition is given",
                    }
                ],
                "unused_sources": [
                    "32025M11936.md: records the decision defining "
                    "the market, more authoritative than m11936.md"
                ],
            }
        )
    )
    assert any("32025M11936.md" in p for p in points), points
    # the coverage gap comes first: it is what blocks publication, and the
    # number of points revision extracts for is bounded
    assert points[0] == "no market definition is given"
    assert any("Owed source unused" in i for i in issues)


class _SequenceSinas:
    """Replies in order, so a repair can be answered differently from the
    reply it repairs. The last reply is repeated if asked again."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def invoke(self, agent, message):
        self.calls.append((agent, message))
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


async def _gate_seq(sinas):
    return await qr._gate_answer(sinas, "Q?", uuid.uuid4(), uuid.uuid4())


VALID = json.dumps({"publishable": False, "missing": "no conclusion is drawn"})


@pytest.mark.asyncio
async def test_a_reply_that_cannot_be_read_is_repaired_once(gate_env):
    """The drafter repairs its own malformed reply; this is that, on the one
    call that decides whether an answer is publishable."""
    sinas = _SequenceSinas("I cannot judge this.", VALID)
    ok, missing, _issues, _corr, _unc = await _gate_seq(sinas)
    assert len(sinas.calls) == 2
    assert ok is False
    assert missing == "no conclusion is drawn"
    assert gate_env["validate"]["gate_reparse"]


@pytest.mark.asyncio
async def test_the_repair_asks_the_gate_and_carries_the_previous_reply(gate_env):
    sinas = _SequenceSinas("I cannot judge this.", VALID)
    await _gate_seq(sinas)
    agent, prompt = sinas.calls[1]
    assert agent == "sgr/answer-gate-agent"
    assert "I cannot judge this." in prompt
    assert "valid JSON" in prompt


@pytest.mark.asyncio
async def test_a_reply_that_is_not_an_object_is_repaired(gate_env):
    """It parses. It carries no verdict, and letting it through was the
    silent pass in another costume."""
    sinas = _SequenceSinas('["not", "a", "verdict"]', VALID)
    ok, _missing, _issues, _corr, _unc = await _gate_seq(sinas)
    assert len(sinas.calls) == 2
    assert ok is False


@pytest.mark.asyncio
async def test_two_failures_stop_the_run_rather_than_publishing_it(gate_env):
    """The defect this file was extended for: the gate is the only stage
    that asks whether the answer addresses the question, so a reply it
    cannot read must never read as approval."""
    sinas = _SequenceSinas("I cannot judge this.")
    with pytest.raises(qr.PartialOutcome) as e:
        await _gate_seq(sinas)
    assert e.value.cause == "coverage"
    assert "never established" in e.value.explanation
    assert len(sinas.calls) == 2


@pytest.mark.asyncio
async def test_both_attempts_are_recorded(gate_env):
    """A retry nobody can count is a retry nobody can act on, and the
    terminal key keeps its old name so runs either side of this change
    answer one query."""
    with pytest.raises(qr.PartialOutcome):
        await _gate_seq(_SequenceSinas("prose, not a verdict"))
    assert gate_env["validate"]["gate_reparse"]
    assert gate_env["validate"]["gate_unparseable"]


def test_the_verdict_is_returned_from_outside_any_broad_try():
    """The shape that hid the bug: the whole gate body sat inside one
    `try: ... except Exception: return True`, so a NameError in it was
    indistinguishable from a judge that replied prose. Only the parse may be
    guarded; the verdict must be built and returned outside it, where a fault
    fails the run instead of publishing the answer.
    """
    src = inspect.getsource(qr._gate_answer)
    fn = ast.parse(textwrap.dedent(src)).body[0]

    guarded = {
        id(node)
        for t in ast.walk(fn)
        if isinstance(t, ast.Try)
        if any(
            isinstance(h.type, ast.Name) and h.type.id == "Exception"
            for h in t.handlers
        )
        for stmt in t.body
        for node in ast.walk(stmt)
    }
    verdicts = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Tuple)
        and any(isinstance(e, ast.Name) and e.id == "publishable" for e in n.value.elts)
    ]
    assert verdicts, "the gate no longer returns a publishable verdict"
    for r in verdicts:
        assert id(r) not in guarded, (
            "the gate's verdict is returned from inside a try/except Exception; "
            "a code fault there becomes a passing verdict"
        )
