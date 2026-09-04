"""Unit tests for the gate's per-cycle record.

`_tele` merges by key and cannot delete, so a stage writing the same key every
cycle keeps only its last one. That is now the fourth field in this file to
need numbering, after `round_N`, `revision_N` and `cycle_N`.

It is the one that was asked for. With only the flat keys, a source still owed
at the end could be shown its note and its running feed count and nothing about
WHEN the gate first named it, so "does the gate flag a still-owed source on the
first cycle or the last" had no answer in the stored data. `gate_N.fed` is that
answer.

The `gate` prefix also broke the numbering helper, which counted any key
starting with `prefix_`. Six flat keys share that prefix, so a run's first gate
cycle would have been called `gate_7`.

The DB read and the telemetry write are stubbed, so no DB and no network.

Run from the backend directory:
`python -m pytest tests/test_gate_cycle_history.py`
"""

import pytest
from app.services import query_runner as qr
from app.services.query_runner import _cycle_key, _is_numbered

# Every flat key the validate stage writes that starts with "gate_".
FLAT = {
    "gate_parts": [], "gate_issues": [], "gate_redraft": None,
    "gate_reparse": None, "gate_unaccounted": [], "gate_unparseable": None,
}


# -- the numbering helper ------------------------------------------------------


def test_a_flat_key_sharing_the_prefix_is_not_a_cycle():
    """The trap this had to clear first. Counting on the prefix alone would
    have opened a run's history at `gate_7`."""
    assert _cycle_key(FLAT, "gate") == "gate_1"


def test_cycles_number_from_one_upward_alongside_the_flat_keys():
    entry = dict(FLAT)
    for expected in ("gate_1", "gate_2", "gate_3"):
        key = _cycle_key(entry, "gate")
        assert key == expected
        entry[key] = {}


def test_the_other_three_prefixes_are_unchanged():
    """`round_N`, `revision_N` and `cycle_N` were correct before this and must
    stay correct: the helper is shared."""
    assert _cycle_key({}, "revision") == "revision_1"
    assert _cycle_key({"revision_1": {}, "revision_2": {}}, "revision") == "revision_3"
    assert _cycle_key({"round_1": {}}, "round") == "round_2"
    assert _cycle_key({"cycle_1": {}, "cycle_2": {}}, "cycle") == "cycle_3"


def test_numbered_means_digits_after_the_prefix():
    assert _is_numbered("gate_1", "gate")
    assert _is_numbered("gate_12", "gate")
    assert not _is_numbered("gate_parts", "gate")
    assert not _is_numbered("gate_unaccounted", "gate")
    assert not _is_numbered("gate", "gate")
    assert not _is_numbered("gateway_1", "gate")
    # A different stage's numbered key is not this stage's.
    assert not _is_numbered("revision_1", "gate")


# -- amending the cycle a gate pass opened -------------------------------------
#
# A gate pass is recorded in two halves and they cannot be one write: `issues`
# is only complete after the caller adds the provenance check that runs outside
# the gate. Writing the second half flat is what made it a last-write.


@pytest.fixture
def telemetry(monkeypatch):
    """A stand-in for the run row's `telemetry->'validate'`."""
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
            return FakeRun()

    async def fake_tele(run_id, stage, **detail):
        assert stage == "validate"
        state["validate"].update(detail)

    monkeypatch.setattr(qr, "AsyncSessionLocal", FakeSession)
    monkeypatch.setattr(qr, "_tele", fake_tele)
    return state


@pytest.mark.asyncio
async def test_the_amend_lands_in_the_open_cycle(telemetry):
    telemetry["validate"] = {**FLAT, "gate_1": {"at": "t1", "fed": []}}
    await qr._amend_gate_cycle("run-1", redraft="part 2", issues=["a", "b"])
    assert telemetry["validate"]["gate_1"] == {
        "at": "t1", "fed": [], "redraft": "part 2", "issues": ["a", "b"]}


@pytest.mark.asyncio
async def test_the_amend_does_not_open_a_new_cycle(telemetry):
    telemetry["validate"] = {"gate_1": {}, "gate_2": {}}
    await qr._amend_gate_cycle("run-1", issues=["x"])
    assert sorted(k for k in telemetry["validate"] if _is_numbered(k, "gate")) == [
        "gate_1", "gate_2"]


@pytest.mark.asyncio
async def test_the_amend_picks_the_highest_cycle_not_the_last_string(telemetry):
    """Ten sorts before two as a string. The cycle count passes ten on a long
    run, and 04e1ae70 reached sixteen revisions."""
    telemetry["validate"] = {f"gate_{i}": {} for i in range(1, 13)}
    await qr._amend_gate_cycle("run-1", issues=["x"])
    assert telemetry["validate"]["gate_12"] == {"issues": ["x"]}
    assert telemetry["validate"]["gate_2"] == {}


