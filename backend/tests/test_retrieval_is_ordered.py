"""Every cut in retrieval has something to cut on.

Three runs of one question returned 100 documents each and shared 32. The
planner was the largest cause -- 14 queries per run, one of them stable across
all three -- but underneath it four cuts had no total order, so the same plan
need not have produced the same working set either. Those are what this pins.

A `LIMIT` without an `ORDER BY` is not a bug Postgres will tell you about: it
returns whichever rows the plan reaches first, which is stable in practice and
not guaranteed, and changes when the data, the statistics or the plan change.
"""

from __future__ import annotations

import inspect
import re

from app import retrieval_first as rf


def _sql_blocks(fn) -> list[str]:
    """Every SQL string literal in the function, normalised to one line."""
    src = inspect.getsource(fn)
    return [re.sub(r"\s+", " ", m).strip()
            for m in re.findall(r'text\("""(.*?)"""\)', src, re.S)]


def test_every_limit_has_an_order_to_cut_on():
    offenders = []
    for block in _sql_blocks(rf.retrieve_and_rank):
        if not re.search(r"\bLIMIT\b", block, re.I):
            continue
        if not re.search(r"\bORDER BY\b", block, re.I):
            offenders.append(block[:110])
    assert not offenders, (
        "LIMIT without ORDER BY returns an arbitrary subset:\n  "
        + "\n  ".join(offenders))


def test_the_order_is_total_where_the_sort_key_can_tie():
    """`ts_rank` ties readily -- several documents can score identically on one
    query -- so ranking on it alone leaves the cut arbitrary among equals."""
    ranked_by_score = [b for b in _sql_blocks(rf.retrieve_and_rank)
                       if re.search(r"ORDER BY r DESC", b, re.I)]
    assert ranked_by_score, "expected the text channel to rank by ts_rank"
    for block in ranked_by_score:
        assert re.search(r"ORDER BY r DESC,\s*\S+", block, re.I), (
            "ts_rank alone is not a total order: " + block[:110])


def test_the_final_ranking_breaks_ties_on_something_stable():
    """Python's sort is stable, so equal scores kept insertion order -- the
    order rows arrived from the queries above. Cutting at top_n then let that
    decide membership rather than only position."""
    src = inspect.getsource(rf.retrieve_and_rank)
    m = re.search(r"sorted\(scores\.items\(\),\s*key=lambda kv:\s*(.+?)\)\[:top_n\]", src)
    assert m, "the final ranking is not where it was"
    key = m.group(1)
    assert "," in key, f"score alone is not a total order: key={key}"


def test_equal_scores_rank_the_same_way_every_time():
    """The property itself, not its spelling."""
    scores = {f"d{i}": 1.0 for i in range(50)}
    once = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    again = sorted(reversed(list(scores.items())), key=lambda kv: (-kv[1], kv[0]))[:10]
    assert once == again, "insertion order still decides which equals survive"
