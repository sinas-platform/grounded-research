"""A source the planner names in full resolves to the entity that most
resembles the name when no stored name or alias contains it.

The planner writes a source as it would cite it in a brief — "Kestrel
Holdings v Northmoor Authority (T-123/45 P)" — and no stored name or alias
CONTAINS a caption that long, so every such source resolved to nothing and
the graph channel walked from nowhere. Measured on 21 September 2026 on one
question's hypotheses: four sources named in full, all four present in the
collection, none resolved. Trigram resemblance over the same indexed
columns put the right entity first for all four (0.75 to 0.94), and an
invented caption resolved to nothing.

Ties are broken by RECOGNISED documents, the name itself before an alias,
an exact name before both. Thirty-four entities carry an alias equal to one
regulation's name — the articles that cite it — thirteen of them recognised
nowhere; ordered by raw mention counts those articles came before the
regulation.

What these pin:

- containment is tried first, resemblance only when containment finds
  nothing, and the resemblance floor is set for that transaction only;
- both branches of the resemblance query use the trigram operator on the
  indexed columns, not a similarity() in the WHERE;
- ties: exact name, then name before alias, then recognised documents.
"""

from __future__ import annotations

import inspect

import pytest
from app import retrieval_first as rf


class _Session:
    def __init__(self, first, second=()):
        self.answers = [list(first), list(second)]
        self.calls = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params))
        if "set_config" in sql:
            return []
        return _Rows(self.answers.pop(0))


class _Rows(list):
    def all(self):
        return list(self)


@pytest.mark.anyio
async def test_containment_first_and_nothing_else_when_it_finds_something():
    s = _Session(first=[("e1", "Kestrel Holdings v Northmoor Authority", "case", 3, 0.9)])
    rows = await rf._entities_matching(s, "Kestrel Holdings")
    assert [r[0] for r in rows] == ["e1"]
    assert len(s.calls) == 1
    sql, params = s.calls[0]
    assert "ILIKE :pat" in sql and params["pat"] == "%Kestrel Holdings%"


@pytest.mark.anyio
async def test_resemblance_when_nothing_contains_the_caption():
    caption = "Kestrel Holdings v Northmoor Authority (T-123/45 P)"
    s = _Session(first=[], second=[("e7", "T-123/45 P Kestrel Holdings v Northmoor", "case", 2, 0.8)])
    rows = await rf._entities_matching(s, caption)
    assert [r[0] for r in rows] == ["e7"]
    sqls = [c[0] for c in s.calls]
    assert len(sqls) == 3, "containment, the local floor, resemblance"
    assert "set_config('pg_trgm.similarity_threshold', :t, true)" in sqls[1]
    assert s.calls[1][1] == {"t": str(rf.RESEMBLANCE_FLOOR)}
    assert "e.canonical_form % :key" in sqls[2] and "a.alias % :key" in sqls[2]
    assert "ILIKE" not in sqls[2]


def test_the_resemblance_floor_is_a_similarity():
    assert 0 < rf.RESEMBLANCE_FLOOR < 1


def test_ties_break_exact_name_then_name_before_alias_then_recognised():
    src = inspect.getsource(rf._entities_matching_sql)
    assert "coalesce(st.recognised_documents, 0) AS docs" in src
    assert "coalesce(st.documents" not in src, "raw mention counts rank citing articles above the regulation"
    assert "0 AS via" in src and "1" in src
    assert "ORDER BY (lower(value) = lower(:key)) DESC, via, docs DESC, id" in src


def test_the_lookup_still_returns_only_the_closest_ties():
    src = inspect.getsource(rf._entities_matching_sql)
    assert "WHERE closeness = top" in src and "LIMIT 6" in src
