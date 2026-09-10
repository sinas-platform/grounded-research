"""A caller who can see a result can read the documents it cites.

The answer a lawyer reads is only worth what its sources are worth, and the
sources must be openable by the person who asked. Corpus documents belong to
whoever ingested them, so without this cascade a non-admin without
`sgr.documents.read:all` sees their answer and a 404 on every source in it.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.documents import _document_visibility
from app.models import Document
from sqlalchemy.dialects import postgresql


class _FakeCaller:
    def __init__(self, permissions=(), is_admin=False):
        self.user_id = uuid.uuid4()
        self.roles = []
        self.is_admin = is_admin
        self._permissions = set(permissions)

    async def has_permission(self, permission):
        return permission in self._permissions


def _compiled(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_a_reader_reaches_documents_through_results_they_can_see() -> None:
    sql = _compiled(await _document_visibility(Document, _FakeCaller()))

    # The shared corpus, own documents, the dossier cascade, and the result
    # cascade: a private document must be attached to a result the caller
    # owns or shares.
    assert "document.visibility" in sql
    assert "document.owner_id" in sql
    assert "dossier_document" in sql
    assert "result_document" in sql
    assert "result.owner_id" in sql


@pytest.mark.asyncio
async def test_the_result_cascade_is_scoped_to_the_callers_own_results() -> None:
    sql = _compiled(await _document_visibility(Document, _FakeCaller()))

    # A result someone else owns must not open its documents: the subquery
    # carries the owner filter, never an always-true clause.
    result_part = sql[sql.index("result_document") :]
    assert "result.owner_id = " in result_part
    assert "result.id = result.id" not in result_part


@pytest.mark.asyncio
async def test_read_all_needs_no_cascade() -> None:
    reader = _FakeCaller(permissions={"sgr.documents.read:all"})
    sql = _compiled(await _document_visibility(Document, reader))

    assert "result_document" not in sql
    assert "dossier_document" not in sql
