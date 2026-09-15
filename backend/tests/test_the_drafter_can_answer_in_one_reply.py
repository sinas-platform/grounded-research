"""The drafter is asked for what only it can decide, and no more.

Two full runs at medium effort ended identically: retrieval, planning and
extraction all succeeded — 50 documents read, 12 passage groups verified —
and the drafting call came back with NOTHING. Not malformed JSON: an empty
assistant reply, zero text blocks, on a 20,772-character prompt and again on
a 46,091-character one. The provider's accounting says where it went: both
calls spent 20,000 completion tokens, exactly the agent's ceiling, and
emitted no text at all, while the argument-plan call in the same runs spent
13,405 to deliver a 6,606-character reply. The model ran out of room before
it could start writing.

The repair path then made it worse. It hands the model its previous reply and
asks for the same claims as valid JSON; the previous reply was empty, so the
model was shown nothing and — correctly, for what it was shown — answered
`{"claims": []}`. The run reported "no passage supported a claim well enough
to draft", a verdict about the corpus that no model had ever given.

So the schema shrank. Six things the drafter used to author are derived by
the engine from what it already had: the section (from the kind), the
authority label (from the cited document's class, as the deployment declares
it), the tier (from the authority annotation), the jurisdiction note and the
currency note (from the retrieved set), and the position (from the order).
What remains is what only a reader of the passages can decide.

These tests pin both halves: a realistic reply in the new shape round-trips
into the columns the answer is built from, and a reply that carries no claims
is surfaced as a named failure of the drafter rather than as a verdict on the
passages.

Run from the backend directory:
`python -m pytest tests/test_the_drafter_can_answer_in_one_reply.py`
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest
from app.services import answer_structure as st
from app.services import query_runner as qr

PARTS = [
    {"index": 0, "label": "Whether it applies", "text": "Whether it applies"},
    {"index": 1, "label": "Where the line falls", "text": "Where the line falls"},
]

#: A reply in the shape the prompt now asks for: no `section`, no
#: `authority_label`, no `jurisdiction_note`, no `currency_note`, and a test
#: whose conditions sit flat on the claim.
REPLY = {
    "claims": [
        {"n": 1, "text": "It applies, and the line falls at the employment "
                         "relationship.", "part": None, "kind": "conclusion",
         "follows_from": [3, 5], "rationale": "Answers the whole question.",
         "evidence": [{"filename": "a.md", "line_from": 16, "line_to": 16,
                       "locator": "44"}]},
        # A part's conclusion that cites nothing and names what it rests on.
        # This is what the live model actually sends for a per-part
        # conclusion, and it has to survive as a derived claim rather than
        # be read as a claim whose evidence went missing.
        {"n": 2, "text": "It applies.", "part": 1, "kind": "conclusion",
         "follows_from": [3], "rationale": "Answers part 1.",
         "evidence": []},
        {"n": 3, "text": "The governing rule is stated in the decision.",
         "part": 1, "kind": "legal_principle", "rationale": "The rule.",
         "evidence": [{"filename": "a.md", "line_from": 16, "line_to": 16,
                       "locator": "44"}]},
        {"n": 4, "text": "The test has two limbs.", "part": 2, "kind": "test",
         "test_name": "The two-limb test", "rationale": "Sets out the test.",
         "conditions": [
             {"text": "the first limb", "cumulative": True,
              "evidence": {"filename": "b.md", "line_from": 149,
                           "line_to": 153, "locator": "1"}},
             {"text": "the second limb", "cumulative": True,
              "evidence": {"filename": "b.md", "line_from": 154,
                           "line_to": 158, "locator": "2"}}],
         "evidence": [{"filename": "b.md", "line_from": 149, "line_to": 158,
                       "locator": None}]},
        {"n": 5, "text": "It follows that the line falls there.", "part": 2,
         "kind": "inference", "follows_from": [4],
         "rationale": "The step.", "evidence": []},
    ]
}


def _prompt_text(fn) -> str:
    """The prompt as the model receives it, not as the source wraps it."""
    src = inspect.getsource(fn)
    return "".join(
        node.value
        for node in ast.walk(ast.parse(textwrap.dedent(src)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str))


# ── the reply round-trips ────────────────────────────────────────────────────

def test_every_claim_in_a_realistic_reply_becomes_a_row():
    """The whole point. A reply in the shape the prompt asks for must lose
    nothing on the way into the columns the answer is rendered from."""
    rows = [st.normalise_claim(c, PARTS) for c in REPLY["claims"]]
    assert all(r is not None for r in rows)
    assert [r["claim_text"] for r in rows] == [c["text"] for c in REPLY["claims"]]


def test_the_section_follows_from_the_kind_without_being_asked_for():
    rows = [st.normalise_claim(c, PARTS) for c in REPLY["claims"]]
    assert [r["section"] for r in rows] == [
        "conclusion", "conclusion", "analysis", "analysis", "analysis"]
    # And the mapping stands on its own, for every kind the contract names.
    assert st.section_of("conclusion") == "conclusion"
    assert st.section_of("abstention") == "conclusion"
    assert st.section_of("label") == "authority"
    for kind in ("legal_principle", "factual", "procedural", "test",
                 "inference"):
        assert st.section_of(kind) == "analysis", kind
    # An unknown kind is analysis, not a crash and not a conclusion.
    assert st.section_of(None) == "analysis"
    assert st.section_of("something else") == "analysis"


def test_a_claim_no_longer_carries_what_its_source_is():
    """Those four columns exist and are filled by the engine. The
    normaliser's job is to leave them empty, so a model that volunteers one
    anyway cannot put a word into the answer about a source."""
    volunteered = dict(REPLY["claims"][0],
                       authority_label="court_judgment",
                       jurisdiction_note="somewhere else",
                       currency_note="repealed", section="analysis")
    row = st.normalise_claim(volunteered, PARTS)
    assert row["authority_label"] is None
    assert row["jurisdiction_note"] is None
    assert row["currency_note"] is None
    # and the section it tried to file itself under is ignored
    assert row["section"] == "conclusion"


def test_a_test_arrives_flat_and_is_stored_with_its_conditions_in_order():
    row = st.normalise_claim(REPLY["claims"][3], PARTS)
    assert row["claim_kind"] == "test"
    assert row["test"]["name"] == "The two-limb test"
    assert [c["text"] for c in row["test"]["conditions"]] == [
        "the first limb", "the second limb"]
    assert all(c["cumulative"] for c in row["test"]["conditions"])


def test_each_condition_keeps_its_own_passage():
    spans = st.condition_spans(st.raw_test(REPLY["claims"][3]))
    assert [s["filename"] for s in spans] == ["b.md", "b.md"]
    assert [s["line_from"] for s in spans] == [149, 154]
    assert [s["note"] for s in spans] == ["condition 1", "condition 2"]


def test_the_older_nested_test_shape_still_reads():
    """A model that nests `test` anyway is not a malformed reply, and an
    older reply replayed through this code is not either."""
    nested = {"text": "x", "kind": "test",
              "test": {"name": "T", "conditions": [{"text": "a"},
                                                   {"text": "b"}]}}
    row = st.normalise_claim(nested, PARTS)
    assert row["test"]["name"] == "T"
    assert len(row["test"]["conditions"]) == 2


def test_a_conclusion_that_cites_nothing_is_derived_not_unsupported():
    c = REPLY["claims"][1]
    assert qr._evidence_entries(c) == []
    assert st.is_derived("conclusion", has_evidence=False,
                         follows_from=c["follows_from"])


def test_the_reasoning_chain_survives_the_round_trip():
    rows = [st.normalise_claim(c, PARTS) for c in REPLY["claims"]]
    assert rows[0]["follows_from_refs"] == [3, 5]
    assert rows[4]["follows_from_refs"] == [4]
    assert rows[2]["follows_from_refs"] == []


def test_the_claims_are_ordered_conclusions_first_then_part_by_part():
    rows = [st.normalise_claim(c, PARTS) for c in REPLY["claims"]]
    ordered = st.order_claims(rows)
    assert [r["claim_text"] for r in ordered][0].startswith("It applies, and")
    assert [r["section"] for r in ordered] == [
        "conclusion", "conclusion", "analysis", "analysis", "analysis"]
    # position is assigned here, which is why the drafter is not asked for it
    assert all(isinstance(r["position"], int) for r in ordered)


def test_the_locator_the_drafter_read_off_the_passage_is_kept():
    ev = qr._evidence_entries(REPLY["claims"][0])
    assert ev == [{"filename": "a.md", "line_from": 16, "line_to": 16,
                   "locator": "44"}]


# ── the prompt and the parser agree ──────────────────────────────────────────

def test_the_prompt_asks_for_exactly_the_fields_the_engine_reads():
    prompt = _prompt_text(qr._draft_from_extracts)
    for field in ('"n"', '"text"', '"part"', '"kind"', '"follows_from"',
                  '"rationale"', '"evidence"', '"conditions"', '"locator"'):
        assert field in prompt, field


def test_the_prompt_no_longer_asks_for_what_the_engine_derives():
    """Every one of these was a decision the engine could already make, and
    output the drafter had to spend before it could write a claim."""
    prompt = _prompt_text(qr._draft_from_extracts)
    for field in ('"section"', '"authority_label"', '"jurisdiction_note"',
                  '"currency_note"', '"position"'):
        assert field not in prompt, field


def test_the_reviser_is_asked_for_the_same_shape_as_the_drafter():
    """Two prompts that disagree ask for one answer in two shapes, and the
    revision path writes the same columns the drafting path does."""
    prompt = _prompt_text(qr._revise_answer)
    for field in ('"section"', '"authority_label"', '"jurisdiction_note"',
                  '"currency_note"'):
        assert field not in prompt, field


def test_the_structure_rules_no_longer_dictate_a_section():
    rules = qr._structure_rules(PARTS, 12)
    assert '"section"' not in rules
    assert '"conclusion"' in rules


def test_a_source_line_says_what_the_document_is_and_nothing_to_copy_back():
    """It used to carry the tier, the issuing body, the date, the
    jurisdiction and a currency note, all of which existed so the drafter
    could write them back out as fields."""
    row = {"title": "A Decision", "class": "Court Decision",
           "annotation_values": {"authority_tier": {"depth": 1},
                                 "issuing_body": "A Body"},
           "props": {"decision_date": "2021-05-04", "jurisdiction": "Here"}}
    line = st.source_context_line(row)
    assert line == "title: A Decision; class: Court Decision"
    assert st.source_context_line(row, "commentary").endswith(
        "labelled: commentary")


# ── what the engine fills in instead ─────────────────────────────────────────

def test_the_label_comes_from_the_class_the_deployment_declared():
    rows = [{"filename": "a.md", "title": "A", "class": "Court Decision",
             "class_authority_label": None,
             "annotation_values": {"authority_tier": {"depth": 1}}},
            {"filename": "b.md", "title": "B", "class": "Commentary",
             "class_authority_label": "commentary",
             "annotation_values": {}}]
    ctx = qr._source_context(rows)
    assert ctx["a.md"]["label"] is None
    assert ctx["a.md"]["tier"] == 1
    assert ctx["b.md"]["label"] == "commentary"
    # and the drafter is shown the label, because it decides what may rest
    # on the source — not because it has to write it down
    assert "labelled: commentary" in ctx["b.md"]["line"]
    assert "labelled" not in ctx["a.md"]["line"]


def test_a_jurisdiction_note_is_the_minority_one_in_the_retrieved_set():
    rows = [{"filename": f"{i}.md", "props": {"jurisdiction": "Wide"}}
            for i in range(3)]
    rows.append({"filename": "odd.md", "props": {"jurisdiction": "Narrow"}})
    notes = st.jurisdiction_notes(rows)
    assert notes == {"odd.md": "jurisdiction: Narrow"}


def test_no_note_where_every_source_is_in_the_same_jurisdiction():
    """A note every claim carries is a note that says nothing."""
    rows = [{"filename": f"{i}.md", "props": {"jurisdiction": "Wide"}}
            for i in range(4)]
    assert st.jurisdiction_notes(rows) == {}
    assert st.jurisdiction_notes([{"filename": "a.md", "props": {}}]) == {}


# ── a silent drafter is a named failure ──────────────────────────────────────

class _Sinas:
    """Enough of the client for the drafting call: it records what it was
    asked and returns what the test tells it to."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def invoke(self, agent: str, message: str) -> str:
        self.prompts.append(message)
        return self.replies[len(self.prompts) - 1] if (
            len(self.prompts) <= len(self.replies)) else ""


