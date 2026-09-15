"""The rubric's deterministic half, and what it does with a judge's reply.

The model's scores are the model's. What must not drift is everything
around them: how an answer file is read, how a question splits into parts,
what the text alone settles about citations and duplicates, and how a
malformed or incomplete verdict is retried and then refused.

Run from the backend directory: `python -m pytest tests/test_answer_rubric.py`
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.answer_rubric import (
    CRITERIA,
    RubricError,
    _rubric_json,
    _validate,
    apply_caps,
    citation_precheck,
    claims,
    compare,
    duplicate_precheck,
    load_answer,
    question_parts,
    score_answer,
    split_answer,
)

QUESTION = ("Can a decision be annulled in part where the evidence supports "
            "only some of the period it covers, and what then happens to "
            "material already gathered?")

GOOD_ANSWER = """\
**1.** A decision may be annulled in part, as held in Alpha v Authority (Case T-100/09, ECLI:EU:T:2012:596, 14 November 2012), para 91.

**2.** Material gathered outside the surviving scope may not be used, per Alpha v Authority (Case T-100/09, ECLI:EU:T:2012:596, 14 November 2012), para 116.

**3.** The same follows from Regulation No 1/2003, Article 20(4), as in force on 1 May 2004.
"""

FILENAME_ANSWER = """\
**1.** A decision may be annulled in part.
    62009TJ0135.md

**2.** Material gathered outside the surviving scope may not be used.
    62009TJ0135.md, digest--p10.md
