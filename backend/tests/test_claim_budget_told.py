"""Unit tests for telling the reviser the answer is full.

`_admit_adds` refuses an addition once the answer is at MAX_CLAIMS and tells
nobody: the claim is not written, the reviser is not informed, and the run
continues as though it had been. Over 131 revision cycles, 23 of the 56
additions the reviser proposed were discarded that way, 41% of everything it
tried to add; three cycles changed nothing at all and all three were cap
refusals; 38 of the 131 ran with the answer already full.

The count already existed as `add_dropped_at_cap`. This says it out loud.

Pure, apart from one telemetry read that is stubbed. No DB and no network.

Run from the backend directory:
`python -m pytest tests/test_claim_budget_told.py`
"""

import pytest
from app.services import query_runner as qr
from app.services.query_runner import MAX_CLAIMS, _claim_budget_line

# -- the budget, before the reviser writes -------------------------------------


def test_room_is_stated_as_a_number():
    line = _claim_budget_line(11)
    assert "holds 11 claims" in line
    assert f"maximum is {MAX_CLAIMS}" in line
    assert "add up to 3" in line


def test_room_is_the_gap_to_the_cap_not_to_the_target():
    """The prompt aims at about 12 and the cap is 14. The reviser was told the
    target and never the cap, so an addition inside its own budget could still
    be discarded."""
    assert "add up to 1" in _claim_budget_line(MAX_CLAIMS - 1)
    assert "add up to 8" in _claim_budget_line(MAX_CLAIMS - 8)


def test_a_full_answer_is_told_it_is_full():
    line = _claim_budget_line(MAX_CLAIMS)
    assert "FULL" in line
    assert "add up to" not in line


def test_a_full_answer_is_told_what_to_do_about_it():
    """Knowing an addition will vanish is not enough on its own. The only move
    that makes room is a drop, and that has to be in the same patch, because a
    patch is applied whole."""
    line = _claim_budget_line(MAX_CLAIMS)
    assert "same patch" in line
    assert '"drop"' in line


def test_over_the_cap_reads_as_full_not_as_negative_room():
    """Claims can exceed the cap: the cap bounds additions, and nothing trims
    an answer that arrives longer. Reporting "add up to -2" would be worse
    than useless."""
    line = _claim_budget_line(MAX_CLAIMS + 2)
    assert "FULL" in line
    assert "-" not in line.split("maximum")[1]


def test_the_target_survives():
    """The instruction to aim at about 12 is still right; it was just never the
    number that decided whether an addition landed."""
    for live in (0, 6, 11, MAX_CLAIMS, MAX_CLAIMS + 3):
        assert "about 12 claims" in _claim_budget_line(live)


# -- what happened to the last reply -------------------------------------------


def test_a_refusal_last_cycle_is_reported():
    line = _claim_budget_line(MAX_CLAIMS, 4)
    assert "proposed 4 addition(s)" in line
    assert "never written" in line


def test_no_refusal_is_not_mentioned():
    """A cycle that lost nothing must not be told it did — the whole point is
    that the number is true."""
    line = _claim_budget_line(MAX_CLAIMS, 0)
    assert "previous reply" not in line
    assert "proposed" not in line


def test_the_refusal_note_is_independent_of_the_room_note():
    """Refusals are read from the previous cycle and room from the current
    one. A drop since then can leave room while the last reply still lost
    additions, and both facts are worth having."""
    line = _claim_budget_line(MAX_CLAIMS - 2, 3)
    assert "add up to 2" in line
    assert "proposed 3 addition(s)" in line


# -- reading the previous cycle ------------------------------------------------


@pytest.fixture
def telemetry(monkeypatch):
    state = {"validate": {}}

    class FakeRun:
        @property
        def telemetry(self):
            return {"validate": state["validate"]}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, run_id):
            return state.get("run", FakeRun())

    monkeypatch.setattr(qr, "AsyncSessionLocal", FakeSession)
    return state


