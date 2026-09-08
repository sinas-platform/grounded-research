"""Unit tests for the pre-publish sweep's per-cycle record.

`_tele` merges by key and cannot delete, so a stage writing the same key every
cycle keeps only its last one. `final_sweep_result` was flat, which makes this
the fifth field to need numbering after `round_N`, `revision_N`, `cycle_N` and
`gate_N`.

It loses the half worth reading. The first sweep is the one that spends the
repair attempt; the second only decides whether to drop. So what the first
sweep objected to, and whether the repair answered it, were both unreadable
from a run that swept twice. Q46 run 75b569ba is one: two sweeps, one stored
finding.

Three flat keys share the `final_sweep_` prefix, so the numbering helper has to
tell a cycle from a flat key here exactly as it does for `gate_`.

No DB and no network: the helper is pure, and the sweep's collaborators are
stubbed so two real sweeps run through `_next_cycle_key` and `_tele`.

Run from the backend directory:
`python -m pytest tests/test_sweep_cycle_history.py`
"""

import uuid

import pytest

from app.services import query_runner as qr
from app.services.query_runner import _cycle_key, _is_numbered

# Every flat key the validate stage writes that starts with "final_sweep".
FLAT = {
    "final_sweeps": 2,
    "final_sweep_dropped": 1,
    "final_sweep_dropped_detail": [],
    "final_sweep_published_after_drop": True,
}


def test_a_flat_key_sharing_the_prefix_is_not_a_cycle():
    """Counting on the prefix alone would open a run's history at
    `final_sweep_4`."""
    assert _cycle_key(FLAT, "final_sweep") == "final_sweep_1"


def test_final_sweeps_is_not_read_as_a_cycle():
    """`final_sweeps` has no underscore before its suffix, so it cannot be
    mistaken for `final_sweep_1` however the helper is changed."""
    assert not _is_numbered("final_sweeps", "final_sweep")


def test_sweeps_number_from_one_upward_alongside_the_flat_keys():
    entry = dict(FLAT)
    for expected in ("final_sweep_1", "final_sweep_2", "final_sweep_3"):
        key = _cycle_key(entry, "final_sweep")
        assert key == expected
        entry[key] = {}


def test_the_second_sweep_does_not_overwrite_the_first():
    """The defect itself: two findings, two keys, both readable."""
    entry = {}
    entry[_cycle_key(entry, "final_sweep")] = {"overreaching": 1}
    entry[_cycle_key(entry, "final_sweep")] = {"overreaching": 0}
    assert entry == {"final_sweep_1": {"overreaching": 1},
                     "final_sweep_2": {"overreaching": 0}}


def test_the_other_four_prefixes_are_unchanged():
    """The helper is shared, and the four fields already numbered by it must
    stay correct."""
    assert _cycle_key({}, "revision") == "revision_1"
    assert _cycle_key({"gate_parts": []}, "gate") == "gate_1"
    assert _cycle_key({"round_1": {}}, "round") == "round_2"
    assert _cycle_key({"cycle_1": {}, "cycle_2": {}}, "cycle") == "cycle_3"


# -- two real sweeps through the telemetry path --------------------------------
#
# Asserting on the source text of `_pre_publish_sweep` would pass while the
# expected strings are present even if the records did not survive, and would
# break on a rename that changed nothing. These run the function.

CID = str(uuid.uuid4())


def _verdict(over=(), failed=(), judged=1, errors=0):
    return {
        "judged": judged, "passed": 0,
        "failed": [{"claim_id": CID, "claim_sequence": s, "reason": "r"}
                   for s in failed],
        "overreaching": [{"claim_id": CID, "claim_sequence": s,
                          "claim_text": f"claim {s}", "uncovered": f"u{s}"}
                         for s in over],
        "errors": [{"evidence_id": str(uuid.uuid4()),
                    "error": "no extracted content"} for _ in range(errors)],
    }


@pytest.fixture
def sweep(monkeypatch):
    """`_pre_publish_sweep` with its DB, telemetry and collaborators stubbed.

    Returns the accumulated `telemetry->'validate'` plus a `dropped` list, so a
    test can assert both what was recorded and whether the drop branch ran."""
    from app.services import faithfulness

    state = {"validate": {}}
    dropped: list = []
    verdicts: list = []

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

        async def execute(self, *a, **k):
            return None

        async def commit(self):
            return None

    async def fake_tele(run_id, stage, **detail):
        assert stage == "validate"
        state["validate"].update(detail)

    async def fake_validate(session, caller, answer_id, **kw):
        return verdicts.pop(0)

    async def fake_removal_record(session, ids):
        return [{"sequence": 8, "claim": "c", "cites": []} for _ in ids]

    async def fake_record_removal(run_id, path, swept):
        dropped.append((path, swept))

    async def fake_gate(sinas, question, answer_id, run_id):
        return True, "", [], [], []

    async def fake_amend(run_id, **kw):
        return None

    async def fake_revise(*a, **k):
        return 0

    monkeypatch.setattr(qr, "AsyncSessionLocal", FakeSession)
    monkeypatch.setattr(qr, "_tele", fake_tele)
    monkeypatch.setattr(qr, "_removal_record", fake_removal_record)
    monkeypatch.setattr(qr, "_record_removal", fake_record_removal)
    monkeypatch.setattr(qr, "_gate_answer", fake_gate)
    monkeypatch.setattr(qr, "_amend_gate_cycle", fake_amend)
    monkeypatch.setattr(qr, "_revise_answer", fake_revise)
    monkeypatch.setattr(faithfulness, "validate_answer_evidence", fake_validate)

    async def run():
        return await qr._pre_publish_sweep(
            "run-1", object(), object(), uuid.uuid4(), "q?")

    return {"state": state, "dropped": dropped, "verdicts": verdicts, "run": run}


