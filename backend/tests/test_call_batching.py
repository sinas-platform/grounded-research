"""Unit tests for the call-batching endpoints.

These endpoints exist to collapse the agents' per-item tool calls into one:
  - GET /answers/{id}/evidence      — claims with evidence nested
  - POST /synthesis/answers/{id}/verdicts — bulk validation verdicts

The tests pin the shaping/validation logic with faked sessions; query
correctness is exercised live against a real database.

Run from the backend directory: `python -m pytest tests/test_call_batching.py`
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.v1.answers import get_answer_evidence
from app.api.v1.synthesis import (
    BulkVerdictsIn,
    VerdictEntry,
    record_validation_verdicts,
)
from app.models import AnswerClaim, ClaimEvidence

_NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _claim(answer_id, sequence):
    row = AnswerClaim(
        id=uuid.uuid4(),
        answer_id=answer_id,
        sequence=sequence,
        claim_text=f"claim {sequence}",
        claim_type="fact",
    )
    row.created_at = _NOW
    row.updated_at = _NOW
    return row


def _evidence(claim_id, validated=False):
    row = ClaimEvidence(
        id=uuid.uuid4(),
        claim_id=claim_id,
        document_id=uuid.uuid4(),
        document_version_id=None,
        span={"line_from": 1, "line_to": 3},
        stance="supports",
        relevance=None,
        validated=validated,
        validation_reasoning=None,
    )
    row.created_at = _NOW
    row.updated_at = _NOW
    return row


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeCaller:
    """Caller whose visibility filter always passes (checked via the preset
    visible-parent result, not here)."""

    def __init__(self):
        self.user_id = uuid.uuid4()
        self.roles = []
        self.is_admin = False

    async def has_permission(self, _permission):
        return False


class _FakeSession:
    """Returns preset results for successive execute() calls."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = False

    async def execute(self, _stmt):
        return _ScalarsResult(self._results.pop(0))

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_answer_evidence_nests_rows_under_their_claims():
    answer_id = uuid.uuid4()
    c1, c2 = _claim(answer_id, 1), _claim(answer_id, 2)
    e1, e2, e3 = _evidence(c1.id), _evidence(c2.id), _evidence(c1.id, validated=True)
    session = _FakeSession([[object()], [c1, c2], [e1, e2, e3]])

    out = await get_answer_evidence(answer_id=answer_id, session=session, caller=_FakeCaller())

    assert [c.sequence for c in out] == [1, 2]
    assert {e.id for e in out[0].evidence} == {e1.id, e3.id}
    assert [e.id for e in out[1].evidence] == [e2.id]
    assert any(e.validated for e in out[0].evidence)


@pytest.mark.asyncio
async def test_answer_evidence_keeps_evidenceless_claims():
    answer_id = uuid.uuid4()
    c1 = _claim(answer_id, 1)
    session = _FakeSession([[object()], [c1], []])

    out = await get_answer_evidence(answer_id=answer_id, session=session, caller=_FakeCaller())

    assert len(out) == 1
    assert out[0].evidence == []


@pytest.mark.asyncio
async def test_bulk_verdicts_updates_all_rows():
    answer_id = uuid.uuid4()
    claim = _claim(answer_id, 1)
    e1, e2 = _evidence(claim.id), _evidence(claim.id)
    session = _FakeSession([[object()], [e1, e2]])

    out = await record_validation_verdicts(
        answer_id=answer_id,
        payload=BulkVerdictsIn(
            verdicts=[
                VerdictEntry(evidence_id=e1.id, validated=True, reasoning="span supports"),
                VerdictEntry(evidence_id=e2.id, validated=False, reasoning="tangential"),
            ]
        ),
        session=session,
        caller=_FakeCaller(),
    )

    assert out == {"ok": True, "updated": 2}
    assert session.committed
    assert e1.validated is True and e1.validation_reasoning == "span supports"
    assert e2.validated is False and e2.validation_reasoning == "tangential"


@pytest.mark.asyncio
async def test_bulk_verdicts_rejects_foreign_evidence_atomically():
    answer_id = uuid.uuid4()
    claim = _claim(answer_id, 1)
    e1 = _evidence(claim.id)
    foreign_id = uuid.uuid4()  # belongs to another answer → not in query result
    session = _FakeSession([[object()], [e1]])

    with pytest.raises(HTTPException) as exc:
        await record_validation_verdicts(
            answer_id=answer_id,
            payload=BulkVerdictsIn(
                verdicts=[
                    VerdictEntry(evidence_id=e1.id, validated=True, reasoning="ok"),
                    VerdictEntry(evidence_id=foreign_id, validated=True, reasoning="ok"),
                ]
            ),
            session=session,
            caller=_FakeCaller(),
        )

    assert exc.value.status_code == 404
    # atomic: the valid row must not have been updated either
    assert e1.validated is False
    assert not session.committed


@pytest.mark.asyncio
async def test_bulk_verdicts_empty_list_is_a_noop():
    session = _FakeSession([[object()]])
    out = await record_validation_verdicts(
        answer_id=uuid.uuid4(),
        payload=BulkVerdictsIn(verdicts=[]),
        session=session,
        caller=_FakeCaller(),
    )
    assert out == {"ok": True, "updated": 0}