EXTRACTS = [{"part": 0, "passages": [
    {"filename": "a.md", "line_from": 16, "line_to": 16,
     "text": "The passage."}]}]
SOURCES = {"a.md": {"line": "title: A; class: Court Decision", "label": None,
                    "tier": 1, "jurisdiction": None, "currency": None}}


def _draft(sinas, monkeypatch, telemetry: dict):
    async def _tele(run_id, stage, **detail):
        telemetry.setdefault(stage, {}).update(detail)

    async def _playbook(role="drafting"):
        return ""

    monkeypatch.setattr(qr, "_tele", _tele)
    monkeypatch.setattr(qr, "_synthesis_playbook", _playbook)
    return asyncio.run(qr._draft_from_extracts(
        None, None, sinas, "A question?", EXTRACTS, cap=12, parts=PARTS,
        sources=SOURCES))


def test_an_empty_reply_is_retried_with_the_work_not_with_nothing(monkeypatch):
    """The old repair prompt was 250 characters and carried an empty
    PREVIOUS REPLY. The retry has to carry the passages, or there is nothing
    for the model to draft from."""
    sinas = _Sinas("", "")
    tele: dict = {}
    with pytest.raises(qr.DrafterSilent):
        _draft(sinas, monkeypatch, tele)
    assert len(sinas.prompts) == 2
    retry = sinas.prompts[1]
    assert "The passage." in retry, "the retry must carry the passages again"
    assert "at most 6 claims, not 12" in retry
    assert len(retry) > len(sinas.prompts[0])


