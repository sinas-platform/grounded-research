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

No DB and no network: the helper is pure and the call sites are read as source.

Run from the backend directory:
`python -m pytest tests/test_sweep_cycle_history.py`
"""

import inspect

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


# -- the call site -------------------------------------------------------------


def _sweep_src() -> str:
    return inspect.getsource(qr._pre_publish_sweep)


def test_the_sweep_writes_a_numbered_key():
    src = _sweep_src()
    assert '_next_cycle_key(run_id, "validate", "final_sweep")' in src


def test_the_flat_result_key_is_no_longer_written():
    """One write, one place. Keeping both is how #103 and #106 drifted.

    Asserts on the assignment rather than the name: the comment above the call
    site names the old key to say what changed, and should stay free to."""
    assert "final_sweep_result=" not in _sweep_src()


def test_the_control_flow_counter_is_untouched():
    """`final_sweeps` is read back to decide whether the repair chance is
    spent. Numbering the detail must not change what decides the drop."""
    src = _sweep_src()
    assert 'get("final_sweeps") or 0' in src
    assert "final_sweeps=sweeps + 1" in src
    assert "if sweeps >= 1:" in src
