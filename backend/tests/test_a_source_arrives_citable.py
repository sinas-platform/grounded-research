"""What a reader needs to cite a source reaches the reader.

Every read that served a source served its filename. Assembling a citation —
name, number, ECLI, date, and whether the instrument is still in force —
took a property-values call per document, so a consumer holding one document
and no second call cited the filename stem. That is the defect the expert
review named, and it is an API shape, not a prompt.

These tests pin what each surface now carries: the declared properties on
result-document rows and on a single-document read, every structured field
on a claim row, the locator inside the evidence row's span, and the two
answer-level facts on the run object a consumer actually holds.

The last group pins the deployment side: a domain config declaring `status`,
`superseded_by`, `jurisdiction` and ECLI guidance imports as ordinary class
properties through a schema that forbids unknown keys.

Run from the backend directory:
`python -m pytest tests/test_a_source_arrives_citable.py`
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from app.api.v1.query_runs import QueryRunOut, get_query_run
from app.schemas.package import PackageDocumentClassEntry, PackagePropertyEntry, SgrPackage
from app.schemas.runtime import ClaimEvidenceOut, ClaimOut, DocumentOut, ResultDocumentOut
from app.services.document_identity import properties_for_documents
from pydantic import ValidationError


def _stamps():
    now = datetime.now(UTC)
    return {"id": uuid.uuid4(), "created_at": now, "updated_at": now}


# ── the rows that name a source ──────────────────────────────────────────────

def test_a_result_document_row_carries_the_class_properties():
    assert "properties" in ResultDocumentOut.model_fields
    row = ResultDocumentOut(
        **_stamps(), result_id=uuid.uuid4(), document_id=uuid.uuid4(),
        title="Kestrel Holdings v Northmoor Authority", external_ref="T-100/20",
        properties={"case_number": "T-100/20", "ecli": "ECLI:XX:YY:2021:1",
                    "decision_date": "2021-05-04", "status": "in_force"})
    assert row.properties["ecli"] == "ECLI:XX:YY:2021:1"


def test_a_single_document_read_carries_properties_and_annotations():
    """Both, on one read. They answer different questions — what the source
    IS, and where it sits in the authority hierarchy — and a citation needs
    the first while a labelled citation needs the second."""
    for field in ("properties", "annotations", "title", "external_ref"):
        assert field in DocumentOut.model_fields, field


class _PropRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _PropSession:
    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _PropRows(self._rows)


@pytest.mark.asyncio
async def test_property_values_come_back_unwrapped_and_keyed_by_document():
    """Scalars are stored wrapped as {"_": x} so they fit a JSONB dict
    column. A reader that gets the wrapper back has to know that, and one
    that does not prints `{'_': 'T-100/20'}` into a citation."""
    a, b = uuid.uuid4(), uuid.uuid4()
    session = _PropSession([
        (a, "case_number", {"_": "T-100/20"}),
        (a, "status", {"_": "repealed"}),
        (a, "nothing_extracted", None),
        (b, "celex", "3XX0001"),
    ])
    out = await properties_for_documents(session, [a, b])
    assert out[a] == {"case_number": "T-100/20", "status": "repealed"}
    assert out[b] == {"celex": "3XX0001"}


@pytest.mark.asyncio
async def test_asking_about_no_documents_asks_the_database_nothing():
    session = _PropSession([])
    assert await properties_for_documents(session, []) == {}
    assert session.statements == []


# ── the rows that carry the argument ─────────────────────────────────────────

def test_a_claim_row_carries_every_structured_field():
    """A renderer orders by (section, part_index, position) and labels by
    authority_label; `follows_from` is what makes an inference checkable.
    Serving the text and the type alone is the flat list again."""
    expected = {"id", "section", "part_index", "part_label", "position",
                "claim_kind", "test", "authority_label", "authority_tier",
                "jurisdiction_note", "currency_note", "follows_from"}
    assert expected <= set(ClaimOut.model_fields)


def test_an_evidence_rows_span_carries_its_locator():
    """The label travels inside the span, beside the coordinates it labels:
    a renderer reads a citation's position out of one object rather than
    pairing two fields and occasionally pairing them wrong."""
    row = ClaimEvidenceOut(
        **_stamps(), claim_id=uuid.uuid4(), document_id=uuid.uuid4(),
        span={"line_from": 3, "line_to": 5}, stance="supports",
        validated=True, paragraph_ref="r.o. 4.2")
    assert row.span == {"line_from": 3, "line_to": 5, "paragraph_ref": "r.o. 4.2"}


def test_a_span_with_no_locator_says_so_rather_than_omitting_the_key():
    """"This row has no locator" and "this API is older than locators" are
    different facts, and a missing key cannot tell them apart."""
    row = ClaimEvidenceOut(
        **_stamps(), claim_id=uuid.uuid4(), document_id=uuid.uuid4(),
        span={"line_from": 3}, stance="supports", validated=False)
    assert row.span["paragraph_ref"] is None


# ── the run a consumer holds ─────────────────────────────────────────────────

class _Run:
    def __init__(self, answer_id):
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.created_at = self.updated_at = now
        self.owner_id = uuid.uuid4()
        self.roles = []
        self.question = "Does it apply?"
        self.reference = self.title = self.change_note = None
        self.tags = []
        self.mode, self.effort, self.status = "full", "medium", "published"
        self.subqueries = None
        self.parent_result_id = None
        self.answer_id = answer_id
        self.error = None
        self.telemetry = {}
        self.started_at = self.completed_at = None


class _AnswerRow:
    law_stated_as_at = date(2024, 3, 1)
    question_parts = [{"index": 0, "label": "Whether it applies", "text": "..."}]


class _RunExec:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _RunSession:
    def __init__(self, run, answer):
        self._run, self._answer = run, answer

    async def execute(self, stmt):
        return _RunExec(self._run)

    async def get(self, model, pk):
        return self._answer


class _Caller:
    user_id = uuid.uuid4()
    roles: list[str] = []
    is_admin = True

    async def has_permission(self, permission):
        return True


@pytest.mark.asyncio
async def test_the_run_says_how_the_question_was_read_and_how_current_it_is():
    """A downstream consumer holds the run id. Making it fetch the answer to
    learn the decomposition and the as-at date is a round trip for two
    facts the run already knows where to find."""
    run = _Run(answer_id=uuid.uuid4())
    out = await get_query_run(
        run.id, session=_RunSession(run, _AnswerRow()), caller=_Caller())
    assert out.law_stated_as_at == date(2024, 3, 1)
    assert out.question_parts[0]["label"] == "Whether it applies"


@pytest.mark.asyncio
async def test_a_run_with_no_answer_yet_reports_neither_rather_than_guessing():
    run = _Run(answer_id=None)
    out = await get_query_run(
        run.id, session=_RunSession(run, None), caller=_Caller())
    assert out.law_stated_as_at is None
    assert out.question_parts is None


def test_both_fields_are_declared_on_the_run_object():
    assert {"law_stated_as_at", "question_parts"} <= set(QueryRunOut.model_fields)


# ── what a deployment declares ───────────────────────────────────────────────

def _class_entry(**over) -> dict:
    return {
        "name": "Legislation",
        "properties": [
            {"name": "status",
             "description": "Whether the instrument is still in force.",
             "schema": {"type": "string",
                        "enum": ["in_force", "amended", "repealed",
                                 "superseded"]}},
            {"name": "superseded_by",
             "description": "What replaced it, where it was replaced.",
             "schema": {"type": "string"}},
            {"name": "jurisdiction",
             "schema": {"type": "string"},
             "guidance": "The body of law the instrument belongs to."},
            {"name": "ecli",
             "schema": {"type": "string"},
             "guidance": "The source's ECLI, exactly as the document prints "
                         "it. Leave empty rather than deriving one."},
        ],
        **over,
    }


def test_currency_and_jurisdiction_import_as_ordinary_class_properties():
    """Nothing about them is special to the schema. The engine reads
    `status`, `superseded_by` and a jurisdiction-shaped property by NAME off
    whatever the deployment declared, so the package needs no new key for
    them — and a package schema that forbids unknown keys must still take
    them without one."""
    entry = PackageDocumentClassEntry.model_validate(_class_entry())
    by_name = {p.name: p for p in entry.properties}
    assert by_name["status"].schema["enum"] == [
        "in_force", "amended", "repealed", "superseded"]
    assert by_name["superseded_by"].schema == {"type": "string"}
    assert "ECLI" in by_name["ecli"].guidance
    assert by_name["jurisdiction"].cardinality == "one"


def test_a_whole_package_declaring_them_validates():
    pkg = SgrPackage.model_validate({
        "metadata": {"name": "a-deployment"},
        "package": {"name": "a-deployment", "version": "1.0.0"},
        "spec": {"document_classes": [_class_entry()]},
    })
    assert [p.name for p in pkg.spec.document_classes[0].properties] == [
        "status", "superseded_by", "jurisdiction", "ecli"]


def test_the_rejected_paragraph_pattern_key_is_refused_by_the_schema():
    """Not merely absent from the config — refused. The schema forbids
    unknown keys, so a deployment that adds it back gets an import error
    rather than a key the engine silently ignores."""
    with pytest.raises(ValidationError):
        PackageDocumentClassEntry.model_validate(
            _class_entry(paragraph_pattern=r"^\d+\.\s"))


def test_an_unknown_property_key_is_refused_too():
    with pytest.raises(ValidationError):
        PackagePropertyEntry.model_validate(
            {"name": "status", "pattern": r"^\d+"})