"""


def _verdict(score: int = 2, n_parts: int = 2, **overrides) -> dict:
    crit = {k: {"score": score, "reason": f"{k} looks fine"} for k in CRITERIA}
    crit.update(overrides)
    return {"criteria": crit,
            "parts": [{"part": f"p{i}", "score": score, "reason": "covered"}
                      for i in range(n_parts)],
            "verdict": "solid answer"}


# ── reading answers ──────────────────────────────────────────────────────────

def test_the_first_heading_is_the_question_and_the_rest_is_the_body():
    q, body = split_answer("# What is the rule?\n\n**1.** The rule is X.\n")
    assert q == "What is the rule?"
    assert body == "**1.** The rule is X."


def test_an_answer_without_a_heading_has_no_question():
    assert split_answer("**1.** The rule is X.")[0] is None


def test_a_sidecar_question_wins_over_the_heading(tmp_path):
    (tmp_path / "a.md").write_text("# heading question\n\nbody\n")
    (tmp_path / "a.question.txt").write_text("sidecar question\n")
    assert load_answer(tmp_path / "a.md") == ("sidecar question", "body")


def test_no_question_anywhere_is_an_error_not_a_guess(tmp_path):
    (tmp_path / "a.md").write_text("body only\n")
    with pytest.raises(ValueError):
        load_answer(tmp_path / "a.md")


def test_numbered_claims_are_split_and_unnumbered():
    got = claims(GOOD_ANSWER)
    assert len(got) == 3
    assert got[0].startswith("A decision may be annulled in part")


def test_an_unnumbered_answer_falls_back_to_paragraphs():
    assert claims("## Heading\n\nFirst point.\n\nSecond point.") == ["First point.", "Second point."]


# ── question parts ───────────────────────────────────────────────────────────

def test_a_sentence_asking_two_things_splits_at_and_what():
    assert question_parts(QUESTION) == [
        "Can a decision be annulled in part where the evidence supports only "
        "some of the period it covers?",
        "What then happens to material already gathered?",
    ]


def test_each_question_mark_is_a_part():
    parts = question_parts("Does the rule apply? Must documents be sealed on request?")
    assert parts == ["Does the rule apply?", "Must documents be sealed on request?"]


def test_a_single_question_is_one_part():
    assert question_parts("What is the test for independence?") == [
        "What is the test for independence?"]


# ── citation pre-check ───────────────────────────────────────────────────────

def test_complete_citations_leave_the_judge_free():
    c = citation_precheck(GOOD_ANSWER)
    assert c["filename_citations"] == []
    assert c["with_name_and_number"] == 3
    assert c["with_date"] == 3
    assert c["with_ecli"] == 2
    assert c["with_paragraph"] == 2
    assert c["cap"] == 2


def test_filename_stems_cap_the_score_at_zero_when_nothing_is_named():
    c = citation_precheck(FILENAME_ANSWER)
    assert c["filename_citations"] == ["62009TJ0135.md", "digest--p10.md"]
    assert c["with_name_and_number"] == 0
    assert c["cap"] == 0


def test_filenames_beside_named_citations_cap_at_one():
    mixed = GOOD_ANSWER + "\n    62009TJ0135.md\n"
    assert citation_precheck(mixed)["cap"] == 1


def test_a_captioned_regulator_case_counts_as_named():
    c = citation_precheck("**1.** In Case AT.40882 (Fragrances), the authority found X.\n")
    assert c["per_claim"][0]["name"] and c["per_claim"][0]["number"]


def test_a_bare_docket_number_is_not_a_name():
    c = citation_precheck("**1.** In Case T-141/08, the court confirmed X.\n")
    assert c["per_claim"][0]["number"] and not c["per_claim"][0]["name"]


def test_an_instrument_number_is_not_a_year():
    c = citation_precheck("**1.** Regulation No 1/2003 says so.\n")
    assert c["per_claim"][0]["year"] is False


# ── duplicate pre-check ──────────────────────────────────────────────────────

def test_a_restated_holding_is_a_near_duplicate_pair():
    body = ("**1.** The court annulled the decision insofar as it concerned "
            "cables other than high voltage underwater cables and dismissed "
            "the remainder.\n\n"
            "**2.** Something else entirely about the burden of proof.\n\n"
            "**3.** The court's disposition annulled the decision insofar as it "
            "concerned cables other than high voltage underwater cables, and "
            "rejected the remainder.\n")
    d = duplicate_precheck(body)
    assert [p["claims"] for p in d["near_duplicate_pairs"]] == [[1, 3]]
    assert d["cap"] == 1


def test_distinct_claims_do_not_cap():
    d = duplicate_precheck(GOOD_ANSWER)
    assert d["near_duplicate_pairs"] == []
    assert d["cap"] == 2


def test_a_source_cited_from_three_claims_is_reported_not_capped():
    body = ("**1.** The scope must match the grounds, Case T-100/09, para 40.\n\n"
            "**2.** Material outside it cannot be used, Case T-100/09, para 65.\n\n"
            "**3.** Fishing expeditions are unlawful, Case T-100/09, para 91.\n")
    d = duplicate_precheck(body)
    assert d["authorities_cited_thrice_or_more"] == {"T-100/09": [1, 2, 3]}
    assert d["cap"] == 2


# ── the judge's reply ────────────────────────────────────────────────────────

def test_fenced_and_prefixed_replies_parse():
    raw = json.dumps(_verdict())
    for wrapped in (f"```json\n{raw}\n```", f"json {raw}", f"Here:\n{raw}\nDone."):
        assert _rubric_json(wrapped)["verdict"] == "solid answer"


def test_a_reply_with_no_object_raises_a_decode_error():
    with pytest.raises(json.JSONDecodeError):
        _rubric_json("I cannot judge this.")


def test_a_missing_criterion_is_a_rubric_error_naming_it():
    data = _verdict()
    del data["criteria"]["currency_flagged"]
    with pytest.raises(RubricError, match="currency_flagged"):
        _validate(data, ["p0", "p1"])


def test_a_score_outside_0_1_2_is_refused():
    with pytest.raises(RubricError, match="no_off_topic"):
        _validate(_verdict(no_off_topic={"score": 3, "reason": "x"}), ["p0", "p1"])
    with pytest.raises(RubricError):
        _validate(_verdict(no_off_topic={"score": True, "reason": "x"}), ["p0", "p1"])


def test_the_part_count_must_match_the_question():
    with pytest.raises(RubricError, match="parts"):
        _validate(_verdict(n_parts=1), ["p0", "p1"])


def test_a_valid_verdict_keeps_one_line_per_reason():
    data = _verdict(conclusion_first={"score": 1, "reason": "late\nsecond line"})
    out = _validate(data, ["p0", "p1"])
    assert out["criteria"]["conclusion_first"] == {"score": 1, "reason": "late"}
    assert [p["part"] for p in out["parts"]] == ["p0", "p1"]


def test_caps_lower_the_judge_and_say_so():
    judged = {k: {"score": 2, "reason": "fine"} for k in CRITERIA}
    pre = {"citations": {"cap": 0}, "duplicates": {"cap": 2}}
    out = apply_caps(judged, pre)
    assert out["citations_complete"]["score"] == 0
    assert out["citations_complete"]["judge_score"] == 2
    assert "capped at 0" in out["citations_complete"]["reason"]
    assert "cap" not in out["no_duplicate_authorities"]


# ── the whole call, with the model mocked ────────────────────────────────────

def _run(replies: list[str]) -> tuple[dict, list[str]]:
    sent: list[str] = []

    async def invoke(message: str) -> str:
        sent.append(message)
        return replies[len(sent) - 1]

    return asyncio.run(score_answer(QUESTION, GOOD_ANSWER, invoke)), sent


def test_a_clean_verdict_is_scored_once():
    r, sent = _run([json.dumps(_verdict())])
    assert len(sent) == 1
    assert r["total"] == 18 and r["max"] == 18
    assert r["verdict"].startswith("18/18 — solid answer")
    assert [p["part"] for p in r["parts_answered"]] == question_parts(QUESTION)
    assert "PRE-CHECKS" in sent[0] and "per_claim" not in sent[0]


def test_a_malformed_verdict_is_retried_once_with_the_error():
    bad = '{"criteria": {"conclusion_first": {"score": "yes"}}'
    r, sent = _run([bad, json.dumps(_verdict(score=1))])
    assert len(sent) == 2
    assert "was not a valid rubric verdict" in sent[1]
    assert r["total"] == 9


def test_an_incomplete_verdict_is_retried_and_a_second_failure_raises():
    incomplete = json.dumps(_verdict(n_parts=1))
    with pytest.raises(RubricError):
        _run([incomplete, incomplete])


def test_the_filename_cap_applies_to_the_judge_score():
    async def invoke(_msg: str) -> str:
        return json.dumps(_verdict())

    r = asyncio.run(score_answer(QUESTION, FILENAME_ANSWER, invoke))
    assert r["criteria"]["citations_complete"]["score"] == 0
    assert r["total"] == 16


# ── compare ──────────────────────────────────────────────────────────────────

def test_compare_reports_candidate_minus_baseline_per_criterion():
    base = {"q1": {"criteria": {k: {"score": 1} for k in CRITERIA}, "total": 9},
            "q2": {"criteria": {k: {"score": 0} for k in CRITERIA}, "total": 0}}
    cand = {"q1": {"criteria": {k: {"score": 2} for k in CRITERIA}, "total": 18},
            "q3": {"criteria": {k: {"score": 2} for k in CRITERIA}, "total": 18}}
    d = compare(base, cand)
    assert set(d["answers"]) == {"q1"}
    assert d["answers"]["q1"]["conclusion_first"] == 1
    assert d["mean"]["citations_complete"] == 1.0
    assert d["total"] == {"q1": 9}
    assert d["only_baseline"] == ["q2"] and d["only_candidate"] == ["q3"]
