"""T4 create-or-link: twin citations must not collide on natural_key.

A document citing the same case in two surface forms ("Case C-110/04" and
"Strintzis Lines Shipping v. Commission, Case C-110/04") sends both
mentions to the creation step with the same derived key. The first insert
must create; the second must LINK to it instead of violating
ix_entity_natural_key and rolling back the document's resolution. A
cross-session collision (owner created after the index was built) links
to the existing owner via the IntegrityError fallback.

Run from the backend directory:
`python -m pytest tests/test_entity_resolver_create_or_link.py`
"""

import contextlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.services import entity_resolver
from app.services.entity_resolver import _EntityIndex, resolve_document


class _ExecResult:
    def __init__(self, scalars=None, scalar=None):
        self._scalars = scalars or []
        self._scalar = scalar

    def scalars(self):
        rows = self._scalars
        return SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None)

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    """Serves mentions, then content, then any entity-owner queries."""

    def __init__(self, mentions, owner_rows=None, fail_flushes=0):
        self._mentions = mentions
        self._owner_rows = owner_rows or []
        self._fail_flushes = fail_flushes
        self.calls = 0
        self.added = []
        self.committed = False

    async def get(self, model, pk):
        return SimpleNamespace(filename="doc.md", current_version_id=None)

    async def execute(self, stmt):
        self.calls += 1
        s = str(stmt)
        if "entity_mention" in s:
            return _ExecResult(scalars=self._mentions)
        return _ExecResult(scalars=self._owner_rows)

    def add(self, obj):
        obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        if self._fail_flushes > 0:
            self._fail_flushes -= 1
            raise IntegrityError("INSERT INTO entity", {}, Exception("duplicate key"))

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        yield self

    async def commit(self):
        self.committed = True


class _NoJudgeSinas:
    async def invoke(self, agent, prompt):
        raise AssertionError("T3 must not fire in these tests")


def _mention(surface, type_id):
    return SimpleNamespace(
        surface_form=surface, span={"text": surface}, entity_id=None,
        entity_type_id=type_id, status="active",
        link_method=None, link_confidence=None, link_evidence=None,
    )


def _open_type(type_id):
    return {type_id: SimpleNamespace(id=type_id, name="Competition Decision / Case",
                                     creation_mode="open")}


@pytest.mark.asyncio
async def test_twin_citations_create_then_link_not_collide():
    tid = uuid.uuid4()
    m1 = _mention("Case C-110/04", tid)
    m2 = _mention("Strintzis Lines Shipping v. Commission, Case C-110/04", tid)
    session = _FakeSession([m1, m2])
    index = _EntityIndex([])
    report = await resolve_document(
        session, _NoJudgeSinas(), index, _open_type(tid), uuid.uuid4()
    )
    assert report["created"] == 1
    assert report["natural_key"] == 1
    assert len(session.added) == 1  # exactly one INSERT
    assert m1.entity_id is not None
    assert m2.entity_id == m1.entity_id  # twin linked to its sibling
    assert m2.link_method == "natural_key"
    assert m2.link_evidence["linked_instead_of_created"] is True


@pytest.mark.asyncio
async def test_cross_session_collision_links_to_existing_owner():
    tid = uuid.uuid4()
    owner = SimpleNamespace(id=uuid.uuid4(), entity_type_id=tid,
                            canonical_form="Case C-110/04",
                            natural_key="CJEU:C-110/04", merged_into_id=None)
    m = _mention("Case C-110/04", tid)
    session = _FakeSession([m], owner_rows=[owner], fail_flushes=1)
    index = _EntityIndex([])
    report = await resolve_document(
        session, _NoJudgeSinas(), index, _open_type(tid), uuid.uuid4()
    )
    assert report["natural_key"] == 1
    assert report["created"] == 0
    assert m.entity_id == owner.id
    assert m.link_evidence["linked_after_collision"] is True


@pytest.mark.asyncio
async def test_unkeyed_creation_path_unchanged():
    tid = uuid.uuid4()
    m = _mention("Some Plain Company Name", tid)
    session = _FakeSession([m])
    index = _EntityIndex([])
    report = await resolve_document(
        session, _NoJudgeSinas(), index, _open_type(tid), uuid.uuid4()
    )
    assert report["created"] == 1
    assert m.link_method == "created"


# ── name identity: the case natural keys never covered ──────────────────────
# 98.9% of entities have no natural key, so these paths had no database
# constraint behind them and no test in front of them.


class _NamedType(dict):
    pass


def _named_type(type_id):
    return {type_id: SimpleNamespace(id=type_id, name="Competition Authority",
                                     creation_mode="open")}


@pytest.mark.asyncio
async def test_name_twin_in_one_document_creates_once():
    """Two spellings of one authority in a document must not become two
    entities. Neither derives a natural key, so only normalized_form can
    catch it."""
    tid = uuid.uuid4()
    m1 = _mention("Competition and Markets Authority", tid)
    m2 = _mention("Competition and Markets  Authority", tid)  # doubled space
    session = _FakeSession([m1, m2])
    index = _EntityIndex([])
    report = await resolve_document(
        session, _NoJudgeSinas(), index, _named_type(tid), uuid.uuid4()
    )
    assert report["created"] == 1
    assert len([a for a in session.added if hasattr(a, "canonical_form")]) == 1
    assert m2.entity_id == m1.entity_id


@pytest.mark.asyncio
async def test_name_collision_across_sessions_links_not_duplicates():
    """An entity created concurrently is invisible to our index; the
    pre-insert lookup must find it and link rather than insert a twin."""
    tid = uuid.uuid4()
    owner = SimpleNamespace(
        id=uuid.uuid4(), entity_type_id=tid, canonical_form="Autorite de la concurrence",
        natural_key=None, normalized_form="autorite de la concurrence",
        merged_into_id=None)
    m = _mention("Autorité de la Concurrence", tid)   # accented spelling
    session = _FakeSession([m], owner_rows=[owner])
    index = _EntityIndex([])
    report = await resolve_document(
        session, _NoJudgeSinas(), index, _named_type(tid), uuid.uuid4()
    )
    assert report["created"] == 0, "must not create a twin"
    assert m.entity_id == owner.id
    assert not [a for a in session.added if hasattr(a, "canonical_form")]


def test_one_identity_function_shared_by_resolver_and_dedup():
    """Two normalizations meant two sets of duplicates: dedup stripped
    accented characters where the resolver folds them, so 'Autorité' and
    'Autorite' could never be recognised as the same name."""
    from app.entity_dedup import _norm
    from app.services.entity_resolver import normalize

    for s in ("Autorité de la Concurrence", "U.S. Department of Justice",
              "Anti-Trust  Division", "Bundeskartellamt"):
        assert _norm(s) == normalize(s)
    assert normalize("Autorité") == normalize("Autorite")
