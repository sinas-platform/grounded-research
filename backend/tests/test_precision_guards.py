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
from app.api.v1.ingestion import EntityIn, EntityMentionInWithBody
from app.models import (
    Document,
    DocumentVersion,
    Entity,
    EntityType,
    RelationshipDefinition,
)
from app.schemas.runtime import RelationshipIn


class _Exec:
    """execute() result stand-in exposing scalar_one_or_none (used by
    _alias_match inside propose_new_entity)."""

    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


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
        from datetime import UTC, datetime

        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        now = datetime.now(UTC)
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


# ─────────────────────────────────────────────────────────────
# Guard 2 — target/source type validation against the definition
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_relationship_type_allows_matching_types(monkeypatch):
    from app.api.v1.ingestion import record_relationship

    _patch_settings(monkeypatch)
    rdef = _reldef()  # source/target are distinct entity_types
    a, b = uuid.uuid4(), uuid.uuid4()
    ea = Entity(id=a, entity_type_id=rdef.source_ref_id, canonical_form="A")
    eb = Entity(id=b, entity_type_id=rdef.target_ref_id, canonical_form="B")
    session = _FakeSession(gets={rdef.id: rdef, a: ea, b: eb})

    out = await record_relationship(_rel_payload(rdef, a, b), session=session)

    assert out.kind == "relationship"
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_relationship_type_rejects_wrong_target(monkeypatch):
    from app.api.v1.ingestion import record_relationship

    _patch_settings(monkeypatch)
    rdef = _reldef()
    a, b = uuid.uuid4(), uuid.uuid4()
    ea = Entity(id=a, entity_type_id=rdef.source_ref_id, canonical_form="A")
    # target is some other entity_type, not the one the definition declares.
    eb = Entity(id=b, entity_type_id=uuid.uuid4(), canonical_form="B")
    session = _FakeSession(gets={rdef.id: rdef, a: ea, b: eb})

    out = await record_relationship(_rel_payload(rdef, a, b), session=session)

    assert out.kind == "rejected"
    assert out.id is None
    assert session.added == []


@pytest.mark.asyncio
async def test_relationship_type_rejects_missing_node(monkeypatch):
    from app.api.v1.ingestion import record_relationship

    _patch_settings(monkeypatch)
    rdef = _reldef()
    a, b = uuid.uuid4(), uuid.uuid4()
    ea = Entity(id=a, entity_type_id=rdef.source_ref_id, canonical_form="A")
    # target node not present at all.
    session = _FakeSession(gets={rdef.id: rdef, a: ea})

    out = await record_relationship(_rel_payload(rdef, a, b), session=session)

    assert out.kind == "rejected"
    assert session.added == []


@pytest.mark.asyncio
async def test_relationship_type_polymorphic_document_class(monkeypatch):
    """A definition whose target is a document_class validates the target
    against Document.document_class_id — the rule follows ref_type generically."""
    from app.api.v1.ingestion import record_relationship

    _patch_settings(monkeypatch)
    doc_class = uuid.uuid4()
    rdef = _reldef(target_ref_type="document_class", target_ref_id=doc_class)
    a, b = uuid.uuid4(), uuid.uuid4()
    ea = Entity(id=a, entity_type_id=rdef.source_ref_id, canonical_form="A")

    ok_doc = Document(id=b, document_class_id=doc_class, filename="f")
    session = _FakeSession(gets={rdef.id: rdef, a: ea, b: ok_doc})
    out = await record_relationship(_rel_payload(rdef, a, b), session=session)
    assert out.kind == "relationship"

    wrong_doc = Document(id=b, document_class_id=uuid.uuid4(), filename="f")
    session = _FakeSession(gets={rdef.id: rdef, a: ea, b: wrong_doc})
    out = await record_relationship(_rel_payload(rdef, a, b), session=session)
    assert out.kind == "rejected"


@pytest.mark.asyncio
async def test_relationship_type_unknown_ref_type_not_enforced(monkeypatch):
    """An unrecognised ref_type cannot be checked from config, so it is left
    unenforced rather than blocking the write."""
    from app.api.v1.ingestion import record_relationship

    _patch_settings(monkeypatch)
    rdef = _reldef(target_ref_type="something_new", target_ref_id=uuid.uuid4())
    a, b = uuid.uuid4(), uuid.uuid4()
    ea = Entity(id=a, entity_type_id=rdef.source_ref_id, canonical_form="A")
    session = _FakeSession(gets={rdef.id: rdef, a: ea})  # b intentionally absent

    out = await record_relationship(_rel_payload(rdef, a, b), session=session)

    assert out.kind == "relationship"
    assert len(session.added) == 1


# ─────────────────────────────────────────────────────────────
# Guard 3 — mention span must point at real body text
# ─────────────────────────────────────────────────────────────
def _mention(span, dv_id=None):
    dv_id = dv_id or uuid.uuid4()
    payload = EntityMentionInWithBody(
        document_id=uuid.uuid4(),
        document_version_id=dv_id,
        entity_id=uuid.uuid4(),
        span=span,
    )
    return payload, dv_id


