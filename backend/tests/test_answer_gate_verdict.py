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

SPLIT_CALL = "Split the question into the distinct things"
DEFAULT_SPLIT = json.dumps({"parts": ["whether it applies", "whether it is mandatory"]})
SPLIT_ONE = json.dumps({"parts": ["whether it applies"]})


class _FakeSinas:
    """Answers by what it was asked, not by call order: the gate splits the
    question in its own call before it judges coverage."""

    def __init__(self, reply, split=DEFAULT_SPLIT):
        self.reply = reply
        self.split = split
        self.calls = []

    async def invoke(self, agent, message):
        self.calls.append((agent, message))
        return self.split if SPLIT_CALL in message else self.reply


@pytest.fixture
def gate_env(monkeypatch):
    """Stub the gate's two DB reads and capture its telemetry."""
    tele: dict = {}

    class _Session:
        async def get(self, _model, _ident):
            # The gate looks for a split already made for this run, so the
            # stub reflects what _tele below recorded.
            return SimpleNamespace(
                telemetry={"validate": dict(tele.get("validate") or {})}
            )

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


async def _gate(reply, split=DEFAULT_SPLIT):
    return await qr._gate_answer(
        _FakeSinas(reply, split), "Q?", uuid.uuid4(), uuid.uuid4()
    )


