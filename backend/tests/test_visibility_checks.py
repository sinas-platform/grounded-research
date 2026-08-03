"""Unit tests for visibility/ownership checks on answer, result, and synthesis
endpoints.

Child-row reads (claims, evidence, result documents, traces) and per-row
synthesis writes must resolve their parent Answer/Result through
`visible_clause` first, 404-ing on rows the caller can't see — same pattern as
`get_answer`/`get_result`. The `read:all`/`write:all` permissions lift the
owner/roles filter.

These tests pin the endpoint contract with fake sessions: which queries run,
that invisible parents stop the request before any child query or insert, and
that the compiled visibility filter drops the owner constraint when the caller
holds the `:all` permission. Query semantics of `visible_clause` itself are
exercised against a real database elsewhere.

Run from the backend directory: `python -m pytest tests/test_visibility_checks.py`
"""

import uuid
from types import SimpleNamespace

import pytest
from app.api.v1.answers import get_answer_claims, get_claim_evidence
from app.api.v1.results import get_result_documents, get_trace
from app.api.v1.synthesis import (
    DraftClaimIn,
    StartAnswerIn,
    ValidationVerdict,
    bind_evidence,
    draft_claim,
    publish_answer,
    record_validation_verdict,
    start_answer,
)
from app.models import Answer, ClaimEvidence, Result
from app.schemas.common import Span
from app.schemas.runtime import ClaimEvidenceIn
from fastapi import HTTPException


class _FakeCaller:
    def __init__(self, permissions=()):
        self.user_id = uuid.uuid4()
        self.roles = []
        self.is_admin = False
        self._permissions = set(permissions)

    async def has_permission(self, permission):
        return permission in self._permissions


class _ExecResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Async session stand-in: execute() pops preset results in call order and
    records each statement, so tests can assert what ran (and what didn't)."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._results.pop(0)

    def add(self, row):
        self.added.append(row)

    def add_all(self, rows):
        self.added.extend(rows)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, row):
        # Stand in for the DB stamping server defaults on INSERT.
        from datetime import UTC, datetime

        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        now = datetime.now(UTC)
        if getattr(row, "created_at", None) is None:
            row.created_at = now
        if getattr(row, "updated_at", None) is None:
            row.updated_at = now


def _answer(**overrides):
    fields = {"id": uuid.uuid4(), "question": "q?", "status": "draft", **overrides}
    return Answer(**fields)


def _evidence_payload():
    return ClaimEvidenceIn(
        document_id=uuid.uuid4(),
        document_version_id=None,
        span=Span(line_from=1, line_to=2),
        stance="supports",
        relevance=0.9,
    )


