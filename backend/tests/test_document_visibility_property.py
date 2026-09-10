"""`shared` is the corpus, `private` is someone's upload, and retrieval
never ranks what the asker could not open.

Documents used to carry only the owner-and-roles tenancy every table
shares, which is right for an upload and wrong for a corpus every user is
meant to read. The property splits the two. Its second half is retrieval:
a property the engine ignores is a promise it does not keep, since a private
upload could still be cited in a stranger's answer.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app import retrieval_first as rf
from app.models import Document
from app.models.runtime import DOCUMENT_VISIBILITIES
from app.services.document_registry import register_document


def test_the_column_defaults_to_shared_on_both_sides() -> None:
    column = Document.__table__.c.visibility
    assert not column.nullable
    assert column.server_default.arg == "shared"
    assert column.default.arg == "shared"
    assert DOCUMENT_VISIBILITIES == ("shared", "private")


class _NoExistingSession:
    """A session that finds no prior document, so registration creates."""

    def __init__(self):
        self.added = []

    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_registration_stamps_the_requested_visibility() -> None:
    session = _NoExistingSession()

    await register_document(
        session, filename="brief.md", content="# Brief", owner_id=uuid.uuid4(),
        roles=[], visibility="private",
    )

    doc = next(row for row in session.added if isinstance(row, Document))
    assert doc.visibility == "private"


@pytest.mark.asyncio
async def test_registration_defaults_to_shared() -> None:
    session = _NoExistingSession()

    await register_document(
        session, filename="decision.md", content="# Decision",
        owner_id=uuid.uuid4(), roles=[],
    )

    doc = next(row for row in session.added if isinstance(row, Document))
    assert doc.visibility == "shared"


@pytest.mark.asyncio
async def test_registration_refuses_an_unknown_visibility() -> None:
    with pytest.raises(ValueError, match="visibility"):
        await register_document(
            _NoExistingSession(), filename="x.md", content="x",
            owner_id=uuid.uuid4(), roles=[], visibility="public",
        )


class _RecordingSession:
    """Answers every retrieval query with nothing, recording what was asked."""

    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), dict(params or {})))
        return SimpleNamespace(
            scalar=lambda: 1,
            all=lambda: [],
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )


def _plan(**over) -> dict:
    plan = {
        "effort": "medium",
        "anchors": [str(uuid.uuid4())],
        "anchor_names": {},
        "queries": ["inspection powers of the commission"],
    }
    plan["anchor_names"] = {plan["anchors"][0]: "Energy Wholesale Merger Inquiry"}
    plan.update(over)
    return plan


async def _retrieve(monkeypatch, **kw) -> _RecordingSession:
    session = _RecordingSession()

    @asynccontextmanager
    async def _session_local():
        yield session

    import app.db

    monkeypatch.setattr(app.db, "AsyncSessionLocal", _session_local)
    await rf.retrieve_and_rank(_plan(), **kw)
    return session


@pytest.mark.asyncio
async def test_every_document_channel_is_scoped_to_the_asker(monkeypatch) -> None:
    owner = uuid.uuid4()
    session = await _retrieve(monkeypatch, owner_id=owner, roles=["reviewers"])

    channels = [(sql, p) for sql, p in session.statements if "document d" in sql]
    # Mentions, relationship evidence, full text, filename: four channels
    # read documents, and the entity-frontier and frequency queries do not.
    assert len(channels) == 4
    for sql, params in channels:
        assert rf.VISIBLE_TO_ASKER in sql
        assert params["owner"] == str(owner)
        assert params["roles"] == ["reviewers"]


@pytest.mark.asyncio
async def test_without_an_asker_only_the_corpus_is_searched(monkeypatch) -> None:
    session = await _retrieve(monkeypatch)

    channels = [(sql, p) for sql, p in session.statements if "document d" in sql]
    assert channels
    for _, params in channels:
        assert params["owner"] is None
        assert params["roles"] == []


def test_the_asker_clause_admits_exactly_the_three_grounds() -> None:
    assert "d.visibility = 'shared'" in rf.VISIBLE_TO_ASKER
    assert "d.owner_id = CAST(:owner AS uuid)" in rf.VISIBLE_TO_ASKER
    assert "d.roles && :roles" in rf.VISIBLE_TO_ASKER