@pytest.mark.asyncio
async def test_a_clean_sweep_records_its_own_pass(sweep):
    sweep["verdicts"].append(_verdict())
    assert await sweep["run"]() is True
    v = sweep["state"]["validate"]
    assert v["final_sweeps"] == 1
    assert v["final_sweep_1"] == {
        "judged": 1, "errors": 0,
        "failed": 0, "overreaching": 0, "overreaching_claims": []}


@pytest.mark.asyncio
async def test_two_sweeps_write_two_records(sweep):
    """The defect. The first sweep is the one that spends the repair attempt,
    so its finding is the one worth reading, and it was the one overwritten."""
    sweep["verdicts"].extend([_verdict(over=[3]), _verdict(over=[8])])
    assert await sweep["run"]() is False      # repair attempt spent
    assert await sweep["run"]() is True       # dropped, gate re-asked, published

    v = sweep["state"]["validate"]
    assert v["final_sweeps"] == 2
    assert [c["seq"] for c in v["final_sweep_1"]["overreaching_claims"]] == [3]
    assert [c["seq"] for c in v["final_sweep_2"]["overreaching_claims"]] == [8]


@pytest.mark.asyncio
async def test_the_flat_key_is_not_written_beside_the_numbered_one(sweep):
    """One write, one place. Keeping both is how #103 and #106 drifted."""
    sweep["verdicts"].append(_verdict(over=[3]))
    await sweep["run"]()
    assert "final_sweep_result" not in sweep["state"]["validate"]


@pytest.mark.asyncio
async def test_the_drop_waits_for_the_second_sweep(sweep):
    """`final_sweeps` is the control-flow counter, and numbering the detail
    must not change what decides the drop."""
    sweep["verdicts"].extend([_verdict(over=[3]), _verdict(over=[8])])
    await sweep["run"]()
    assert sweep["dropped"] == []             # first sweep repairs, never drops
    await sweep["run"]()
    assert [p for p, _ in sweep["dropped"]] == ["final_sweep"]
    assert sweep["state"]["validate"]["final_sweep_published_after_drop"] is True


@pytest.mark.asyncio
async def test_a_failing_span_is_recorded_beside_the_overreach(sweep):
    sweep["verdicts"].append(_verdict(over=[8], failed=[2]))
    await sweep["run"]()
    assert sweep["state"]["validate"]["final_sweep_1"]["failed"] == 1


# -- what the sweep examined, not only what it found ---------------------------


@pytest.mark.asyncio
async def test_a_sweep_records_how_many_spans_it_judged(sweep):
    """`failed` and `overreaching` say what the sweep found. Neither says
    whether it looked, and the two readings are the same number."""
    sweep["verdicts"].append(_verdict(judged=12))
    await sweep["run"]()
    assert sweep["state"]["validate"]["final_sweep_1"]["judged"] == 12


@pytest.mark.asyncio
async def test_a_sweep_that_judged_nothing_no_longer_reads_as_a_clean_one(sweep):
    """The defect this record exists for.

    `validate_answer_evidence` returns errors beside failures, and a span that
    errored is skipped before any verdict is written: it is not in `failed`,
    not in `overreaching`, and its row keeps whatever it had. A sweep where
    every span errored therefore returned exactly the numbers a sweep that
    judged everything and objected to nothing returns.
    """
    sweep["verdicts"].append(_verdict(judged=0, errors=9))
    await sweep["run"]()
    r = sweep["state"]["validate"]["final_sweep_1"]
    assert (r["judged"], r["errors"]) == (0, 9)
    assert (r["failed"], r["overreaching"]) == (0, 0)


@pytest.mark.asyncio
async def test_errors_do_not_object_yet(sweep):
    """Recorded, not acted on. Making them block publication is a behaviour
    change on a path that has not fired once in 231 published runs, so it
    waits for the record to say whether it ever does."""
    sweep["verdicts"].append(_verdict(judged=0, errors=9))
    assert await sweep["run"]() is True