# ─────────────────────────────────────────────────────────────
# Answer child reads
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_answer_claims_404_when_answer_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await get_answer_claims(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert exc.value.status_code == 404
    # the claims query must not have run
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_get_answer_claims_returns_rows_when_visible():
    # SimpleNamespace, not object(): the endpoint stamps display_number onto
    # each returned row.
    claims = [SimpleNamespace(), SimpleNamespace()]
    session = _FakeSession([_ExecResult(scalar=_answer()), _ExecResult(rows=claims)])
    out = await get_answer_claims(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert out == claims
    assert [c.display_number for c in claims] == [1, 2]


@pytest.mark.asyncio
async def test_get_answer_claims_filters_by_owner_without_read_all():
    session = _FakeSession([_ExecResult(scalar=_answer()), _ExecResult(rows=[])])
    await get_answer_claims(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert "owner_id" in str(session.statements[0].whereclause)


@pytest.mark.asyncio
async def test_get_answer_claims_skips_owner_filter_with_read_all():
    session = _FakeSession([_ExecResult(scalar=_answer()), _ExecResult(rows=[])])
    caller = _FakeCaller(permissions={"grove.answers.read:all"})
    await get_answer_claims(uuid.uuid4(), session=session, caller=caller)
    assert "owner_id" not in str(session.statements[0].whereclause)


@pytest.mark.asyncio
async def test_get_claim_evidence_404_when_claim_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await get_claim_evidence(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert exc.value.status_code == 404
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_get_claim_evidence_returns_rows_when_visible():
    evidence = [object()]
    session = _FakeSession([_ExecResult(scalar=object()), _ExecResult(rows=evidence)])
    out = await get_claim_evidence(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert out == evidence
    # the claim lookup resolves visibility through the parent answer
    assert "answer" in str(session.statements[0])


# ─────────────────────────────────────────────────────────────
# Result child reads
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_result_documents_404_when_result_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await get_result_documents(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert exc.value.status_code == 404
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_get_result_documents_returns_rows_when_visible():
    from datetime import UTC, datetime

    from app.models import ResultDocument

    rd = ResultDocument(
        id=uuid.uuid4(),
        result_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
    )
    rd.created_at = rd.updated_at = datetime.now(UTC)
    # (row, filename, class_name, summary_preview) — the identify join from #14
    docs = [(rd, "doc.md", "Some Class", "a summary")]
    session = _FakeSession([_ExecResult(scalar=object()), _ExecResult(rows=docs)])
    out = await get_result_documents(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert [d.document_id for d in out] == [rd.document_id]
    assert out[0].filename == "doc.md"
    assert out[0].document_class_name == "Some Class"


@pytest.mark.asyncio
async def test_get_trace_404_when_result_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await get_trace(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert exc.value.status_code == 404
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_get_trace_returns_rows_when_visible():
    trace = [object()]
    session = _FakeSession([_ExecResult(scalar=object()), _ExecResult(rows=trace)])
    out = await get_trace(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert out == trace


# ─────────────────────────────────────────────────────────────
# Synthesis writes
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_answer_404_when_source_result_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await start_answer(
            StartAnswerIn(question="q?", source_result_id=uuid.uuid4()),
            session=session,
            caller=_FakeCaller(),
        )
    assert exc.value.status_code == 404
    assert session.added == []
    assert not session.committed
    assert "owner_id" in str(session.statements[0].whereclause)


@pytest.mark.asyncio
async def test_start_answer_inherits_owner_from_visible_source_result():
    source_owner = uuid.uuid4()
    result = Result(id=uuid.uuid4(), query="q", owner_id=source_owner, roles=["legal"])
    session = _FakeSession([_ExecResult(scalar=result)])
    await start_answer(
        StartAnswerIn(question="q?", source_result_id=result.id),
        session=session,
        caller=_FakeCaller(),
    )
    assert session.committed
    (answer,) = session.added
    assert answer.owner_id == source_owner
    assert answer.roles == ["legal"]


@pytest.mark.asyncio
async def test_draft_claim_404_when_answer_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await draft_claim(
            uuid.uuid4(),
            DraftClaimIn(sequence=1, claim_text="c"),
            session=session,
            caller=_FakeCaller(),
        )
    assert exc.value.status_code == 404
    assert session.added == []
    assert not session.committed


@pytest.mark.asyncio
async def test_draft_claim_inserts_when_answer_visible():
    answer_id = uuid.uuid4()
    # Second result: the idempotency lookup for an existing claim at this
    # (answer_id, sequence) — none, so the endpoint takes the insert path.
    session = _FakeSession([_ExecResult(scalar=_answer(id=answer_id)), _ExecResult(scalar=None)])
    row = await draft_claim(
        answer_id,
        DraftClaimIn(sequence=1, claim_text="c"),
        session=session,
        caller=_FakeCaller(),
    )
    assert session.committed
    assert row.answer_id == answer_id
    assert row.claim_text == "c"


@pytest.mark.asyncio
async def test_draft_claim_skips_owner_filter_with_write_all():
    session = _FakeSession([_ExecResult(scalar=_answer()), _ExecResult(scalar=None)])
    caller = _FakeCaller(permissions={"grove.answers.write:all"})
    await draft_claim(
        uuid.uuid4(), DraftClaimIn(sequence=1, claim_text="c"), session=session, caller=caller
    )
    assert "owner_id" not in str(session.statements[0].whereclause)


@pytest.mark.asyncio
async def test_bind_evidence_404_when_claim_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await bind_evidence(
            uuid.uuid4(), _evidence_payload(), session=session, caller=_FakeCaller()
        )
    assert exc.value.status_code == 404
    assert session.added == []
    assert not session.committed


@pytest.mark.asyncio
async def test_bind_evidence_inserts_when_claim_visible():
    claim_id = uuid.uuid4()
    session = _FakeSession([_ExecResult(scalar=object())])
    row = await bind_evidence(claim_id, _evidence_payload(), session=session, caller=_FakeCaller())
    assert session.committed
    assert row.claim_id == claim_id


@pytest.mark.asyncio
async def test_record_validation_verdict_404_when_evidence_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await record_validation_verdict(
            uuid.uuid4(),
            ValidationVerdict(validated=True, reasoning="ok"),
            session=session,
            caller=_FakeCaller(),
        )
    assert exc.value.status_code == 404
    assert not session.committed


@pytest.mark.asyncio
async def test_record_validation_verdict_updates_when_visible():
    evidence = ClaimEvidence(id=uuid.uuid4(), claim_id=uuid.uuid4(), validated=False)
    session = _FakeSession([_ExecResult(scalar=evidence)])
    out = await record_validation_verdict(
        evidence.id,
        ValidationVerdict(validated=True, reasoning="checked"),
        session=session,
        caller=_FakeCaller(),
    )
    assert out == {"ok": True}
    assert evidence.validated is True
    assert evidence.validation_reasoning == "checked"
    assert session.committed


@pytest.mark.asyncio
async def test_publish_answer_404_when_answer_invisible():
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await publish_answer(uuid.uuid4(), session=session, caller=_FakeCaller())
    assert exc.value.status_code == 404
    assert not session.committed


@pytest.mark.asyncio
async def test_publish_answer_publishes_when_visible_and_validated():
    answer = _answer()
    # second result: the unvalidated-evidence probe finds nothing
    session = _FakeSession([_ExecResult(scalar=answer), _ExecResult(scalar=None)])
    out = await publish_answer(answer.id, session=session, caller=_FakeCaller())
    assert out == {"id": answer.id, "status": "published"}
    assert answer.status == "published"
    assert session.committed
