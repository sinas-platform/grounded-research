"""Unit tests for the ingestion precision guards.

Fake-session style (no database): each test pins the endpoint contract — a
valid write inserts and commits, a violating write is skipped with a `rejected`
outcome and nothing is added. Every rule the guards enforce is read from the
config/schema objects the test constructs, so fixtures use placeholder types
(rel_x, TypeA/TypeB) with no real-world names.

Run from the backend directory: `python -m pytest tests/test_precision_guards.py`
"""

import uuid
from types import SimpleNamespace

import pytest
from app.models import (
    Document,
    DocumentVersion,
    Entity,
    RelationshipDefinition,
)
from app.schemas.runtime import RelationshipIn


class _FakeSession:
    """Async session stand-in. `get(Model, id)` returns a preset object keyed
    by id; `execute()` pops preset results; add()/commit()/refresh() record the
    write so a test can assert whether anything was persisted."""

    def __init__(self, gets=None, results=None):
        self._gets = dict(gets or {})
        self._results = list(results or [])
        self.added = []
        self.committed = False

    async def get(self, model, ident):
        return self._gets.get(ident)

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True

    async def refresh(self, row):
        from datetime import datetime, timezone

        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        if getattr(row, "created_at", None) is None:
            row.created_at = now
        if getattr(row, "updated_at", None) is None:
            row.updated_at = now


def _settings(**modes):
    """A fake Settings exposing the three guard modes (default reject)."""
    return SimpleNamespace(
        grove_guard_self_reference=modes.get("self_reference", "reject"),
        grove_guard_relationship_type=modes.get("relationship_type", "reject"),
        grove_guard_mention_in_body=modes.get("mention_in_body", "reject"),
    )


def _patch_settings(monkeypatch, **modes):
    import app.api.v1.ingestion as mod

    monkeypatch.setattr(mod, "get_settings", lambda: _settings(**modes))


def _reldef(**overrides):
    fields = {
        "id": uuid.uuid4(),
        "name": "rel_x",
        "source_ref_type": "entity_type",
        "source_ref_id": uuid.uuid4(),
        "target_ref_type": "entity_type",
        "target_ref_id": uuid.uuid4(),
        "creation_mode": "open",
        **overrides,
    }
    return RelationshipDefinition(**fields)


def _rel_payload(rdef, source_id, target_id):
    return RelationshipIn(
        relationship_definition_id=rdef.id,
        source_id=source_id,
        target_id=target_id,
    )


# ─────────────────────────────────────────────────────────────
# Guard 1 — self-referential relationship rejection
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_self_reference_allows_distinct_endpoints(monkeypatch):
    from app.api.v1.ingestion import record_relationship

    _patch_settings(monkeypatch)
    rdef = _reldef()
    a, b = uuid.uuid4(), uuid.uuid4()
    # both ends are the declared entity_type so the type guard (if present)
    # also passes; only distinctness is under test here.
    ea = Entity(id=a, entity_type_id=rdef.source_ref_id, canonical_form="A")
    eb = Entity(id=b, entity_type_id=rdef.target_ref_id, canonical_form="B")
    session = _FakeSession(gets={rdef.id: rdef, a: ea, b: eb})

    out = await record_relationship(_rel_payload(rdef, a, b), session=session)

    assert out.kind == "relationship"
    assert out.id is not None
    assert len(session.added) == 1
    assert session.committed is True


@pytest.mark.asyncio
async def test_self_reference_rejects_self_loop(monkeypatch):
    from app.api.v1.ingestion import record_relationship

    _patch_settings(monkeypatch)
    rdef = _reldef()
    node = uuid.uuid4()
    session = _FakeSession(gets={rdef.id: rdef})

    out = await record_relationship(_rel_payload(rdef, node, node), session=session)

    assert out.kind == "rejected"
    assert out.id is None
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_self_reference_warn_mode_still_writes(monkeypatch):
    from app.api.v1.ingestion import record_relationship

    # isolate guard 1: turn the type guard off so only self-reference is exercised.
    _patch_settings(monkeypatch, self_reference="warn", relationship_type="off")
    rdef = _reldef()
    node = uuid.uuid4()
    session = _FakeSession(gets={rdef.id: rdef})

    out = await record_relationship(_rel_payload(rdef, node, node), session=session)

    # warn logs but does not skip: the self-loop is still written.
    assert out.kind == "relationship"
    assert len(session.added) == 1