@pytest.mark.asyncio
async def test_not_publishable_blocks(gate_env):
    ok, missing, _issues, _corr, uncovered = await _gate(
        json.dumps(
            {
                "publishable": False,
                "missing": "no conclusion is drawn",
                "parts": [{"n": 1, "covered": True}, {"n": 2, "covered": True}],
            }
        )
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
                    {"n": 1, "covered": True},
                    {
                        "n": 2,
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
                    {"n": 1, "covered": False, "gap": "no market definition is given"}
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

    def __init__(self, *replies, split=DEFAULT_SPLIT):
        self.replies = list(replies)
        self.split = split
        self.calls = []

    async def invoke(self, agent, message):
        self.calls.append((agent, message))
        # The split is answered by content, so a sequence of verdict replies
        # keeps its order however many calls the gate makes around it.
        if SPLIT_CALL in message:
            return self.split
        n = len([c for c in self.calls if SPLIT_CALL not in c[1]]) - 1
        return self.replies[min(n, len(self.replies) - 1)]


def verdicts(sinas):
    """The calls that asked for a verdict. The split is a call of its own
    now, so a count of every call no longer means what these tests mean."""
    return [m for _a, m in sinas.calls if SPLIT_CALL not in m]


async def _gate_seq(sinas):
    return await qr._gate_answer(sinas, "Q?", uuid.uuid4(), uuid.uuid4())


VALID = json.dumps(
    {
        "publishable": False,
        "missing": "no conclusion is drawn",
        "parts": [{"n": 1, "covered": True}, {"n": 2, "covered": True}],
    }
)


@pytest.mark.asyncio
async def test_a_reply_that_cannot_be_read_is_repaired_once(gate_env):
    """The drafter repairs its own malformed reply; this is that, on the one
    call that decides whether an answer is publishable."""
    sinas = _SequenceSinas("I cannot judge this.", VALID)
    ok, missing, _issues, _corr, _unc = await _gate_seq(sinas)
    assert len(verdicts(sinas)) == 2
    assert ok is False
    assert missing == "no conclusion is drawn"
    assert gate_env["validate"]["gate_reparse"]


@pytest.mark.asyncio
async def test_the_repair_asks_the_gate_and_carries_the_previous_reply(gate_env):
    sinas = _SequenceSinas("I cannot judge this.", VALID)
    await _gate_seq(sinas)
    agent, prompt = sinas.calls[-1]
    assert agent == "sgr/answer-gate-agent"
    assert "I cannot judge this." in prompt
    assert "valid JSON" in prompt


@pytest.mark.asyncio
async def test_a_reply_that_is_not_an_object_is_repaired(gate_env):
    """It parses. It carries no verdict, and letting it through was the
    silent pass in another costume."""
    sinas = _SequenceSinas('["not", "a", "verdict"]', VALID)
    ok, _missing, _issues, _corr, _unc = await _gate_seq(sinas)
    assert len(verdicts(sinas)) == 2
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
    assert len(verdicts(sinas)) == 2


@pytest.mark.asyncio
async def test_both_attempts_are_recorded(gate_env):
    """A retry nobody can count is a retry nobody can act on, and the
    terminal key keeps its old name so runs either side of this change
    answer one query."""
    with pytest.raises(qr.PartialOutcome):
        await _gate_seq(_SequenceSinas("prose, not a verdict"))
    assert gate_env["validate"]["gate_reparse"]
    assert gate_env["validate"]["gate_unparseable"]


@pytest.mark.asyncio
async def test_the_decomposition_is_recorded(gate_env):
    """uncovered names the parts the gate failed. It cannot show a part the
    gate never wrote down, and that is the case nobody can currently count."""
    await _gate(
        json.dumps(
            {
                "publishable": True,
                "parts": [
                    {"n": 1, "covered": True},
                    {
                        "n": 2,
                        "covered": False,
                        "gap": "nothing addresses whether the step is mandatory",
                    },
                ],
            }
        )
    )
    recorded = gate_env["validate"]["gate_parts"]
    assert [p["asks"] for p in recorded] == [
        "whether it applies",
        "whether it is mandatory",
    ]
    assert [p["covered"] for p in recorded] == [True, False]
    assert recorded[1]["gap"].startswith("nothing addresses")


@pytest.mark.asyncio
async def test_a_thin_decomposition_is_visible(gate_env):
    """The failure this exists to expose: one part enumerated for a question
    that asks more. It is now the split that is thin rather than the verdict,
    and the recorded decomposition is still the only thing that shows it."""
    ok, missing, _issues, _corr, _unc = await _gate(
        json.dumps({"publishable": True, "parts": [{"n": 1, "covered": True}]}),
        split=SPLIT_ONE,
    )
    assert ok is True
    assert missing == ""
    assert len(gate_env["validate"]["gate_parts"]) == 1


@pytest.mark.asyncio
async def test_a_verdict_naming_no_part_leaves_every_part_uncovered(gate_env):
    """Was: a verdict with no parts records an empty list. With a fixed
    decomposition the record is the run's parts, not the verdict's, so a
    verdict that names none of them leaves all of them uncovered. Silence
    about a part cannot read as coverage."""
    ok, _missing, _issues, _corr, uncovered = await _gate(
        json.dumps({"publishable": True})
    )
    assert ok is False
    assert len(gate_env["validate"]["gate_parts"]) == 2
    assert uncovered == ["the review returned no verdict for this part"] * 2


@pytest.mark.asyncio
async def test_an_unreadable_verdict_does_not_inherit_the_previous_decomposition(
    gate_env,
):
    """The gate runs once per validation cycle and telemetry merges, so a
    cycle that parsed leaves its decomposition behind. Without an empty write
    on the unreadable path, the next cycle's verdict would be recorded with
    parts it never produced, and the record would be wrong in exactly the
    case it exists to expose."""
    await _gate(
        json.dumps({"publishable": True, "parts": [{"n": 1, "covered": True}]}),
        split=SPLIT_ONE,
    )
    assert len(gate_env["validate"]["gate_parts"]) == 1

    # Under the merged repair flow (#102), a twice-unreadable verdict raises
    # rather than passing — the staleness property is asserted on what was
    # recorded before the raise.
    with pytest.raises(qr.PartialOutcome):
        await _gate("I cannot judge this.")
    assert gate_env["validate"]["gate_parts"] == []
    assert gate_env["validate"]["gate_unparseable"]


@pytest.mark.asyncio
async def test_a_readable_verdict_does_not_inherit_the_previous_failure(gate_env):
    """The mirror of the case above, and reachable for the same reason: a
    cycle can be followed by another when the pre-publish sweep feeds a
    repair and the caller re-enters the loop. Without the whole outcome
    being written each cycle, the record would say the gate produced no
    verdict when the one that decided the run parsed fine."""
    # The split is stored by the first cycle, so both cycles here judge the
    # same one part.
    with pytest.raises(qr.PartialOutcome):
        await _gate("I cannot judge this.", split=SPLIT_ONE)
    assert gate_env["validate"]["gate_unparseable"]

    await _gate(
        json.dumps({"publishable": True, "parts": [{"n": 1, "covered": True}]}),
        split=SPLIT_ONE,
    )
    assert gate_env["validate"]["gate_unparseable"] is None
    assert len(gate_env["validate"]["gate_parts"]) == 1


@pytest.mark.asyncio
async def test_the_absent_key_is_written_null_not_omitted(gate_env):
    """Null rather than omitted is what makes the clearing work at all, since
    telemetry merges and cannot delete. The cost is that key-existence tests
    stop discriminating, so these are truthiness fields."""
    await _gate(json.dumps({"publishable": True, "parts": []}))
    assert "gate_unparseable" in gate_env["validate"]
    assert gate_env["validate"]["gate_unparseable"] is None
    assert not gate_env["validate"]["gate_unparseable"]


@pytest.mark.asyncio
async def test_covered_is_stored_as_a_boolean(gate_env):
    """The check reads it as truthiness, so the record has to agree with the
    check rather than with the reply."""
    await _gate(
        json.dumps(
            {
                "publishable": True,
                "parts": [{"n": 1, "covered": "yes"}, {"n": 2, "covered": 0}],
            }
        )
    )
    assert [p["covered"] for p in gate_env["validate"]["gate_parts"]] == [True, False]


@pytest.mark.asyncio
async def test_what_is_recorded_is_what_the_check_judged(gate_env):
    """Was: a part that is not an object is absent from the record. With a
    fixed decomposition it cannot be absent, because the record is the run's
    parts. An element the check cannot read leaves its part uncovered
    instead, which is the same rule seen from the other side."""
    ok, _missing, _issues, _corr, uncovered = await _gate(
        json.dumps(
            {"publishable": True, "parts": ["not an object", {"n": 2, "covered": True}]}
        )
    )
    recorded = gate_env["validate"]["gate_parts"]
    assert [r["asks"] for r in recorded] == [
        "whether it applies",
        "whether it is mandatory",
    ]
    assert recorded[1]["covered"] is True
    assert ok is False
    assert uncovered == ["the review returned no verdict for this part"]


@pytest.mark.asyncio
async def test_long_text_is_bounded(gate_env):
    """`asks` is bounded where it now comes from, the split; `gap` where it
    still comes from, the verdict."""
    await _gate(
        json.dumps(
            {
                "publishable": True,
                "parts": [{"n": 1, "covered": False, "gap": "y" * 900}],
            }
        ),
        split=json.dumps({"parts": ["x" * 900]}),
    )
    part = gate_env["validate"]["gate_parts"][0]
    assert len(part["asks"]) == 300
    assert len(part["gap"]) == 300


class _ScriptedSinas:
    """Replies in order, and records what each call was asked. The split and
    the verdict are separate calls now, so a test needs both."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def invoke(self, agent, message):
        self.calls.append(message)
        i = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[i]


async def _run(sinas):
    return await qr._gate_answer(sinas, "Q?", uuid.uuid4(), uuid.uuid4())


# -- the decomposition is fixed and reused ------------------------------------


@pytest.mark.asyncio
async def test_the_question_is_split_before_the_verdict(gate_env):
    sinas = _ScriptedSinas(
        DEFAULT_SPLIT,
        json.dumps(
            {
                "publishable": True,
                "parts": [{"n": 1, "covered": True}, {"n": 2, "covered": True}],
            }
        ),
    )
    ok, _missing, _issues, _corr, _unc = await _run(sinas)
    assert len(sinas.calls) == 2
    assert "Split the question" in sinas.calls[0]
    assert "PARTS OF THE QUESTION (fixed for this run" in sinas.calls[1]
    assert ok is True


@pytest.mark.asyncio
async def test_the_split_is_stored_for_later_cycles(gate_env):
    sinas = _ScriptedSinas(
        DEFAULT_SPLIT,
        json.dumps(
            {
                "publishable": True,
                "parts": [{"n": 1, "covered": True}, {"n": 2, "covered": True}],
            }
        ),
    )
    await _run(sinas)
    assert gate_env["validate"]["question_parts"] == [
        "whether it applies",
        "whether it is mandatory",
    ]


@pytest.mark.asyncio
async def test_the_split_call_is_not_given_the_claims(gate_env):
    """The whole point: the question is 0.4% of the verdict prompt, so a
    split derived there follows the claims. This one sees the question."""
    sinas = _ScriptedSinas(
        DEFAULT_SPLIT,
        json.dumps(
            {
                "publishable": True,
                "parts": [{"n": 1, "covered": True}, {"n": 2, "covered": True}],
            }
        ),
    )
    await _run(sinas)
    assert "CLAIMS OF THE DRAFT ANSWER" not in sinas.calls[0]
    assert "WORKING DOCUMENT SET" not in sinas.calls[0]


# -- what makes the fixed list binding ----------------------------------------


@pytest.mark.asyncio
async def test_a_part_the_runner_did_not_ask_about_is_dropped(gate_env):
    """A run added a fifth part the question does not ask, because the draft
    contained the material, and marked it covered. Out of range, so ignored."""
    sinas = _ScriptedSinas(
        DEFAULT_SPLIT,
        json.dumps(
            {
                "publishable": True,
                "parts": [
                    {"n": 1, "covered": True},
                    {"n": 2, "covered": True},
                    {"n": 3, "covered": True},
                ],
            }
        ),
    )
    ok, _missing, _issues, _corr, uncovered = await _run(sinas)
    assert ok is True
    assert uncovered == []


@pytest.mark.asyncio
async def test_a_part_with_no_verdict_is_uncovered_not_assumed(gate_env):
    """The other half. Silence about a part cannot read as coverage, or the
    fixed list is advice rather than a rule."""
    sinas = _ScriptedSinas(
        DEFAULT_SPLIT,
        json.dumps({"publishable": True, "parts": [{"n": 1, "covered": True}]}),
    )
    ok, missing, _issues, _corr, uncovered = await _run(sinas)
    assert ok is False
    assert uncovered == ["the review returned no verdict for this part"]
    assert "no verdict" in missing


@pytest.mark.asyncio
async def test_an_uncovered_fixed_part_still_blocks(gate_env):
    sinas = _ScriptedSinas(
        DEFAULT_SPLIT,
        json.dumps(
            {
                "publishable": True,
                "parts": [
                    {"n": 1, "covered": True},
                    {
                        "n": 2,
                        "covered": False,
                        "gap": "nothing says whether it is mandatory",
                    },
                ],
            }
        ),
    )
    ok, missing, _issues, _corr, uncovered = await _run(sinas)
    assert ok is False
    assert uncovered == ["nothing says whether it is mandatory"]
    assert "mandatory" in missing


# -- degrading to the old behaviour -------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_split_falls_back_to_deriving_it(gate_env):
    """Two failures on the split, then the verdict prompt asks for the split
    as it does today. An improvement that cannot be applied must not fail a
    run that worked without it."""
    sinas = _ScriptedSinas(
        "not json",
        "still not json",
        json.dumps(
            {
                "publishable": True,
                "parts": [{"asks": "whether it applies", "covered": True}],
            }
        ),
    )
    ok, _missing, _issues, _corr, uncovered = await _run(sinas)
    assert len(sinas.calls) == 3
    assert "First split the QUESTION" in sinas.calls[2]
    assert ok is True
    assert uncovered == []
    assert "question_parts" not in (gate_env.get("validate") or {})


@pytest.mark.asyncio
async def test_the_split_is_repaired_once(gate_env):
    sinas = _ScriptedSinas(
        "not json",
        DEFAULT_SPLIT,
        json.dumps(
            {
                "publishable": True,
                "parts": [{"n": 1, "covered": True}, {"n": 2, "covered": True}],
            }
        ),
    )
    await _run(sinas)
    assert len(sinas.calls) == 3
    assert "was not valid JSON" in sinas.calls[1]
    assert gate_env["validate"]["question_parts"]


@pytest.mark.asyncio
async def test_a_part_that_is_not_a_string_makes_the_split_unusable(gate_env):
    """str() on a dict is a non-empty string, so without a type check an
    object in the list becomes a question part that binds every later cycle.
    Dropping it quietly would lose a part, which is the defect this change
    exists to stop, so it goes to the repair instead."""
    sinas = _ScriptedSinas(
        json.dumps({"parts": ["whether it applies", {"asks": "smuggled"}]}),
        SPLIT_ONE,
        json.dumps({"publishable": True, "parts": [{"n": 1, "covered": True}]}),
    )
    await _run(sinas)
    assert gate_env["validate"]["question_parts"] == ["whether it applies"]


@pytest.mark.asyncio
async def test_a_clean_cycle_does_not_inherit_the_previous_repair(gate_env):
    """The third key, and the one the merge of the two gate changes left
    outside the single write. A cycle that needed a repair, followed by one
    that parsed first time, would otherwise record the repair against the
    verdict that decided the run."""
    sinas = _SequenceSinas("not a verdict", VALID)
    await _gate_seq(sinas)
    assert gate_env["validate"]["gate_reparse"]

    await _gate_seq(_SequenceSinas(VALID))
    assert gate_env["validate"]["gate_reparse"] is None


@pytest.mark.asyncio
async def test_a_repair_that_then_parsed_records_both_facts(gate_env):
    """Carried to the one write rather than written where it happens, so the
    cycle's record says both that it needed a repair and what it found."""
    sinas = _SequenceSinas(
        "not a verdict",
        json.dumps({"publishable": True, "parts": [{"n": 1, "covered": True}]}),
        split=SPLIT_ONE,
    )
    await _gate_seq(sinas)
    assert gate_env["validate"]["gate_reparse"]
    assert len(gate_env["validate"]["gate_parts"]) == 1
    assert gate_env["validate"]["gate_unparseable"] is None


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
