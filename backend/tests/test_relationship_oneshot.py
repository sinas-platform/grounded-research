"""One-shot relationship extraction behavior.

One tool-less call per document; edges come back by NAME and the server
does the mapping: names resolve through the document's linked active
mentions with end-type validation against the definition; open+confident
edges become Relationships, review-mode or low-confidence edges become
proposals, cited-but-unmapped targets become UnresolvedRelationships (the
cite is never dropped); duplicates and type mismatches are skipped; an
unparseable reply writes nothing.

Run from the backend directory:
`python -m pytest tests/test_relationship_oneshot.py`
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.models.runtime import (
    Relationship,
    RelationshipProposal,
    UnresolvedRelationship,
)
from app.services.relationship_oneshot import (
    _apply_edges,
    build_prompt,
    extract_document,
)

CONTENT = (
    "JUDGMENT OF THE COURT. Intel Corporation appeals the decision of the "
    "European Commission in Case COMP/C-3/37.990. The Court cites "
    "ECLI:EU:C:2015:184 on the treatment of rebates."
)

DECISION_TYPE = uuid.uuid4()
AUTHORITY_TYPE = uuid.uuid4()
DOC_CLASS = uuid.uuid4()


def _definition(name, *, source=("entity_type", DECISION_TYPE),
                target=("entity_type", AUTHORITY_TYPE), mode="open"):
    return {
        "id": uuid.uuid4(),
        "name": name,
        "creation_mode": mode,
        "source_ref_type": source[0],
        "source_ref_id": source[1],
        "target_ref_type": target[0],
        "target_ref_id": target[1],
        "source_desc": "x",
        "target_desc": "y",
        "guidance": "",
    }


class _ExecResult:
    def __init__(self, scalars=None, scalar=None):
        self._scalars = scalars or []
        self._scalar = scalar

    def scalars(self):
        rows = self._scalars
        return SimpleNamespace(
            all=lambda: rows,
            first=lambda: rows[0] if rows else None,
            __iter__=lambda s: iter(rows),
        )

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    """Routes queries by table name in the rendered statement."""

    def __init__(self, *, doc=None, version=None, mentions=None,
                 entities=None, relationships=None, unresolved=None):
        self._doc = doc
        self._version = version
        self._mentions = mentions or []
        self._entities = entities or []
        self._relationships = relationships or []
        self._unresolved = unresolved or []
        self.added = []
        self.committed = False

    async def get(self, model, pk):
        return self._doc

    async def execute(self, stmt):
        s = str(stmt).lower()
        # route on the FROM clause: column names overlap across tables
        # (entity_mention.document_version_id, relationship.relationship_
        # definition_id) so substring checks on the whole statement lie
        if "from entity_mention" in s:
            return _ExecResult(scalars=self._mentions)
        if "from document_version" in s:
            return _ExecResult(scalars=[self._version] if self._version else [])
        if "from unresolved_relationship" in s:
            return _ExecResult(scalars=self._unresolved)
        if "from relationship_proposal" in s:
            return _ExecResult(scalars=[])
        if "from relationship" in s:
            return _ExecResult(scalars=self._relationships)
        if "from entity" in s:
            return _ExecResult(scalars=self._entities)
        return _ExecResult(scalars=[])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeSinas:
    def __init__(self, reply=""):
        self.reply = reply
        self.calls = []

    async def invoke(self, agent, prompt):
        self.calls.append((agent, prompt))
        return self.reply


def _entity(name, type_id):
    return SimpleNamespace(
        id=uuid.uuid4(), canonical_form=name, entity_type_id=type_id
    )


def _mention(entity):
    return SimpleNamespace(
        entity_id=entity.id, surface_form=entity.canonical_form, status="active"
    )


def _reply(*edges):
    return json.dumps({"relationships": list(edges)})


# ── _apply_edges: the deterministic mapping/write core ─────────────────────


@pytest.mark.asyncio
async def test_open_confident_edge_becomes_relationship():
    case = _entity("Case COMP/C-3/37.990", DECISION_TYPE)
    ec = _entity("European Commission", AUTHORITY_TYPE)
    d = _definition("issued_by")
    session = _FakeSession()
    report = await _apply_edges(
        session,
        document_id=uuid.uuid4(),
        content=CONTENT,
        edges=[{
            "definition": "issued_by",
            "source": "Case COMP/C-3/37.990",
            "target": "European Commission",
            "quote": "the decision of the European Commission in Case COMP/C-3/37.990",
            "confidence": 0.95,
        }],
        definitions=[d],
        name_to_entity={
            "case comp/c-3/37.990": case.id, "european commission": ec.id
        },
        entity_types={case.id: DECISION_TYPE, ec.id: AUTHORITY_TYPE},
        doc_class_id=None,
        write=True,
    )
    assert report["relationships"] == 1
    (row,) = session.added
    assert isinstance(row, Relationship)
    assert row.source_id == case.id and row.target_id == ec.id
    assert row.evidence_span["char_start"] > 0  # quote located verbatim


@pytest.mark.asyncio
async def test_low_confidence_becomes_proposal_and_review_mode_too():
    case = _entity("Case A", DECISION_TYPE)
    ec = _entity("Commission", AUTHORITY_TYPE)
    names = {"case a": case.id, "commission": ec.id}
    types = {case.id: DECISION_TYPE, ec.id: AUTHORITY_TYPE}
    for d, conf in (
        (_definition("issued_by"), 0.5),           # open but unsure
        (_definition("issued_by", mode="review"), 0.95),  # review mode
    ):
        session = _FakeSession()
        report = await _apply_edges(
            session, document_id=uuid.uuid4(), content=CONTENT,
            edges=[{"definition": "issued_by", "source": "Case A",
                    "target": "Commission", "quote": "", "confidence": conf}],
            definitions=[d], name_to_entity=names, entity_types=types,
            doc_class_id=None, write=True,
        )
        assert report["proposals"] == 1, (d["creation_mode"], conf)
        assert isinstance(session.added[0], RelationshipProposal)
        assert session.added[0].status == "pending"


@pytest.mark.asyncio
async def test_document_source_maps_to_document_id():
    ec = _entity("European Commission", AUTHORITY_TYPE)
    doc_id = uuid.uuid4()
    d = _definition("cites", source=("document_class", DOC_CLASS))
    session = _FakeSession()
    report = await _apply_edges(
        session, document_id=doc_id, content=CONTENT,
        edges=[{"definition": "cites", "source": "DOCUMENT",
                "target": "European Commission", "quote": "", "confidence": 0.9}],
        definitions=[d],
        name_to_entity={"european commission": ec.id},
        entity_types={ec.id: AUTHORITY_TYPE},
        doc_class_id=DOC_CLASS,
        write=True,
    )
    assert report["relationships"] == 1
    assert session.added[0].source_id == doc_id


@pytest.mark.asyncio
async def test_document_source_with_wrong_class_is_skipped():
    ec = _entity("European Commission", AUTHORITY_TYPE)
    d = _definition("cites", source=("document_class", DOC_CLASS))
    session = _FakeSession()
    report = await _apply_edges(
        session, document_id=uuid.uuid4(), content=CONTENT,
        edges=[{"definition": "cites", "source": "DOCUMENT",
                "target": "European Commission", "quote": "", "confidence": 0.9}],
        definitions=[d],
        name_to_entity={"european commission": ec.id},
        entity_types={ec.id: AUTHORITY_TYPE},
        doc_class_id=uuid.uuid4(),  # a different class than the definition wants
        write=True,
    )
    assert report["skipped_type_mismatch"] == 1
    assert session.added == []


@pytest.mark.asyncio
async def test_cited_unmapped_target_parks_as_unresolved_not_dropped():
    case = _entity("Case A", DECISION_TYPE)
    d = _definition("cites_legal_instrument",
                    target=("entity_type", DECISION_TYPE))
    session = _FakeSession()
    report = await _apply_edges(
        session, document_id=uuid.uuid4(), content=CONTENT,
        edges=[{"definition": "cites_legal_instrument", "source": "Case A",
                "target": "ECLI:EU:C:2015:184",
                "target_reference": "ECLI:EU:C:2015:184",
                "quote": "The Court cites ECLI:EU:C:2015:184",
                "confidence": 0.9}],
        definitions=[d],
        name_to_entity={"case a": case.id},
        entity_types={case.id: DECISION_TYPE},
        doc_class_id=None, write=True,
    )
    assert report["unresolved"] == 1
    (row,) = session.added
    assert isinstance(row, UnresolvedRelationship)
    assert row.target_key == "ECLI:EU:C:2015:184"
    assert row.status == "unresolved"


@pytest.mark.asyncio
async def test_type_mismatched_end_is_skipped_not_written():
    case = _entity("Case A", DECISION_TYPE)
    ec = _entity("Commission", AUTHORITY_TYPE)
    d = _definition("issued_by")  # wants DECISION → AUTHORITY
    session = _FakeSession()
    report = await _apply_edges(
        session, document_id=uuid.uuid4(), content=CONTENT,
        edges=[{"definition": "issued_by", "source": "Commission",
                "target": "Case A", "quote": "", "confidence": 0.9}],
        definitions=[d],
        name_to_entity={"case a": case.id, "commission": ec.id},
        entity_types={case.id: DECISION_TYPE, ec.id: AUTHORITY_TYPE},
        doc_class_id=None, write=True,
    )
    # source "Commission" is AUTHORITY, definition wants DECISION as source
    assert report["skipped_type_mismatch"] == 1
    assert session.added == []


@pytest.mark.asyncio
async def test_existing_edge_is_not_duplicated():
    case = _entity("Case A", DECISION_TYPE)
    ec = _entity("Commission", AUTHORITY_TYPE)
    d = _definition("issued_by")
    doc_id = uuid.uuid4()
    existing = SimpleNamespace(
        relationship_definition_id=d["id"], source_id=case.id, target_id=ec.id
    )
    session = _FakeSession(relationships=[existing])
    report = await _apply_edges(
        session, document_id=doc_id, content=CONTENT,
        edges=[{"definition": "issued_by", "source": "Case A",
                "target": "Commission", "quote": "", "confidence": 0.9}],
        definitions=[d],
        name_to_entity={"case a": case.id, "commission": ec.id},
        entity_types={case.id: DECISION_TYPE, ec.id: AUTHORITY_TYPE},
        doc_class_id=None, write=True,
    )
    assert report["skipped_duplicate"] == 1
    assert session.added == []


@pytest.mark.asyncio
async def test_unknown_definition_is_counted_and_skipped():
    session = _FakeSession()
    report = await _apply_edges(
        session, document_id=uuid.uuid4(), content=CONTENT,
        edges=[{"definition": "invented_by_the_model", "source": "A",
                "target": "B", "quote": "", "confidence": 0.9}],
        definitions=[_definition("issued_by")],
        name_to_entity={}, entity_types={}, doc_class_id=None, write=True,
    )
    assert report["skipped_unknown_definition"] == 1
    assert session.added == []


# ── extract_document: the per-document driver ──────────────────────────────


def _doc_session(mentions, entities):
    doc = SimpleNamespace(
        filename="intel.md", document_class_id=DOC_CLASS,
    )
    version = SimpleNamespace(content_md=CONTENT, version=1)
    return _FakeSession(
        doc=doc, version=version, mentions=mentions, entities=entities
    )


@pytest.mark.asyncio
async def test_one_call_per_document_and_edges_written():
    case = _entity("Case COMP/C-3/37.990", DECISION_TYPE)
    ec = _entity("European Commission", AUTHORITY_TYPE)
    session = _doc_session([_mention(case), _mention(ec)], [case, ec])
    d = _definition("issued_by")
    sinas = _FakeSinas(_reply({
        "definition": "issued_by", "source": "Case COMP/C-3/37.990",
        "target": "European Commission",
        "quote": "decision of the European Commission", "confidence": 0.9,
    }))
    report = await extract_document(
        session, sinas, uuid.uuid4(), definitions=[d], write=True
    )
    assert report["llm_calls"] == 1
    assert len(sinas.calls) == 1
    assert report["relationships"] == 1
    assert session.committed


@pytest.mark.asyncio
async def test_unparseable_reply_writes_nothing():
    case = _entity("Case A", DECISION_TYPE)
    session = _doc_session([_mention(case)], [case])
    sinas = _FakeSinas("I found no relationships, sorry!")
    report = await extract_document(
        session, sinas, uuid.uuid4(), definitions=[_definition("issued_by")],
        write=True,
    )
    assert report.get("unparsed") is True
    assert session.added == []


@pytest.mark.asyncio
async def test_no_linked_mentions_skips_without_llm_call():
    session = _doc_session([], [])
    sinas = _FakeSinas()
    report = await extract_document(
        session, sinas, uuid.uuid4(), definitions=[_definition("issued_by")],
        write=True,
    )
    assert report["skipped"] == "no linked mentions"
    assert sinas.calls == []


@pytest.mark.asyncio
async def test_long_document_is_chunked_and_edges_merged():
    from app.services.relationship_oneshot import _CHUNK_CHARS

    case = _entity("Case COMP/C-3/37.990", DECISION_TYPE)
    ec = _entity("European Commission", AUTHORITY_TYPE)
    long_content = CONTENT + ("\nfiller " * ((_CHUNK_CHARS * 2) // 8))
    doc = SimpleNamespace(filename="long.md", document_class_id=DOC_CLASS)
    version = SimpleNamespace(content_md=long_content, version=1)
    session = _FakeSession(
        doc=doc, version=version,
        mentions=[_mention(case), _mention(ec)], entities=[case, ec],
    )

    class _MultiSinas:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = []

        async def invoke(self, agent, prompt):
            self.calls.append(prompt)
            return self.replies[len(self.calls) - 1]

    edge = {
        "definition": "issued_by", "source": "Case COMP/C-3/37.990",
        "target": "European Commission", "quote": "x", "confidence": 0.9,
    }
    # same edge from two chunks (overlap) + one unparseable chunk: the
    # duplicate collapses, the bad chunk doesn't sink the document
    sinas = _MultiSinas([_reply(edge), _reply(edge), "no json here"])
    report = await extract_document(
        session, sinas, uuid.uuid4(), definitions=[_definition("issued_by")],
        write=True,
    )
    assert report["chunks"] == 3
    assert report["llm_calls"] == 3
    assert report["unparsed_chunks"] == 1
    assert report["relationships"] == 1
    assert report["skipped_duplicate"] == 1


def test_prompt_carries_definitions_guidance_and_names():
    d = _definition("issued_by")
    d["guidance"] = "Link each decision to its issuing authority."
    p = build_prompt(
        filename="intel.md", content=CONTENT, definitions=[d],
        mention_names=["European Commission", "Intel Corporation"],
    )
    assert "issued_by" in p
    assert "Link each decision to its issuing authority." in p
    assert "- European Commission" in p
    assert "Intel Corporation" in p
    assert "JUDGMENT OF THE COURT" in p
