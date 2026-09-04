"""Unit tests for making the gate name what covers each part.

The gate declares a part of the question covered and, until now, pointed at
nothing. It judges parts against the claim TEXT alone — it never sees the
passages the reviser sees, and `cited` reaches it as a flat set of filenames,
so it cannot tell a claim that asserts something about a limb from one that
merely uses the limb's vocabulary.

Q41 is the demonstration. Part 4 asks what rights the company retains over the
copied data; the answer says nothing about receiving a copy, deletion, return,
or challenging the sifting. What it does contain is the word "rights" in a
claim about the rights of defence, "presence" in a description of what happened
in Nexans, and "deleted" in a description of forensic imaging. The gate returned
covered: true, gap: "".

`covered_by` turns that assertion into three questions arithmetic can answer:
does the named claim exist, does it carry evidence, and did the gate itself
already call it merely descriptive.

Nothing here blocks anything. It is recorded so the next batch can say how
often each happens.

Pure: no DB, no network.

Run from the backend directory:
`python -m pytest tests/test_gate_covered_by.py`
"""

import inspect
import pathlib

from app.services.query_runner import _audit_coverage, _coverage_summary, _seq_list

CLAIMS = {1, 2, 3, 7, 10}
WITH_EVIDENCE = {1, 2, 7}
UNRESPONSIVE = [2]


def audit(named):
    return _audit_coverage(_seq_list(named), CLAIMS, WITH_EVIDENCE, UNRESPONSIVE)


# -- reading the sequence numbers ----------------------------------------------


def test_numbers_survive_as_numbers():
    assert _seq_list([3, 7]) == [3, 7]


def test_numbers_written_as_strings_are_read():
    """The gate returns them either way depending on the reply."""
    assert _seq_list(["3", 7, "10"]) == [3, 7, 10]


def test_junk_is_dropped_not_guessed():
    assert _seq_list([3, None, "seven", {}, [], 7]) == [3, 7]


def test_a_repeat_means_nothing():
    assert _seq_list([3, 3, 7, 3]) == [3, 7]


def test_order_is_kept():
    """It is the gate's order of importance, not ours to sort."""
    assert _seq_list([10, 1, 7]) == [10, 1, 7]


def test_nothing_named_is_an_empty_list():
    assert _seq_list(None) == [] and _seq_list([]) == []


# -- what the named claims turn out to be --------------------------------------


def test_a_part_naming_sound_claims_is_clean():
    a = audit([1, 7])
    assert a == {"covered_by": [1, 7], "covered_by_missing": [],
                 "covered_by_unsupported": [], "covered_by_unresponsive": []}


def test_a_claim_that_is_not_in_the_answer_is_missing():
    """The gate pointing at something that is not there. Worse than pointing at
    nothing, and kept apart from it for that reason."""
    a = audit([7, 42])
    assert a["covered_by_missing"] == [42]
    assert a["covered_by_unsupported"] == [] and a["covered_by_unresponsive"] == []


def test_a_missing_claim_is_not_also_counted_as_unsupported():
    """It cannot be judged on evidence it does not have, because it does not
    exist. Reporting it twice would double-count one defect."""
    a = audit([42])
    assert a["covered_by_missing"] == [42]
    assert a["covered_by_unsupported"] == []


def test_a_claim_with_no_evidence_is_unsupported():
    """Claim 3 and claim 10 exist and carry no bound passage: a part resting on
    them is a part resting on an assertion."""
    assert audit([3, 10])["covered_by_unsupported"] == [3, 10]


def test_a_claim_the_gate_called_descriptive_is_flagged():
    """`unresponsive` is the one finding class the gate records and then lets
    through: it goes to `issues`, which never block. A part covered only by
    those is the shape of "claims 1 and 2 are framing only"."""
    assert audit([2])["covered_by_unresponsive"] == [2]


def test_the_three_are_independent():
    a = audit([2, 3, 42])
    assert a["covered_by_missing"] == [42]
    assert a["covered_by_unsupported"] == [3]
    assert a["covered_by_unresponsive"] == [2]


def test_naming_nothing_leaves_every_list_empty():
    """The gate declining to point. Recorded as such, not as a defect of the
    claims, because there are none to judge."""
    a = audit([])
    assert a["covered_by"] == []
    assert not any(a[k] for k in a if k != "covered_by")


