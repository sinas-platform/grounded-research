"""A theme the material carries and the question does not ask.

The splitter reads the question alone, which is what keeps its output stable
across the cycles of a run. Before #105 it read the draft too: the split moved
between cycles, a part stopped being listed and so stopped being checked, and
once a fifth part appeared that the question never asked and the draft happened
to contain.

Reading the working set would recover the limbs a lawyer supplies from knowing
the area, on questions whose wording does not name them. Measured on the runs
held on 10 September: 1 question of 19 splits into a single part restating
itself, and 31 answers split into two, three or four parts correctly. Moving
the splitter would help the one and put the thirty-one at the mercy of what
retrieval returned, and inventing a part from the material is the defect #105
removed.

So the material is read and the split is not touched. What comes back is an
observation: not checked, not coverable, and unable to hold an answer back.

Run from the backend directory:
`python -m pytest tests/test_a_theme_no_part_covers.py`
"""

from __future__ import annotations

from app.services.query_runner import _themes_from_reply


def test_a_clean_reply_gives_themes():
    got = _themes_from_reply('{"themes": ["interim relief under Articles 278 and 279 TFEU"]}')
    assert got == ["interim relief under Articles 278 and 279 TFEU"]


def test_the_ordinary_case_is_nothing_to_report():
    assert _themes_from_reply('{"themes": []}') == []


def test_a_fenced_or_prefixed_reply_still_parses():
    for wrapped in ('```json\n{"themes": ["a route"]}\n```',
                    'json {"themes": ["a route"]}',
                    'Here:\n{"themes": ["a route"]}\nEnd.'):
        assert _themes_from_reply(wrapped) == ["a route"], wrapped[:24]


def test_a_malformed_reply_reports_nothing_rather_than_an_error_string():
    """A theme that is really an error message would read as a finding."""
    for broken in ("", "no json here", '{"themes": ', '{"themes": "not a list"}',
                   '{"parts": ["wrong key"]}'):
        assert _themes_from_reply(broken) == [], repr(broken)


def test_non_strings_are_dropped_not_stringified():
    got = _themes_from_reply('{"themes": ["a route", {"b": 1}, null, 7, "  ", "another"]}')
    assert got == ["a route", "another"]


def test_the_list_is_bounded():
    many = '{"themes": [' + ",".join(f'"t{i}"' for i in range(20)) + ']}'
    assert len(_themes_from_reply(many)) == 5


# ── The observation is made once, before drafting, and is bounded ──────────
#
# It is not retried at the gate: the observation is about the question and
# what retrieval returned, and after drafting it would be a different
# observation. So the two things that can go wrong have to be legible from the
# record rather than inferred from a missing key.

import asyncio
import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import query_runner as qr


@pytest.fixture
def tele(monkeypatch):
    written: dict = {}

    class _S:
        async def get(self, _m, _i):
            return SimpleNamespace(telemetry={"validate": {}})

        async def execute(self, *_a, **_k):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
                scalar_one_or_none=lambda: None, all=lambda: [])

    @asynccontextmanager
    async def _sl():
        yield _S()

    async def _tele(_run_id, stage, **detail):
        written.setdefault(stage, {}).update(detail)

    monkeypatch.setattr(qr, "AsyncSessionLocal", _sl)
    monkeypatch.setattr(qr, "_tele", _tele)
    return written


@pytest.mark.asyncio
async def test_an_unavailable_split_says_so_rather_than_leaving_the_key_absent(tele):
    """Absent read the same as three different facts: the run predates the
    field, the observation ran and found nothing, and the split was not there
    to observe against."""
    got = await qr._uncovered_themes(
        object(), uuid.uuid4(), "Q?", parts=[], working_set="material")

    assert got == []
    assert tele["validate"]["uncovered_themes_not_observed"] == "no question parts"
    assert "uncovered_themes" not in tele["validate"], (
        "an empty list would say the observation ran"
    )


@pytest.mark.asyncio
async def test_an_empty_working_set_is_a_different_reason(tele):
    got = await qr._uncovered_themes(
        object(), uuid.uuid4(), "Q?", parts=["whether it applies"], working_set="")

    assert got == []
    assert tele["validate"]["uncovered_themes_not_observed"] == "no working set"


def test_the_observation_is_bounded_and_its_cost_recorded():
    """Two invokes under the general policy, at three attempts each with a
    600s client timeout and the 5s and 15s retry waits, is a worst case near
    an hour, against a median published run of 559s. Optional work that can
    outlast the run it observes is not optional.

    The budget is a first cut, so the elapsed time is written every run and
    the next reading of it comes from measurement.
    """
    src = inspect.getsource(qr._stage_synthesize)
    assert "asyncio.timeout(_OBSERVATION_BUDGET_S)" in src
    assert "uncovered_themes_seconds" in src
    assert qr._OBSERVATION_BUDGET_S <= 300, (
        "a best-effort observation may not approach the length of a run"
    )


def test_a_timeout_is_recorded_and_does_not_stop_the_run():
    """It sits on the path to `_argument_plan`, which is required work."""
    src = inspect.getsource(qr._stage_synthesize)
    body = src[src.index("observed_from = time.monotonic()"):]
    body = body[:body.index("_plan_text, plan_claims")]
    assert "except TimeoutError:" in body
    assert '"over budget"' in body
    assert "raise" in body.split("except CancelledOutcome:")[1][:40], (
        "a cancel is control flow and must still reach the runner"
    )