def _body_session(dv_id, content_md):
    return _FakeSession(gets={dv_id: DocumentVersion(id=dv_id, content_md=content_md)})


@pytest.mark.asyncio
async def test_mention_char_span_in_bounds_writes(monkeypatch):
    from app.api.v1.ingestion import record_entity_mention

    _patch_settings(monkeypatch)
    payload, dv = _mention({"char_from": 0, "char_to": 5})
    session = _body_session(dv, "hello world")

    out = await record_entity_mention(payload, session=session)

    assert out["id"] is not None
    assert out["rejected"] is False
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_mention_char_span_out_of_bounds_rejected(monkeypatch):
    from app.api.v1.ingestion import record_entity_mention

    _patch_settings(monkeypatch)
    payload, dv = _mention({"char_from": 5, "char_to": 100})
    session = _body_session(dv, "hello world")

    out = await record_entity_mention(payload, session=session)

    assert out["id"] is None
    assert out["rejected"] is True
    assert session.added == []


@pytest.mark.asyncio
async def test_mention_whitespace_span_rejected(monkeypatch):
    from app.api.v1.ingestion import record_entity_mention

    _patch_settings(monkeypatch)
    payload, dv = _mention({"char_from": 2, "char_to": 5})
    session = _body_session(dv, "ab   cd")  # [2:5] is whitespace

    out = await record_entity_mention(payload, session=session)

    assert out["rejected"] is True
    assert session.added == []


@pytest.mark.asyncio
async def test_mention_line_span_out_of_bounds_rejected(monkeypatch):
    from app.api.v1.ingestion import record_entity_mention

    _patch_settings(monkeypatch)
    payload, dv = _mention({"line_from": 5, "line_to": 6})
    session = _body_session(dv, "line one\nline two")  # 2 lines

    out = await record_entity_mention(payload, session=session)

    assert out["rejected"] is True


@pytest.mark.asyncio
async def test_mention_no_body_allows(monkeypatch):
    """No extracted text (e.g. a binary doc) → unverifiable → allowed."""
    from app.api.v1.ingestion import record_entity_mention

    _patch_settings(monkeypatch)
    payload, dv = _mention({"char_from": 0, "char_to": 9999})
    session = _body_session(dv, None)

    out = await record_entity_mention(payload, session=session)

    assert out["id"] is not None
    assert out["rejected"] is False
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_mention_no_offsets_allows(monkeypatch):
    """A span carrying neither char nor line offsets is unverifiable → allowed
    (avoids false-rejecting valid mentions)."""
    from app.api.v1.ingestion import record_entity_mention

    _patch_settings(monkeypatch)
    payload, dv = _mention({"note": "somewhere"})
    session = _body_session(dv, "hello world")

    out = await record_entity_mention(payload, session=session)

    assert out["id"] is not None
    assert out["rejected"] is False


@pytest.mark.asyncio
async def test_propose_entity_rejects_bad_evidence_span(monkeypatch):
    """propose_new_entity refuses to create from evidence whose span is out of
    the body's bounds."""
    from app.api.v1.ingestion import propose_new_entity

    _patch_settings(monkeypatch)
    et_id, dv_id = uuid.uuid4(), uuid.uuid4()
    et = EntityType(id=et_id, name="TypeA", creation_mode="open")
    dv = DocumentVersion(id=dv_id, content_md="short body")
    # two _alias_match queries return no match, then guard loads the body.
    session = _FakeSession(
        gets={et_id: et, dv_id: dv}, results=[_Exec(None), _Exec(None)]
    )
    payload = EntityIn(
        entity_type_id=et_id,
        canonical_form="A",
        document_id=uuid.uuid4(),
        document_version_id=dv_id,
        span={"char_from": 100, "char_to": 200},
    )

    out = await propose_new_entity(payload, session=session)

    assert out.kind == "rejected"
    assert out.id is None
    assert session.added == []


@pytest.mark.asyncio
async def test_propose_entity_allows_good_evidence_span(monkeypatch):
    from app.api.v1.ingestion import propose_new_entity

    _patch_settings(monkeypatch)
    et_id, dv_id = uuid.uuid4(), uuid.uuid4()
    et = EntityType(id=et_id, name="TypeA", creation_mode="open")
    dv = DocumentVersion(id=dv_id, content_md="short body")
    session = _FakeSession(
        gets={et_id: et, dv_id: dv}, results=[_Exec(None), _Exec(None)]
    )
    payload = EntityIn(
        entity_type_id=et_id,
        canonical_form="A",
        document_id=uuid.uuid4(),
        document_version_id=dv_id,
        span={"char_from": 0, "char_to": 5},
    )

    out = await propose_new_entity(payload, session=session)

    assert out.kind == "entity"
    assert len(session.added) == 1