# -- the per-cycle summary -----------------------------------------------------


def part(covered=True, by=None, missing=None, unsup=None, unresp=None):
    return {"covered": covered, "covered_by": by or [],
            "covered_by_missing": missing or [],
            "covered_by_unsupported": unsup or [],
            "covered_by_unresponsive": unresp or []}


def test_the_summary_counts_only_covered_parts():
    """An uncovered part already blocks publication and needs no audit."""
    s = _coverage_summary([part(), part(covered=False)])
    assert s["parts"] == 2 and s["covered"] == 1


def test_a_covered_part_naming_nothing_is_counted():
    assert _coverage_summary([part(by=[])])["naming_nothing"] == 1


def test_a_covered_part_naming_a_ghost_is_counted():
    assert _coverage_summary([part(by=[42], missing=[42])])["naming_missing"] == 1


def test_only_unsupported_means_every_named_claim():
    """A part covered by three claims of which one lacks evidence is not the
    finding. One where all of them do is."""
    some = part(by=[1, 3], unsup=[3])
    all_ = part(by=[3, 10], unsup=[3, 10])
    assert _coverage_summary([some])["only_unsupported"] == 0
    assert _coverage_summary([all_])["only_unsupported"] == 1


def test_only_unresponsive_means_every_named_claim():
    assert _coverage_summary([part(by=[1, 2], unresp=[2])])["only_unresponsive"] == 0
    assert _coverage_summary([part(by=[2], unresp=[2])])["only_unresponsive"] == 1


def test_a_part_naming_a_ghost_is_not_also_only_unsupported():
    """`missing` and `only_*` describe different failures and a part with a
    ghost in its list has not been shown to rest on assertion."""
    s = _coverage_summary([part(by=[42], missing=[42])])
    assert s["naming_missing"] == 1
    assert s["only_unsupported"] == 0 and s["only_unresponsive"] == 0


def test_no_parts_summarises_to_zeros_not_an_error():
    s = _coverage_summary([])
    assert s["parts"] == 0 and s["covered"] == 0 and s["naming_nothing"] == 0


# -- wiring --------------------------------------------------------------------


def src() -> str:
    from app.services import query_runner as qr

    return pathlib.Path(inspect.getfile(qr)).read_text(encoding="utf-8")


def test_both_reply_schemas_ask_for_it():
    """The fixed decomposition and the fallback the gate derives itself."""
    s = src()
    assert s.count('"covered_by": [<the sequence numbers of the claims ') == 2


def test_both_branches_audit_what_came_back():
    s = src()
    assert s.count("_audit_coverage(") == 3      # the definition and two branches


def test_the_audit_reaches_the_telemetry():
    s = src()
    for k in ("covered_by", "covered_by_missing", "covered_by_unsupported",
              "covered_by_unresponsive"):
        assert f'"{k}": x.get("{k}")' in s, k
    assert "coverage=_coverage_summary(parts))" in s
    assert "gate_coverage=coverage)" in s


def test_nothing_here_changes_what_publishes():
    """The whole point is to watch the judgment, not to act on it yet."""
    s = src()
    assert 'publishable = bool(data.get("publishable")) and not uncovered' in s
    for k in ("covered_by_missing", "covered_by_unsupported", "covered_by_unresponsive"):
        i = s.index("publishable = bool(")
        assert k not in s[i:i + 400], k


def test_the_unresponsive_numbers_are_derived_once():
    """The parts read them and the `issues` line reads them. Two derivations of
    one field is how they drift apart."""
    s = src()
    assert s.count("_seq_list(data.get(\"unresponsive\"))") == 1
    assert "seqs = unresponsive_seqs" in s


def test_a_ghost_keeps_a_part_out_of_only_unsupported_by_arithmetic():
    """Not by a separate guard. A missing sequence never enters the unsupported
    or unresponsive lists, so the length equality `only` tests cannot hold while
    one is present. Pinned because the guard that used to be here could never
    fire, and the next reader will want to know why it is absent."""
    p = part(by=[3, 42], missing=[42], unsup=[3])
    assert len(p["covered_by_unsupported"]) != len(p["covered_by"])
    assert _coverage_summary([p])["only_unsupported"] == 0
    assert _coverage_summary([p])["naming_missing"] == 1