@pytest.mark.asyncio
async def test_the_latest_cycle_is_chosen_by_number(telemetry):
    """`revision_10` sorts before `revision_2` as a string, and runs pass ten
    cycles — one of the French runs reached sixteen."""
    telemetry["validate"] = {
        "revision_2": {"add_dropped_at_cap": 9},
        "revision_10": {"add_dropped_at_cap": 4},
    }
    assert await qr._cap_refusals_last_cycle("run-1") == 4


@pytest.mark.asyncio
async def test_no_cycles_yet_is_zero(telemetry):
    """The first revision of a run has no previous cycle to report."""
    telemetry["validate"] = {}
    assert await qr._cap_refusals_last_cycle("run-1") == 0


@pytest.mark.asyncio
async def test_a_flat_key_is_not_a_cycle(telemetry):
    """`validate` carries flat keys beside the numbered ones. Only
    `revision_<digits>` is a cycle."""
    telemetry["validate"] = {
        "revision_yielded_no_change": True,
        "revision_1": {"add_dropped_at_cap": 2},
    }
    assert await qr._cap_refusals_last_cycle("run-1") == 2


@pytest.mark.asyncio
async def test_a_cycle_without_the_field_is_zero(telemetry):
    telemetry["validate"] = {"revision_1": {"added": 3}}
    assert await qr._cap_refusals_last_cycle("run-1") == 0


# -- wiring --------------------------------------------------------------------


def runner_source() -> str:
    import inspect
    import pathlib

    return pathlib.Path(inspect.getfile(qr)).read_text(encoding="utf-8")


def test_the_reviser_prompt_carries_the_budget():
    src = runner_source()
    assert "+ _claim_budget_line(len(by_claim)," in src
    assert "await _cap_refusals_last_cycle(run_id))" in src


def test_the_bare_target_sentence_is_gone():
    """It said "keep the answer at about 12 claims" and nothing else, so the
    reviser aimed at 12 while 14 was what actually refused it."""
    assert "Keep the answer at about 12 claims." not in runner_source()


def test_the_budget_is_computed_from_the_live_claims():
    """`len(by_claim)` is the answer as it stands this cycle, built before the
    invoke. A stale count would tell the reviser it has room it does not."""
    src = runner_source()
    i = src.index("_claim_budget_line(len(by_claim)")
    assert src.index("by_claim: dict[int, dict] = {}") < i


# -- a reply that changed nothing is still a reply ------------------------------
#
# The no-change path used to return before writing a numbered cycle, so an
# older cycle stayed the latest and the next prompt attributed its refusals to
# a reply that proposed nothing. Telling the reviser it lost four additions it
# never made invites it to drop a sound claim to make room for nothing, which
# is the behaviour this whole change exists to stop.


def test_a_no_change_cycle_is_numbered():
    src = runner_source()
    i = src.index("revision_yielded_no_change=True")
    window = src[i - 700:i + 700]
    assert '_next_cycle_key(run_id, "validate", "revision")' in window
    assert '"yielded_no_change": True' in window


def test_the_no_change_cycle_records_zeros():
    """It reports what was applied, and nothing was. Recording the patch's
    keeps here would claim they landed; the early return means they did not."""
    src = runner_source()
    i = src.index('"yielded_no_change": True')
    window = src[i - 400:i]
    for field in ('"revised": 0', '"added": 0', '"dropped": 0',
                  '"add_dropped_at_cap": 0', '"kept_with_reason": 0'):
        assert field in window, field


@pytest.mark.asyncio
async def test_a_no_change_cycle_clears_the_stale_count(telemetry):
    """The sequence the finding describes: a cycle refuses four additions, the
    next reply yields nothing, and the prompt after that must not still be
    talking about the four."""
    telemetry["validate"] = {
        "revision_1": {"add_dropped_at_cap": 4},
        "revision_2": {"add_dropped_at_cap": 0, "yielded_no_change": True},
    }
    assert await qr._cap_refusals_last_cycle("run-1") == 0