@pytest.mark.asyncio
async def test_an_amend_with_no_cycle_open_does_nothing(telemetry):
    """Every path into the caller has been through `_record_gate_cycle` first,
    so this should not happen. It must not raise if it does: telemetry is
    bookkeeping and may not fail a run."""
    telemetry["validate"] = dict(FLAT)
    await qr._amend_gate_cycle("run-1", issues=["x"])
    assert not [k for k in telemetry["validate"] if _is_numbered(k, "gate")]


@pytest.mark.asyncio
async def test_an_amend_does_not_disturb_an_earlier_cycle(telemetry):
    telemetry["validate"] = {"gate_1": {"issues": ["first"], "fed": [{"doc": "a.md"}]},
                             "gate_2": {"fed": []}}
    await qr._amend_gate_cycle("run-1", issues=["second"])
    assert telemetry["validate"]["gate_1"]["issues"] == ["first"]
    assert telemetry["validate"]["gate_2"]["issues"] == ["second"]


# -- what the cycle records ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_cycle_records_which_obligations_were_fed(telemetry):
    """The point of the whole change. `fed` dates an obligation's arrival,
    which the ledger cannot: it keeps a running total and no history."""
    await qr._record_gate_cycle(
        "run-1", parts=[{"asks": "x", "covered": True}],
        unaccounted=["b.md"],
        fed=[{"doc": "a.md", "feeds": 1}, {"doc": "b.md", "feeds": 3}],
        system_waived=["c.md"])
    cycle = telemetry["validate"]["gate_1"]
    assert cycle["fed"] == [{"doc": "a.md", "feeds": 1}, {"doc": "b.md", "feeds": 3}]
    assert cycle["system_waived"] == ["c.md"]
    assert cycle["unaccounted"] == ["b.md"]


@pytest.mark.asyncio
async def test_two_gate_passes_keep_both_records(telemetry):
    """The defect itself: before numbering, the second pass erased the first,
    so a run that fed an obligation three times looked like it fed it once."""
    await qr._record_gate_cycle("run-1", parts=[],
                                fed=[{"doc": "a.md", "feeds": 1}])
    await qr._record_gate_cycle("run-1", parts=[],
                                fed=[{"doc": "a.md", "feeds": 2}])
    assert telemetry["validate"]["gate_1"]["fed"] == [{"doc": "a.md", "feeds": 1}]
    assert telemetry["validate"]["gate_2"]["fed"] == [{"doc": "a.md", "feeds": 2}]


@pytest.mark.asyncio
async def test_the_flat_keys_still_carry_the_final_state(telemetry):
    """`answer_regress` reads `gate_redraft` and the run export reads
    `gate_issues`. Both want the last cycle, and both keep working."""
    await qr._record_gate_cycle("run-1", parts=[{"asks": "x"}],
                                unaccounted=["b.md"], reparse="repaired")
    assert telemetry["validate"]["gate_unaccounted"] == ["b.md"]
    assert telemetry["validate"]["gate_parts"] == [{"asks": "x"}]
    assert telemetry["validate"]["gate_reparse"] == "repaired"


@pytest.mark.asyncio
async def test_a_cycle_with_nothing_fed_records_an_empty_list(telemetry):
    """Not absent. A cycle that fed nothing is a fact about that cycle, and
    reading it back as "unknown" is how the last-write looked."""
    await qr._record_gate_cycle("run-1", parts=[])
    assert telemetry["validate"]["gate_1"]["fed"] == []
    assert telemetry["validate"]["gate_1"]["system_waived"] == []


# -- the amend must not sit behind an early return -----------------------------


def runner_tree():
    import ast
    import inspect
    import pathlib as _p

    return ast.parse(_p.Path(inspect.getfile(qr)).read_text(encoding="utf-8"))


def test_the_amend_is_the_statement_after_the_gate_call():
    """Two paths out of the validation block return before any later write:
    publishing, and the sweep asking for a repair cycle, which recurses into a
    fresh validate. Amending on the branches left those cycles recorded with
    their fed list and no issues, which is this same gap in a smaller place.

    `issues` is final the moment `_gate_answer` returns and is only read after,
    so the amend belongs immediately next to the call. Checked on the tree
    rather than on offsets: the prose around these lines says "returns" often
    enough that string search reads a comment as control flow.
    """
    import ast

    def is_gate_call(stmt) -> bool:
        """The assignment itself, not every block that encloses it. Matching on
        unparsed text counted the function, the loop and the branch around it
        as three more call sites."""
        val = getattr(stmt, "value", None)
        if isinstance(val, ast.Await):
            val = val.value
        return isinstance(val, ast.Call) and getattr(val.func, "id", "") == "_gate_answer"

    tree = runner_tree()
    recording = 0
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for n, stmt in enumerate(body[:-1]):
            if not is_gate_call(stmt):
                continue
            if "_issues" in ast.unparse(stmt):
                # The read-only caller discards the issues and opens no cycle.
                continue
            nxt = ast.unparse(body[n + 1])
            assert "_amend_gate_cycle" in nxt, (
                "a branch sits between the gate call and its amend: " + nxt[:120])
            assert "redraft=missing" in nxt and "issues=issues" in nxt, nxt[:160]
            recording += 1
    assert recording == 2, f"expected both recording call sites, saw {recording}"
