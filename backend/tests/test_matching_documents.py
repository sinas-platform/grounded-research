"""Unit tests for the matching-documents endpoint shaping.

matching_documents returns each match with the fields needed to identify it
(filename, class, summary preview) so a caller doesn't have to call
get_document per id. The change is additive: `document_ids` is still populated,
in the same order as `documents`.

Query correctness (the left join to document_class, the summary truncation) is
exercised against a real database elsewhere; these tests pin the endpoint's
contract: backward-compatible ids, and one MatchingDocumentOut per row.

Run from the backend directory: `python -m pytest tests/test_matching_documents.py`
"""

import uuid

import pytest

from app.api.v1.retrieval import matching_documents
from app.schemas.runtime import MatchingDocumentsIn


class _FakeCaller:
    async def has_permission(self, _perm):
        return True


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Async session stand-in: execute() returns preset rows regardless of the
    statement, so the test pins how the endpoint maps rows to the response."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _RowsResult(self._rows)


def _row(filename, cls_name, summary):
    # (id, filename, document_class_id, document_class_name, summary_preview)
    return (uuid.uuid4(), filename, uuid.uuid4(), cls_name, summary)


@pytest.mark.asyncio
async def test_returns_identifying_fields_per_document():
    rows = [
        _row("a-decision.md", "Regulatory Decision", "The Northmoor Authority assessed..."),
        _row("an-inquiry--p04.md", "Court Decision", "The reviewing body assessed..."),
    ]
    session = _FakeSession(rows)
    out = await matching_documents(
        payload=MatchingDocumentsIn(filter=None, limit=50),
        session=session,
        caller=_FakeCaller(),
    )
    assert len(out.documents) == 2
    first = out.documents[0]
    assert first.filename == "a-decision.md"
    assert first.document_class_name == "Regulatory Decision"
    assert first.summary.startswith("The Northmoor")


@pytest.mark.asyncio
async def test_document_ids_stay_populated_and_aligned():
    rows = [_row("a.md", "Book", "s1"), _row("b.md", "Article (Review)", "s2")]
    session = _FakeSession(rows)
    out = await matching_documents(
        payload=MatchingDocumentsIn(filter=None, limit=50),
        session=session,
        caller=_FakeCaller(),
    )
    # backward compatible: ids still present, and in the same order as documents
    assert out.document_ids == [d.id for d in out.documents]
    assert out.document_ids == [rows[0][0], rows[1][0]]


@pytest.mark.asyncio
async def test_empty_match_returns_empty_lists_not_error():
    session = _FakeSession([])
    out = await matching_documents(
        payload=MatchingDocumentsIn(filter=None, limit=50),
        session=session,
        caller=_FakeCaller(),
    )
    assert out.document_ids == []
    assert out.documents == []


@pytest.mark.asyncio
async def test_null_class_and_summary_are_tolerated():
    # numeric filename, unclassified doc, no summary — must not raise
    rows = [(uuid.uuid4(), "99999.md", None, None, None)]
    session = _FakeSession(rows)
    out = await matching_documents(
        payload=MatchingDocumentsIn(filter=None, limit=50),
        session=session,
        caller=_FakeCaller(),
    )
    assert out.documents[0].document_class_name is None
    assert out.documents[0].summary is None
    assert out.documents[0].filename == "99999.md"
