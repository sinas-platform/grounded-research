"""Unit tests for get_result_documents pagination and the compact projection.

The compat contract: with no parameters the endpoint returns exactly what it
returned before (full ResultDocumentOut rows, unpaged). `limit`/`offset` apply
at the SQL level — these tests pin that the statement carries them (query
semantics are exercised against a real database elsewhere, same convention as
the other endpoint tests). `compact=true` projects to identity fields only.

Run from the backend directory:
`python -m pytest tests/test_result_documents_pagination.py`
"""

import inspect
import typing
import uuid
from datetime import UTC, datetime

import pytest
from app.api.v1.results import get_result_documents
from app.models import ResultDocument
from app.schemas.runtime import ResultDocumentCompactOut, ResultDocumentOut


class _FakeCaller:
    def __init__(self):
        self.user_id = uuid.uuid4()
        self.roles = []
        self.is_admin = False

    async def has_permission(self, permission):
        return True


class _ExecResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    """Async session stand-in: execute() pops preset results in call order and
    records each statement, so tests can assert what ran."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._results.pop(0)


def _row(rank=None):
    rd = ResultDocument(
        id=uuid.uuid4(),
        result_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        rank=rank,
    )
    rd.created_at = rd.updated_at = datetime.now(UTC)
    return (rd, "doc.md", "Some Class", "a summary")


@pytest.mark.asyncio
async def test_default_call_returns_full_rows_unpaged():
    rows = [_row(rank=1), _row(rank=2)]
    session = _FakeSession([_ExecResult(scalar=object()), _ExecResult(rows=rows)])
    out = await get_result_documents(
        uuid.uuid4(), session=session, caller=_FakeCaller()
    )
    assert all(isinstance(o, ResultDocumentOut) for o in out)
    assert [o.document_id for o in out] == [r[0].document_id for r in rows]
    assert out[0].filename == "doc.md"
    assert out[0].document_class_name == "Some Class"
    assert out[0].summary == "a summary"
    # unpaged: the statement carries no LIMIT and no OFFSET
    stmt = session.statements[1]
    assert stmt._limit_clause is None
    assert stmt._offset_clause is None


@pytest.mark.asyncio
async def test_limit_and_offset_are_applied_to_the_statement():
    session = _FakeSession([_ExecResult(scalar=object()), _ExecResult(rows=[])])
    await get_result_documents(
        uuid.uuid4(), limit=2, offset=3, session=session, caller=_FakeCaller()
    )
    stmt = session.statements[1]
    assert stmt._limit_clause is not None
    assert stmt._offset_clause is not None


@pytest.mark.asyncio
async def test_compact_projects_identity_fields_only():
    rows = [_row(rank=7)]
    session = _FakeSession([_ExecResult(scalar=object()), _ExecResult(rows=rows)])
    out = await get_result_documents(
        uuid.uuid4(), compact=True, session=session, caller=_FakeCaller()
    )
    assert all(isinstance(o, ResultDocumentCompactOut) for o in out)
    assert out[0].document_id == rows[0][0].document_id
    assert out[0].filename == "doc.md"
    assert out[0].document_class_name == "Some Class"
    assert out[0].rank == 7
    assert set(ResultDocumentCompactOut.model_fields) == {
        "document_id",
        "filename",
        "document_class_name",
        "rank",
        "annotations",  # opt-in via ?annotate=; None (excluded) otherwise
    }


def test_parameter_bounds_are_declared():
    # limit 1..200, offset >= 0 — enforced by FastAPI at the HTTP layer; pin
    # the Annotated declaration so a signature change can't silently drop the
    # bounds, while direct calls keep plain Python defaults.
    params = inspect.signature(get_result_documents).parameters
    hints = typing.get_type_hints(get_result_documents, include_extras=True)

    def _bounds(name):
        (query,) = hints[name].__metadata__
        return {
            k: v
            for m in query.metadata
            for k, v in (("ge", getattr(m, "ge", None)), ("le", getattr(m, "le", None)))
            if v is not None
        }

    assert _bounds("limit") == {"ge": 1, "le": 200}
    assert _bounds("offset") == {"ge": 0}
    assert params["limit"].default is None
    assert params["offset"].default == 0
    assert params["compact"].default is False
