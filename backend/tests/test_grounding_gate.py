"""Grounding gate behavior.

Verbatim names pass free with no LLM call; non-verbatim names are judged
in one batched call per document; ungrounded verdicts soft-drop the
mention (status, not deletion) with the verdict in link_evidence; a judge
reply that cannot be parsed never hides anything; consumers only see
status='active' rows.

Run from the backend directory:
`python -m pytest tests/test_grounding_gate.py`
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services.grounding_gate import (
    STATUS_ACTIVE,
    STATUS_REJECTED,
    ground_document,
    is_verbatim,
)

CONTENT = (
    "JUDGMENT OF THE COURT. Dow Benelux NV contests the inspection ordered "
    "by the Commission of the European Communities under Regulation 17."
)


class _ExecResult:
    def __init__(self, scalars=None, scalar=None, rows=None):
        self._scalars = scalars
        self._scalar = scalar
        self._rows = rows or []

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars)

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, mentions, content=CONTENT, canonicals=None):
        self._mentions = mentions
        self._content = content
        self._canonicals = canonicals or {}
        self.statements = []
        self.committed = False

    async def get(self, model, pk):
        return SimpleNamespace(filename="doc.md", current_version_id=uuid.uuid4())

    async def execute(self, stmt):
        self.statements.append(stmt)
        s = str(stmt)
        if "entity_mention" in s:
            return _ExecResult(scalars=self._mentions)
        if "document_version" in s:
            return _ExecResult(scalar=self._content)
        return _ExecResult(rows=list(self._canonicals.items()))

    async def commit(self):
        self.committed = True


class _FakeSinas:
    def __init__(self, reply=""):
        self.reply = reply
        self.calls = []

    async def invoke(self, agent, prompt):
        self.calls.append((agent, prompt))
        return self.reply


def _mention(surface, **kw):
    kw.setdefault("entity_id", None)
    return SimpleNamespace(
        surface_form=surface, span={"text": surface},
        status=STATUS_ACTIVE, link_method=None, link_evidence=None, **kw
    )


def _verdict_reply(*verdicts):
    return json.dumps({"verdicts": [
        {"n": i, **v} for i, v in enumerate(verdicts, start=1)
    ]})


def test_is_verbatim_exact_and_case_insensitive():
    assert is_verbatim("Dow Benelux NV", CONTENT)
    assert is_verbatim("dow benelux nv", CONTENT)
    assert not is_verbatim("European Commission", CONTENT)
    assert not is_verbatim("", CONTENT)


@pytest.mark.asyncio
async def test_verbatim_passes_free_without_llm():
    mentions = [_mention("Dow Benelux NV"), _mention("regulation 17")]
    session = _FakeSession(mentions)
    sinas = _FakeSinas()
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["verbatim"] == 2
    assert report["llm_calls"] == 0
    assert sinas.calls == []
    assert all(m.status == STATUS_ACTIVE for m in mentions)
    assert all(m.link_evidence is None for m in mentions)  # no noise


@pytest.mark.asyncio
async def test_ungrounded_rejected_softly_with_evidence():
    m = _mention("Deutsche Bahn AG")
    session = _FakeSession([m])
    sinas = _FakeSinas(_verdict_reply(
        {"grounded": False, "confidence": 0.9, "reason": "never referenced"}))
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["rejected"] == 1
    assert m.status == STATUS_REJECTED
    assert m.link_evidence["grounding"]["grounded"] is False
    assert m.link_evidence["grounding"]["reason"] == "never referenced"
    assert m.link_method is None  # untouched: how-linked, not why-hidden
    assert session.committed


@pytest.mark.asyncio
async def test_grounded_kept_active_with_audit_evidence():
    m = _mention("European Commission")  # content says "Commission of the
    # European Communities" — not verbatim, but grounded
    session = _FakeSession([m])
    sinas = _FakeSinas(_verdict_reply(
        {"grounded": True, "confidence": 0.95, "reason": "named as the Commission"}))
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["kept"] == 1
    assert m.status == STATUS_ACTIVE
    assert m.link_evidence["grounding"]["grounded"] is True


@pytest.mark.asyncio
async def test_low_confidence_rejection_is_kept():
    m = _mention("European Commission")
    session = _FakeSession([m])
    sinas = _FakeSinas(_verdict_reply(
        {"grounded": False, "confidence": 0.3, "reason": "unsure"}))
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["kept"] == 1
    assert m.status == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_unparseable_reply_never_hides_mentions():
    mentions = [_mention("European Commission"), _mention("Akzo Nobel")]
    session = _FakeSession(mentions)
    sinas = _FakeSinas("I could not decide, sorry.")
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["unparsed_kept"] == 2
    assert all(m.status == STATUS_ACTIVE for m in mentions)


@pytest.mark.asyncio
async def test_one_batched_call_per_document():
    mentions = [_mention(f"Phantom Corp {i}") for i in range(7)]
    session = _FakeSession(mentions)
    sinas = _FakeSinas(_verdict_reply(*(
        {"grounded": False, "confidence": 0.9, "reason": "absent"}
        for _ in range(7))))
    report = await ground_document(session, sinas, uuid.uuid4())
    assert len(sinas.calls) == 1
    assert report["rejected"] == 7


@pytest.mark.asyncio
async def test_existing_link_evidence_is_merged_not_clobbered():
    m = _mention("European Commission")
    m.link_evidence = {"matched": "European Commission"}
    session = _FakeSession([m])
    sinas = _FakeSinas(_verdict_reply(
        {"grounded": True, "confidence": 0.9, "reason": "ok"}))
    await ground_document(session, sinas, uuid.uuid4())
    assert m.link_evidence["matched"] == "European Commission"
    assert m.link_evidence["grounding"]["grounded"] is True


@pytest.mark.asyncio
async def test_gate_and_consumers_query_only_active_mentions():
    # the gate's own mention load filters on status
    session = _FakeSession([])
    await ground_document(session, _FakeSinas(), uuid.uuid4())
    assert "status" in str(session.statements[0])
    # and so does the resolver, the first consumer in the chain
    from app.services import entity_resolver

    class _ResolverSession(_FakeSession):
        async def get(self, model, pk):
            return None

    rsession = _ResolverSession([])
    index = entity_resolver._EntityIndex([])
    await entity_resolver.resolve_document(
        rsession, _FakeSinas(), index, {}, uuid.uuid4()
    )
    assert "status" in str(rsession.statements[0])


@pytest.mark.asyncio
async def test_legacy_mention_falls_back_to_canonical_form():
    # pre mentions-first rows: no surface_form, span without a text key,
    # but linked to an entity whose canonical form is in the document
    eid = uuid.uuid4()
    m = SimpleNamespace(
        surface_form=None, span={"line": 8, "start": 545, "end": 565},
        status=STATUS_ACTIVE, link_method=None, link_evidence=None,
        entity_id=eid,
    )
    session = _FakeSession([m], canonicals={eid: "Dow Benelux NV"})
    sinas = _FakeSinas()
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["verbatim"] == 1
    assert sinas.calls == []
    assert m.status == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_no_derivable_surface_is_skipped_never_judged_as_question_mark():
    m = SimpleNamespace(
        surface_form=None, span={"line": 8, "start": 545, "end": 565},
        status=STATUS_ACTIVE, link_method=None, link_evidence=None,
        entity_id=None,
    )
    session = _FakeSession([m])
    sinas = _FakeSinas()
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["no_surface_skipped"] == 1
    assert report["llm_calls"] == 0
    assert sinas.calls == []
    assert m.status == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_judge_echoing_name_strings_still_parses(monkeypatch):
    # observed with haiku: the reply carries the surface text in the key
    # field instead of the number; verdicts must still map back
    m1 = _mention("Skype Global S.a.r.l.")
    m2 = _mention("Case COMP/M.6281 - Microsoft/Skype")
    session = _FakeSession([m1, m2])
    reply = json.dumps({"verdicts": [
        {"name": "Skype Global S.a.r.l.", "grounded": True,
         "confidence": 0.99, "reason": "named in title"},
        {"name": "Case COMP/M.6281 - Microsoft/Skype", "grounded": False,
         "confidence": 0.9, "reason": "not referenced"},
    ]})
    sinas = _FakeSinas(reply)
    report = await ground_document(session, sinas, uuid.uuid4())
    assert report["kept"] == 1
    assert report["rejected"] == 1
    assert m1.status == STATUS_ACTIVE
    assert m2.status == STATUS_REJECTED


# ── a revision reply is claims or it is nothing ─────────────────────────────
# The reviser returns the corrected claim set as JSON. Anything that is not a
# claim object cannot become a claim — refusals, questions and verdicts on the
# evidence fail structurally, not because their wording was recognised.


def test_only_patch_operations_survive_parsing():
    from app.services.query_runner import _parse_patch

    good = (
        '{"revise": [{"seq": 4, "text": "Regulation (EU) 2018/1725, not the '
        'GDPR, governs the Commission\'s processing of personal data during '
        'an inspection.", "evidence": [{"filename": "32018R1725.md", '
        '"line_from": 40, "line_to": 52}]}], "drop": [9], "add": []}'
    )
    patch = _parse_patch(good)
    assert patch and len(patch["revise"]) == 1 and patch["drop"] == [9]

    for not_a_claim in [
        "I need to see the evidence/spans you're referring to in order to rewrite.",
        "The passage provided is only a cover/header from the judgment and does "
        "not contain the substantive holdings needed to support the claim.",
        "Could you please provide the working document set?",
        "",
        "{}",
        '{"revise": [], "drop": [], "add": []}',
        # shaped like an operation but carries no citable span
        '{"revise": [{"seq": 2, "text": "The Commission must protect personal '
        'data during an inspection under the regime.", "evidence": []}]}',
        # span without a document
        '{"add": [{"text": "The Commission must protect personal data during an '
        'inspection.", "evidence": [{"line_from": 4, "line_to": 9}]}]}',
    ]:
        assert _parse_patch(not_a_claim) is None, not_a_claim[:60]


def test_revision_only_touches_the_claims_the_patch_names():
    """Re-judging claims the revision did not change was the loop's cost. A
    patch names what to revise, drop and add; everything else keeps its row,
    its spans and its verdicts, because it is never rebuilt."""
    import inspect
    from app.services import query_runner as qr

    src = inspect.getsource(qr._revise_answer)
    # operations are applied by name, not by rebuilding the answer
    for op in ('patch["drop"]', 'patch["revise"]', 'patch["add"]'):
        assert op in src, op
    # the whole-answer replacement is gone
    assert "AnswerClaim.id.in_(old_ids)" not in src
    # only revised claims lose their evidence (it is re-bound)
    revise_block = src[src.index('for item in patch["revise"]'):
                       src.index('if patch["add"]')]
    assert "ClaimEvidence.__table__.delete()" in revise_block


def test_abstention_is_offered_only_on_the_final_gate_cycle():
    """The flag exists inside the reviser; it has to be PASSED, or abstention
    can never fire. It shipped once without a caller supplying it."""
    import inspect
    from app.services import query_runner as qr

    src = inspect.getsource(qr._stage_validate_publish)
    calls = [l for l in src.splitlines() if "last_attempt=" in l]
    assert len(calls) >= 2, "every gate-driven revision must state its cycle"
    for line in calls:
        # budgeted cycles compute the flag; a bonus cycle for an objection
        # the reviser has never seen IS the final chance, so it passes True
        assert "gate_cycles <= 1" in line or "last_attempt=True" in line, line


def test_drafting_input_carries_no_interpretation():
    """Grounding is on raw source text only.

    The manifest — summaries, classes, annotations — is interpretation
    produced at ingestion and checked against nothing. It may decide what to
    READ; it may never be what a claim asserts. One answer attributed an
    Opinion to the right Advocate General on the strength of a summary, with
    no passage behind it: true, and uncheckable.
    """
    import inspect
    from app.services import query_runner as qr

    src = inspect.getsource(qr._draft_from_extracts)
    # the plan's prose target must not be interpolated into the draft prompt
    assert 'e.get("establishes")' not in src and "e['establishes']" not in src
    assert "PASSAGE GROUP" in src
    assert "ONLY thing you know" in src

    # the manifest may steer reading, but must never reach a drafting prompt
    synth = inspect.getsource(qr._stage_synthesize)
    assert "_doc_manifest" in synth, "the manifest still guides the plan"
    assert "+ manifest" not in synth, "manifest text reaches a drafting prompt"

    # the chat drafting loop, which sent the manifest to the drafter, is gone
    assert not hasattr(qr, "_dead_chat_diagnosis") or "synthesis-agent" not in synth
    from app.config import Settings
    import pytest as _pytest
    with _pytest.raises(Exception):
        Settings(SGR_DRAFT_MODE="chat")


def test_a_keep_records_a_reason_and_cannot_smuggle_in_a_claim():
    """The gate names a stronger source; keeping the original citation is
    often right, and was previously indistinguishable from ignoring the
    finding — the reviser had no way to say "I read it, mine is better".

    A keep carries a sequence number and a reason and nothing else: no text,
    no spans. So it can never introduce or alter a claim, and the claim it
    names keeps its verdicts because it is not rebuilt.
    """
    from app.services.query_runner import _parse_patch

    patch = _parse_patch(
        '{"keep": [{"seq": 3, "rationale": "32025M11936.md restates the '
        'operative paragraph; m11936.md carries the Commission\'s own '
        'reasoning on the point."}]}'
    )
    assert patch and patch["keep"] == [
        {"seq": 3, "rationale": "32025M11936.md restates the operative "
                                "paragraph; m11936.md carries the "
                                "Commission's own reasoning on the point."}
    ]
    assert patch["revise"] == [] and patch["add"] == [] and patch["drop"] == []

    for not_a_keep in [
        # no reason given: a bare refusal to act is not a decision
        '{"keep": [{"seq": 3}]}',
        '{"keep": [{"seq": 3, "rationale": "no"}]}',
        # no claim it applies to
        '{"keep": [{"rationale": "the current citation is more direct here"}]}',
    ]:
        assert _parse_patch(not_a_keep) is None, not_a_keep


def test_a_keep_alters_only_the_rationale():
    import inspect

    from app.services import query_runner as qr

    src = inspect.getsource(qr._revise_answer)
    block = src[src.index('for item in patch.get("keep")'):
                src.index("await session.commit()",
                          src.index('for item in patch.get("keep")'))]
    assert "row.rationale = item" in block
    for forbidden in ("claim_text", "_bind_spans", "ClaimEvidence"):
        assert forbidden not in block, forbidden


def test_a_revised_or_added_claim_carries_its_reasoning():
    from app.services.query_runner import _parse_patch

    patch = _parse_patch(
        '{"revise": [{"seq": 4, "text": "Regulation (EU) 2018/1725, not the '
        'GDPR, governs the processing of personal data during an inspection.",'
        ' "rationale": "Answers the applicable-regime part of the question; '
        '2018/1725 is the instrument addressed to the institutions.", '
        '"evidence": [{"filename": "32018R1725.md", "line_from": 40, '
        '"line_to": 52}]}]}'
    )
    assert patch["revise"][0]["rationale"].startswith("Answers the applicable")
