"""What a deletion took out of the answer, as opposed to what it cost.

Two set differences over the finished answer, computed once at publish:

  lost_last_citation  a deleted claim cited a document no surviving claim cites
  lost_last_mention   a deleted claim named an identifier no surviving claim names

Neither is a coverage check and neither is evidence that anything is missing.
The reviser's reasons for the three explained drops in the Q41 run were all
legally sound, and two of them took a source out of the answer anyway. That is
the thing worth recording: an answer that quietly stops citing a publisher's
own material still reads as a good answer, and nothing else in the pipeline
would ever mention it.

What they cannot see is the case that prompted them. Q41 deleted a claim
recording that inspectors imaged employees' drives and indexed them overnight,
because a later judgment superseded the order it came from. T-135/09 and
62009TJ0135.md both survive in other claims of that same answer, so neither
check fires. What left was a fact inside a document still cited, and no set
difference over citations or identifiers reaches it.

The helpers are pure, so no DB.

Run from the backend directory:
`python -m pytest tests/test_what_a_deletion_took.py`
"""

import pathlib

from app.services.query_runner import (
    _deleted_claims,
    _identifiers,
    _last_citation_losses,
    _last_mention_losses,
)


def src():
    return pathlib.Path(
        "app/services/query_runner.py").read_text(encoding="utf-8")


def claim(seq, text, *cites):
    return {"sequence": seq, "claim": text, "cites": list(cites)}


# -- what counts as an identifier ---------------------------------------------
#
# The pattern is structural: a digit somewhere, joined by a separator. It knows
# nothing about courts or articles, which is why it reaches a Spanish case
# number and an EU merger number as well as an EU court reference.


def test_the_shapes_the_corpus_actually_contains():
    for t in ("T-135/09", "C-606/18", "41(1)", "1/2003", "C/1072/19",
              "M.11936", "20(2)", "T-475/14"):
        assert _identifiers(f"as held in {t}, the point stands") == {t}, t


def test_an_article_with_two_subsections_is_one_identifier():
    assert _identifiers("under Article 20(2)(b) of the Regulation") == {"20(2)(b)"}


def test_a_plain_number_is_not_an_identifier():
    """A year, a count, a page. Nothing joins it to anything."""
    assert _identifiers("the 2004 decision ran to 300 pages over 8 days") == set()


def test_a_decimal_is_not_an_identifier():
    """Shares of 17.9% and 7.5% read as identifier-shaped and are not."""
    assert _identifiers("holdings of 17.9% and 7.5% were treated alike") == set()


def test_a_line_range_is_not_an_identifier():
    """`lines 8-8` appears in claims that quote a passage by position."""
    assert _identifiers("the passage at lines 8-8 is a case caption") == set()


def test_a_filename_is_left_to_the_citation_check():
    """It is a citation, and citations are counted exactly rather than by
    pattern. Counting it here would report the same loss twice."""
    assert _identifiers("lines 8-9 of 62013CJ0037.md are a header") == set()


def test_no_text_yields_nothing():
    assert _identifiers("") == set() and _identifiers(None) == set()


# -- gathering the deletions ---------------------------------------------------
#
# Four keys hold deletions and two pairs overlap: the sweep writes its claims
# to both `final_sweep_dropped_detail` and a numbered `removed_N`, and the
# rounds-exhausted path writes both a flat `dropped_detail` and a numbered one.


def test_a_revision_cycle_holds_its_drops_under_its_own_key():
    v = {"revision_1": {"dropped_detail": [claim(3, "a claim about T-135/09")]},
         "revision_2": {"dropped_detail": [claim(9, "another claim")]}}
    assert [e["sequence"] for e in _deleted_claims(v)] == [3, 9]


def test_the_sweeps_two_records_of_one_deletion_count_once():
    e = claim(9, "the rights of defence are safeguarded where")
    v = {"final_sweep_dropped_detail": [e], "removed_1": {"claims": [e]}}
    assert len(_deleted_claims(v)) == 1


def test_the_legacy_flat_key_is_read_too():
    """A run recorded before the numbering existed is still readable. Those
    records carry no citations, so only the mention check says anything."""
    v = {"dropped_detail": [{"sequence": 4, "claim": "held in T-451/20 that"}]}
    assert len(_deleted_claims(v)) == 1


def test_two_deletions_of_different_claims_at_one_sequence_both_count():
    """Sequences are reused across cycles as claims are added and dropped."""
    v = {"revision_1": {"dropped_detail": [claim(3, "the first claim, on scope")]},
         "revision_2": {"dropped_detail": [claim(3, "a different claim, on time")]}}
    assert len(_deleted_claims(v)) == 2


def test_a_cycle_that_dropped_nothing_contributes_nothing():
    """`claims` here is the count of claims the cycle saw, not a list of them.
    Reading the shapes by a fallback chain returned that integer as if it were
    the deletions, which the stored runs found immediately and the fixtures
    did not, because the fixtures had left the count out."""
    v = {"revision_1": {"claims": 14, "dropped": 0, "revised": 2},
         "round_1": {"failed": 1}}
    assert _deleted_claims(v) == []


def test_each_shape_is_read_by_its_own_key():
    """`removed_N` keeps its deletions under `claims` and `revision_N` keeps a
    count there. Only the numbered removal record means a list."""
    v = {"removed_1": {"claims": [claim(9, "a swept claim, on scope")]},
         "revision_1": {"claims": 12, "dropped_detail": [claim(3, "a dropped claim")]}}
    assert sorted(e["sequence"] for e in _deleted_claims(v)) == [3, 9]


