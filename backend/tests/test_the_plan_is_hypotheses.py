"""The plan is what the answer might say, and retrieval runs it.

Two independent runs of one question at temperature zero named different
entities and wrote different queries: 0% shared queries, half-shared
documents, measured. What is stable across runs is what the question
asks. So the plan is hypotheses — the issues the question contains and
the rule expected to govern each — sampled twice and merged, and each rule
runs the same deterministic searches, fused by reciprocal rank, then a
walk of the graph from the fused top. Measured on 14,511 documents, two
questions, four runs each: every document a review of published answers had asked for
inside the top 15 for one question, seven of nine inside the top 30 for
the other, in every run; the second sample raised the shared top-30
between runs from about 21 to 24 of 30.

What these pin:

- the reply flattens to rules, each with its issue and its source, and
  nothing that is not a rule survives;
- the union keeps sample order and one copy of a rule written in the same
  words, while the same rule in other words is two rules;
- fusion is reciprocal rank at the standard constant and breaks ties on
  the document id;
- two samples, the channels per rule, the question's words as a list of
  its own, the summary below the rest, and the walk from the fused top.
"""

from __future__ import annotations

import inspect
import re

from app import hypotheses as h


def test_the_reply_flattens_to_rules_with_issue_and_source():
    reply = {"issues": [
        {"issue": "  Whether the  first thing holds ", "rules": [
            {"rule": "The first  rule.", "source": {"identifier": "T-123/45", "title": "Kestrel v Northmoor"}},
            {"rule": "", "source": {}},
            "not a rule",
            {"rule": "The second rule.", "source": "not an object"}]},
        "not an issue",
        {"issue": "Another", "rules": None}]}
    rules = h.rules_of(reply)
    assert [r["rule"] for r in rules] == ["The first rule.", "The second rule."]
    assert rules[0]["issue"] == "Whether the first thing holds"
    assert rules[0]["source"] == {"identifier": "T-123/45", "title": "Kestrel v Northmoor"}
    assert rules[1]["source"] == {"identifier": "", "title": ""}


def test_the_union_keeps_order_and_one_copy_of_the_same_words():
    a = [{"rule": "A rule, stated."}, {"rule": "Another rule."}]
    b = [{"rule": "a rule stated"}, {"rule": "The other rule, in other words."}]
    merged = h.union_rules([a, b])
    assert [r["rule"] for r in merged] == ["A rule, stated.", "Another rule.", "The other rule, in other words."]
    assert h.union_rules([[], []]) == []


def test_fusion_is_reciprocal_rank_with_stable_ties():
    fused = h.rrf([(1.0, ["a", "b", "c"]), (1.0, ["b", "a"]), (0.3, ["d"])])
    ids = [d for d, _ in fused]
    assert ids[:2] == ["a", "b"] and ids[-1] == "d"
    tie = h.rrf([(1.0, ["y", "x"]), (1.0, ["x", "y"])])
    assert [d for d, _ in tie] == ["x", "y"], "equal scores rank by id"
    assert abs(fused[0][1] - (1 / 61 + 1 / 62)) < 1e-9


def test_two_samples_and_the_standard_constant():
    assert h.SAMPLES == 2
    assert h.RRF_K == 60
    assert 0 < h.SUMMARY_WEIGHT < 1
    assert h.CORE >= 1


def test_the_rank_fuses_every_rule_and_walks_from_the_fused_top():
    src = inspect.getsource(h.rank)
    for label in ("rule {k}: propositions", "rule {k}: full text", "rule {k}: graph",
                  "rule {k}: summary", "the question's words", "walk from the retrieved core"):
        assert label in src, label
    assert "SUMMARY_WEIGHT" in src
    assert src.index("rrf(lists)[:CORE]") < src.index("walk from the retrieved core")


def test_the_plan_samples_merges_and_resolves_each_rules_source():
    src = inspect.getsource(h.plan)
    assert "for i in range(SAMPLES)" in src
    assert "union_rules(samples)" in src
    assert "_resolve_names([r[\"source\"]])" in src
    assert '"queries": []' in src, "the rules are the queries"


def test_the_prompt_asks_for_issues_rules_and_sources_and_nothing_of_a_domain():
    p = h.HYPOTHESES_PROMPT
    assert '"issues"' in p and '"rule"' in p and '"source"' in p
    for word in ("case", "court", "legal", "judgment", "statute"):
        assert word not in p.lower(), word


def test_the_full_text_channel_selects_candidates_by_rare_words_then_ranks_on_all():
    """Ranking every document carrying any word of a rule read a gigabyte a
    second for eleven minutes per rule; candidates through the index, on
    the rare words, then a rank of those on all the words, takes a minute
    and kept the review's documents. The common words are the collection's
    own statistics, not a list."""
    assert h.rare_words("the | kestrel | of | northmoor", {"the", "of"}) == "kestrel | northmoor"
    assert h.rare_words("the | of", {"the", "of"}) == ""
    src = inspect.getsource(h._text_words)
    assert "rare_words(terms, common)" in src
    assert "ORDER BY n DESC, id LIMIT :c" in src, "candidates by how many rare words, cut on a total order"
    assert "ts_rank(dv.content_tsvector, to_tsquery('simple', :t), 1)" in src, "ranked on all the words"
    assert "pg_stats" in h._COMMON_TERMS_SQL and "most_common_elems" in h._COMMON_TERMS_SQL
    assert h.TEXT_CANDIDATES >= 1000


def test_every_word_channel_selects_candidates_the_same_way():
    """Measured with the full-text channel alone two-staged, twelve rules to
    one question: propositions 50 to 72 seconds a rule, summaries 40 to 45,
    full text 34 to 51 — the two that ranked everything they touched cost
    the same as the one that used to. Each word channel now takes its
    candidates through its index on the rule's rare words, and a rule with
    no rare word keeps the one-stage path."""
    for fn in (h._proposition_words, h._text_words, h._summary_words):
        src = inspect.getsource(fn)
        assert "rare_words(terms, common)" in src, fn.__name__
        assert "if not rare:" in src, fn.__name__
        assert "ORDER BY n DESC, id LIMIT :c" in src, fn.__name__
        assert "LATERAL" in src, fn.__name__
    # The proposition channel's candidates are propositions, not documents:
    # counted per document, the cut lost two of seven of the review's documents.
    src = inspect.getsource(h._proposition_words)
    assert "SELECT p.id FROM proposition p" in src
    assert "JOIN proposition p ON p.id = c.id" in src


def test_the_summary_is_matched_on_the_expression_its_index_is_built_on():
    """An expression index serves a query only when the planner sees the
    same expression: the channel and migration 0053 write one string."""
    import importlib.util
    from pathlib import Path

    path = Path(h.__file__).parent / "alembic" / "versions" / "0053_summary_word_index.py"
    spec = importlib.util.spec_from_file_location("m0053", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    migration = inspect.getsource(m.upgrade)
    assert "to_tsvector('simple', coalesce(summary, ''))" in migration
    src = inspect.getsource(h._summary_words)
    assert src.count("to_tsvector('simple', coalesce(d.summary, ''))") >= 3
    assert "USING gin" in migration


def test_a_source_several_rules_name_is_walked_once():
    """Two samples of one question name the same sources over and over and
    the walk's second hop fans out to two hundred entities: walked once
    per distinct anchor set, counted once per rule that names it."""
    src = inspect.getsource(h.rank)
    assert "walked: dict[frozenset[str], list[str]] = {}" in src
    assert "if key not in walked:" in src
    assert "await graph_of(s, r.get(\"anchors\") or [])" in src