def test_an_empty_reply_is_recorded_as_one(monkeypatch):
    sinas = _Sinas("", "")
    tele: dict = {}
    with pytest.raises(qr.DrafterSilent) as exc:
        _draft(sinas, monkeypatch, tele)
    assert exc.value.cause == qr.DrafterSilent.SILENT
    assert tele["draft"]["draft_empty_reply"] is True
    assert tele["draft"]["draft_reply_chars"] == 0
    assert tele["draft"]["draft_retry"] == "reduced_ask"
    assert tele["draft"]["draft_prompt_chars"] > 0
    assert tele["draft"]["draft_retry_empty"] is True


def test_a_malformed_reply_is_still_repaired_rather_than_redrafted(monkeypatch):
    """The two failures are different and get different retries. A reply
    with claims in it that broke on a quote is worth repairing."""
    sinas = _Sinas('{"claims": [{"text": "He said "yes".", "evidence": []}]}',
                   "")
    tele: dict = {}
    with pytest.raises(qr.DrafterSilent):
        _draft(sinas, monkeypatch, tele)
    assert "was not valid JSON" in sinas.prompts[1]
    assert tele["draft"]["draft_retry"] == "repair_json"
    assert tele["draft"]["draft_empty_reply"] is False


def test_the_run_reports_the_drafter_not_the_passages():
    """The failure the two lost runs were recorded under. A drafter that
    said nothing is not a corpus that supports no claim, and the cause on
    the run has to say which happened."""
    src = inspect.getsource(qr._stage_synthesize)
    assert "except DrafterSilent as exc:" in src
    assert "PartialOutcome(exc.cause, exc.explanation)" in src
    # the three causes are distinct and none of them is the old message
    causes = {qr.DrafterSilent.SILENT, qr.DrafterSilent.NO_CLAIMS,
              qr.DrafterSilent.UNUSABLE}
    assert len(causes) == 3
    assert "no_progress" not in causes


def test_an_empty_claim_list_is_its_own_cause():
    """`{"claims": []}` parses. It is a judgment, and a different one from
    "no passage supported a claim well enough to draft" — which is what the
    run said when the model had never replied at all."""
    src = inspect.getsource(qr._draft_from_extracts)
    tail = src[src.index("if written == 0:"):]
    assert "DrafterSilent.NO_CLAIMS" in tail
    assert "DrafterSilent.UNUSABLE" in tail


def test_the_reduced_ask_keeps_a_floor_under_the_claim_count():
    """Halving a small cap must not ask for an answer with no room for a
    conclusion per part."""
    assert "at most 4 claims" in qr._shorter_draft_prompt("...", 2)
    assert "at most 4 claims" in qr._shorter_draft_prompt("...", 8)
    assert "at most 12 claims" in qr._shorter_draft_prompt("...", 24)
    # and it is the same task, not a summary of it
    assert qr._shorter_draft_prompt("THE ORIGINAL", 12).endswith("THE ORIGINAL")