def test_a_record_with_no_text_is_not_a_deletion_this_can_read():
    """The count-only records the three paths used to write. Nothing to
    compare, so nothing to report — silently, because they are not errors."""
    v = {"revision_1": {"dropped_detail": [{"sequence": 3}]}}
    assert _deleted_claims(v) == []


def test_unrelated_keys_are_ignored():
    v = {"gate_1": {"parts": []}, "published": "now", "quality_issues": ["x"]}
    assert _deleted_claims(v) == []


def test_no_telemetry_at_all():
    assert _deleted_claims({}) == []


# -- the citation loss ---------------------------------------------------------


def test_a_document_no_surviving_claim_cites_is_reported():
    d = [claim(10, "Advocate General Kokott took the view", "93706-kokott.md")]
    assert _last_citation_losses(d, {"62018CJ0606.md"}) == [
        {"document": "93706-kokott.md", "sequences": [10]}]


def test_a_document_another_claim_still_cites_is_not():
    d = [claim(3, "the T-135/09 order records", "62009TJ0135.md")]
    assert _last_citation_losses(d, {"62009TJ0135.md", "x.md"}) == []


def test_one_document_lost_by_two_deletions_is_one_entry():
    d = [claim(3, "first", "a.md"), claim(7, "second", "a.md")]
    assert _last_citation_losses(d, set()) == [
        {"document": "a.md", "sequences": [3, 7]}]


def test_a_deletion_that_cited_nothing_reports_nothing():
    assert _last_citation_losses([claim(3, "a claim with no evidence")], set()) == []


def test_entries_come_out_in_document_order():
    d = [claim(1, "x", "z.md"), claim(2, "y", "a.md")]
    assert [e["document"] for e in _last_citation_losses(d, set())] == ["a.md", "z.md"]


# -- the mention loss ----------------------------------------------------------


def test_an_identifier_no_surviving_claim_names_is_reported():
    d = [claim(11, "the Commission must observe a reasonable time limit under "
                   "Article 41(1) of the Charter", "62014TJ0449.md")]
    assert _last_mention_losses(d, {"C-606/18"}) == [
        {"identifier": "41(1)", "sequences": [11]}]


def test_an_identifier_a_surviving_claim_still_names_is_not():
    d = [claim(3, "the order in T-135/09 recorded the imaging")]
    assert _last_mention_losses(d, {"T-135/09"}) == []


def test_the_two_checks_do_not_imply_each_other():
    """A deletion can take a document's last citation while the case number
    survives in a claim citing something else, and the reverse. Reporting one
    of them as the other would be a claim about coverage, which neither is."""
    d = [claim(3, "the order in T-135/09 recorded the imaging", "62009TJ0135.md")]
    assert _last_citation_losses(d, set()) != []
    assert _last_mention_losses(d, {"T-135/09"}) == []


def test_the_q41_deletion_that_prompted_this_fires_neither():
    """The reason to build both and the reason neither is a coverage check.
    T-449/14 was held to supersede the T-135/09 order, so the claim went; the
    method it recorded — imaging employees' drives, indexing them overnight —
    went with it. Both the document and the case number survive in other
    claims of the same answer, so no set difference reaches the loss."""
    d = [claim(3, "In Nexans France v Commission (T-135/09), the General Court "
                  "recorded that inspectors took copy-images of employees' hard "
                  "drives and used indexing software overnight to enable a "
                  "keyword search.", "62009TJ0135.md")]
    surviving_docs = {"62009TJ0135.md", "62014TJ0449.md"}
    surviving_ids = {"T-135/09", "T-449/14"}
    assert _last_citation_losses(d, surviving_docs) == []
    assert _last_mention_losses(d, surviving_ids) == []


def test_one_identifier_lost_by_two_deletions_is_one_entry():
    d = [claim(4, "Article 20(1) does not preclude"),
         claim(9, "nor does Article 20(1) require")]
    assert _last_mention_losses(d, set()) == [
        {"identifier": "20(1)", "sequences": [4, 9]}]


def test_a_sequence_that_is_not_a_number_is_dropped_from_the_list():
    """The entry is still reported; only the unusable sequence goes. A loss
    with no readable sequence is still a loss."""
    d = [{"sequence": None, "claim": "held in T-451/20", "cites": []}]
    assert _last_mention_losses(d, set()) == [
        {"identifier": "T-451/20", "sequences": []}]


# -- where it runs -------------------------------------------------------------


def test_both_are_computed_at_publish_and_nowhere_else():
    """"Last" is a claim about the finished answer. During revision a document
    can lose its last citation and get another two cycles later, so the same
    comparison made at deletion time reports losses that did not happen and
    misses ones that had not happened yet."""
    s = src()
    i = s.index("async def _publish_answer")
    body = s[i:i + 2600]
    assert "lost_last_citation=_last_citation_losses(" in body
    assert "lost_last_mention=_last_mention_losses(" in body
    assert s.count("_last_citation_losses(") == 2  # the definition and the one call


def test_the_read_happens_after_the_compaction_commits():
    s = src()
    i = s.index("async def _publish_answer")
    body = s[i:i + 2600]
    assert body.index("await session.commit()") < body.index("select(AnswerClaim.claim_text")


def test_nothing_lost_is_an_empty_list_and_is_still_written():
    """An empty list says the comparison ran and found nothing; a missing key
    says the run predates the comparison. A reader needs to tell those apart,
    so neither helper returns None and neither call site is guarded — unlike
    an empty removal record, which writes no key at all."""
    assert _last_citation_losses([], set()) == []
    assert _last_mention_losses([], set()) == []
    s = src()
    i = s.index("async def _publish_answer")
    body = s[i:i + 2600]
    assert "lost_last_citation=_last_citation_losses(deleted, live_docs)," in body
    assert "lost_last_mention=_last_mention_losses(deleted, live_ids)," in body
