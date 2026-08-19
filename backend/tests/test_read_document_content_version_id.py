"""read_document_content must expose the version's UUID.

Writers that reference an exact version (mentions, property values) need
`document_version_id`; the integer `version` number is not it, and this
response is the only place a reading agent can learn the UUID. Both branches
(extracted content and the empty-content note) carry the field.

Run from the backend directory:
`python -m pytest tests/test_read_document_content_version_id.py`
"""

import uuid
from types import SimpleNamespace

import pytest
from app.api.v1.documents import read_document_content


class _FakeCaller:
    def __init__(self):
        self.user_id = uuid.uuid4()
        self.roles = []
        self.is_admin = False

    async def has_permission(self, permission):
        return True


class _ExecResult:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        return self._results.pop(0)


def _dv(content):
    return SimpleNamespace(id=uuid.uuid4(), version=1, content_md=content)


@pytest.mark.asyncio
async def test_content_response_carries_the_version_uuid():
    dv = _dv("line one\nline two\nline three")
    session = _FakeSession([_ExecResult(object()), _ExecResult(dv)])
    out = await read_document_content(
        uuid.uuid4(),
        1,
        line_from=None,
        line_to=None,
        numbered=False,
        # calling the endpoint function directly, so FastAPI never resolves
        # its Query() defaults — pass them explicitly
        max_lines=None,
        session=session,
        caller=_FakeCaller(),
    )
    assert out["document_version_id"] == dv.id
    assert out["version"] == 1
    assert out["total_lines"] == 3


@pytest.mark.asyncio
async def test_empty_content_response_carries_the_version_uuid():
    dv = _dv(None)
    session = _FakeSession([_ExecResult(object()), _ExecResult(dv)])
    out = await read_document_content(
        uuid.uuid4(),
        1,
        line_from=None,
        line_to=None,
        numbered=False,
        # calling the endpoint function directly, so FastAPI never resolves
        # its Query() defaults — pass them explicitly
        max_lines=None,
        session=session,
        caller=_FakeCaller(),
    )
    assert out["document_version_id"] == dv.id
    assert out["extracted"] is False
