"""A deployment declares what each of its fields means to the engine.

The companion of `test_a_relationship_says_what_it_means`, for the two things
that are not edges. The engine used to read meaning off a NAME: a property
whose name mentioned a date was the source's date, one matching
`jurisdiction|country|member_?state` was its jurisdiction, currency came from
properties literally called `status` and `superseded_by`, a citation's number
was whichever of four spellings existed, and the tier and the issuing body
were annotations found by two more names. Every one is a guess about another
party's vocabulary, and a guess that matches nothing produces an answer with
no date, no jurisdiction note, no currency note and no identifier — which is
byte-identical to a corpus that holds none of those.

A class now says, on the property itself, what that property means
(`engine_role: date`), and an annotation says the same. What is checked here
is that the declaration is read where it is written, that the readers of an
answer take it from what the deployment declared rather than from a name, and
that an undeclared role switches its rule off and says so instead of guessing.

The fixture deployment deliberately names nothing the way the old guesses
expected: its date property is `handed_down`, its jurisdiction `where_it_binds`
and its status `how_it_stands`. Every rule below still works, which is the
whole claim.

Run from the backend directory:
`python -m pytest tests/test_a_property_says_what_it_means.py`
"""

from __future__ import annotations

from datetime import date

import pytest
from app.schemas.package import ANNOTATION_ROLES, PROPERTY_ROLES
from app.services import answer_structure as st
from app.services import declared_roles as dr
from app.services import package as package_service

# ── the declaration, as a package writes it ──────────────────────────────────

_PACKAGE = """
apiVersion: sgr.sinas.co/v1
kind: SgrPackage
metadata:
  name: demo
package:
  name: demo
  version: "0.1.0"
spec:
  document_classes:
    - name: Filing
      identifier_property: docket
      identifier_pattern: '^D-[0-9]+$'
      properties:
        - name: docket
        - name: handed_down
          engine_role: {date_role}
        - name: where_it_binds
          engine_role: jurisdiction
{extra_property}
  annotations:
    - name: how_high_it_stands
      path: issued
      reduce: length
      engine_role: {annotation_role}
"""


def _validate(*, date_role: str = "date", annotation_role: str = "standing_tier",
              extra_property: str = ""):
    return package_service.validate(_PACKAGE.format(
        date_role=date_role, annotation_role=annotation_role,
        extra_property=extra_property))


def test_a_package_declaring_what_its_fields_mean_validates():
    """The names are the deployment's own and mean nothing to the engine; the
    roles are the engine's and mean the same in every deployment."""
    result = _validate()
    assert result.valid, result.errors


def test_a_role_the_engine_does_not_read_is_refused():
    """A declaration nothing acts on is the shape of the defect, not a fix."""
    assert not _validate(date_role="vibes").valid
    assert not _validate(annotation_role="vibes").valid


def test_one_class_does_not_name_two_properties_for_one_role():
    """Two dates on one class would leave the engine to choose at read time,
    which is the guessing this mechanism removes. It is refused where it is
    written."""
    result = _validate(extra_property=(
        "        - name: also_a_date\n          engine_role: date\n"))
    assert not result.valid
    assert any("date" in e for e in result.errors), result.errors


def test_the_engine_reads_exactly_the_roles_the_schema_offers():
    """One vocabulary, held in `schemas.package` and named in
    `declared_roles`: a role the schema accepts and no reader asks for is a
    declaration that does nothing."""
    assert set(PROPERTY_ROLES) == {dr.DATE, dr.JURISDICTION, dr.STATUS,
                                   dr.SUPERSEDED_BY, dr.ALTERNATE_IDENTIFIER}
    assert set(ANNOTATION_ROLES) == {dr.STANDING_TIER, dr.ISSUING_BODY}


# ── what the readers do with it ──────────────────────────────────────────────

#: What the fixture deployment declared. Not one of these names is one the
#: engine could have guessed.
ROLES = dr.DeclaredRoles(
    date={"Filing": "handed_down", "Note": "written_on"},
    jurisdiction={"Filing": "where_it_binds"},
    status={"Filing": "how_it_stands"},
    superseded_by={"Filing": "replaced_by"},
    alternate_identifier={"Filing": "neutral_id"},
    tier_annotations=("how_high_it_stands",),
    issuing_body_annotation="who_issued_it",
)


def _row(filename: str, **props) -> dict:
    return {"filename": filename, "class": "Filing", "props": props}


def test_the_date_is_the_property_the_class_declares():
    props = {"handed_down": "2021-05-04", "filed_on": "2024-01-01"}
    assert st.document_date(props, "Filing", ROLES.date) == date(2021, 5, 4)


def test_two_classes_may_date_themselves_differently():
    """The declaration sits on the property, and properties belong to
    classes, so one deployment's two classes need not agree on a name."""
    assert st.document_date({"written_on": "2024-03-01"}, "Note",
                            ROLES.date) == date(2024, 3, 1)


def test_a_class_that_declares_no_date_has_no_date():
    """Not "look for something that sounds like one". The engine used to take
    the latest value among properties whose name mentioned a date, so a class
    calling it anything else was undated in silence — and a class with a
    `filed_on` beside its real date could be dated by the wrong one."""
    props = {"handed_down": "2021-05-04"}
    assert st.document_date(props, "Unknown", ROLES.date) is None
    assert st.document_date(props, "Filing", dr.NONE.date) is None


def test_the_jurisdiction_is_the_declared_property():
    assert st.jurisdiction_of({"where_it_binds": "Here"}, "Filing",
                              ROLES.jurisdiction) == "Here"
    assert st.jurisdiction_of({"where_it_binds": "Here"}, "Filing",
                              dr.NONE.jurisdiction) is None


def test_currency_reads_the_status_the_class_declares():
    """`status` and `superseded_by` were read by those literal names, so a
    class spelling either differently was current law for ever."""
    notes = st.currency_notes(
        [_row("a.md", how_it_stands="repealed", replaced_by="a later one")],
        ROLES)
    assert notes == {"a.md": "repealed; superseded by a later one"}


def test_what_counts_as_not_current_stays_the_engines_own_word():
    """WHICH property says how a source stands is the deployment's to name;
    WHAT its value must say to mean "not current law" is the contract a class
    writes its values against, and it lives in the engine."""
    assert set(st.STALE_STATUSES) == {"repealed", "superseded"}
    assert st.currency_notes([_row("a.md", how_it_stands="in force")],
                             ROLES) == {}


def test_a_status_the_contract_does_not_know_is_counted_not_swallowed():
    """A value outside the contract produces no note, and neither does no
    value at all. The two are opposite and looked identical: an instrument
    that is no longer law read as law and nothing said so.

    The case that brought it up is a source whose status extracts in its own
    language. The repair is the deployment's mapping; this is how anyone
    learns the mapping is missing."""
    rows = [_row("a.md", how_it_stands="abroge"),
            _row("b.md", how_it_stands="abroge"),
            _row("c.md", how_it_stands="in force"),
            _row("d.md", how_it_stands="repealed")]
    assert st.currency_notes(rows, ROLES) == {"d.md": "repealed"}
    assert st.unrecognised_statuses(rows, ROLES) == {"abroge": 2, "in force": 1}


def test_a_status_declared_many_is_read_as_its_values():
    """`cardinality: many` is legal on a status property and extraction then
    returns a list. Read as one scalar it stringified, so a document stating
    a value the contract knows produced no note AND an unknown word that is
    not a word."""
    rows = [_row("a.md", how_it_stands=["repealed", "abroge"])]
    assert st.currency_notes(rows, ROLES) == {"a.md": "repealed"}
    # Nothing was waved through, so nothing is reported as a mapping gap.
    assert st.unrecognised_statuses(rows, ROLES) == {}
    # A list with no known value is counted, every word of it.
    assert st.unrecognised_statuses(
        [_row("b.md", how_it_stands=["abroge", "caduc"])],
        ROLES) == {"abroge": 1, "caduc": 1}


def test_the_report_is_bounded_because_a_status_has_no_length_limit():
    """It is written into a run's telemetry. An extraction that went wrong
    could otherwise put a manifest's worth of text into a row nobody can
    read, so the count survives whole and the list is cut."""
    counts = {f"value-{i}" * 40: i + 1 for i in range(50)}
    rep = st.status_report(counts)
    assert rep["values"] == 50 and rep["documents"] == sum(counts.values())
    assert len(rep["top"]) == st.MAX_REPORTED_STATUSES and rep["truncated"]
    assert all(len(e["value"]) <= st.MAX_REPORTED_STATUS_LEN
               for e in rep["top"])
    # Busiest first, so a cut list keeps the values that matter.
    assert rep["top"][0]["documents"] >= rep["top"][-1]["documents"]
    assert st.status_report({})["truncated"] is False


def test_a_document_with_no_status_is_not_counted_as_an_unknown_one():
    """Silence is not a broken contract. Counting it would bury the values
    that are one under every document that simply has no status."""
    assert st.unrecognised_statuses([_row("a.md")], ROLES) == {}
    assert st.unrecognised_statuses(
        [_row("a.md", how_it_stands="repealed")], ROLES) == {}
    # A class that declares no status property says nothing about any of them.
    assert st.unrecognised_statuses(
        [_row("a.md", how_it_stands="abroge")], dr.NONE) == {}


def test_a_class_that_declares_no_status_gets_no_currency_note():
    assert st.currency_notes([_row("a.md", how_it_stands="repealed")],
                             dr.NONE) == {}


def test_a_later_decision_in_the_same_case_is_found_by_the_declared_date():
    """The second currency rule compares dates across the retrieved set, so
    it needs the same declaration the first one does."""
    rows = [{**_row("old.md", handed_down="2019-01-01"), "identifier": "X-1"},
            {**_row("new.md", handed_down="2021-05-04"), "identifier": "X-1"}]
    notes = st.currency_notes(rows, ROLES)
    assert "new.md" not in notes
    assert "a later decision in the same case (X-1)" in notes["old.md"]
    assert "new.md, 2021-05-04" in notes["old.md"]
    # Undeclared, the rule is off rather than wrong: nothing is dated.
    assert st.currency_notes(rows, dr.NONE) == {}


def test_the_latest_source_date_reads_each_row_by_its_own_class():
    rows = [_row("a.md", handed_down="2019-01-01"),
            {"filename": "b.md", "class": "Note", "props":
             {"written_on": "2024-03-01"}}]
    assert st.latest_source_date(rows, ROLES) == date(2024, 3, 1)
    assert st.latest_source_date(rows, dr.NONE) is None


def test_the_issuing_body_comes_from_the_declared_annotation():
    """It had a literal name and no documented fallback, so a deployment
    calling it anything else had the field silently empty. With nothing
    declared there is nothing to report — announced where it is resolved."""
    annotations = {"who_issued_it": {"name": "A Body"}}
    assert st.issuing_body_of(
        annotations, ROLES.issuing_body_annotation) == "A Body"
    assert st.issuing_body_of(annotations, None) is None


# ── resolving all of it, once, from the database ─────────────────────────────

class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return _Rows(self._rows)

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _Session:
    """Returns queued results in call order; an empty queue answers nothing."""

    def __init__(self, *results):
        self._queue = list(results)
        self.statements = []

    async def execute(self, stmt, *_a, **_k):
        self.statements.append(stmt)
        return _Rows(self._queue.pop(0) if self._queue else [])


@pytest.mark.asyncio
async def test_resolve_reads_every_role_once():
    """Seven small queries at the top of an answer, against indexed columns —
    not the same lookups inside a loop over documents, which is where writing
    a literal was the cheap thing to do."""
    session = _Session(
        [("Filing", "handed_down"), ("Note", "written_on")],   # date
        [("Filing", "where_it_binds")],    # jurisdiction
        [("Filing", "how_it_stands")],     # status
        [("Filing", "replaced_by")],       # superseded_by
        [("Filing", "neutral_id")],        # alternate_identifier
        ["how_high_it_stands"],            # the standing_tier annotation
        ["who_issued_it"],                 # the issuing_body annotation
    )
    roles = await dr.resolve(session)
    assert roles == ROLES
    assert len(session.statements) == 7


@pytest.mark.asyncio
async def test_an_annotation_declaring_the_tier_wins_over_the_walked_relation():
    """Two paths say the same thing — the annotation's own declaration, and
    the relation its path walks — so either will do and the explicit one is
    not asked twice."""
    session = _Session([], [], [], [], [], ["how_high_it_stands"])
    roles = await dr.resolve(session)
    assert roles.tier_annotations == ("how_high_it_stands",)
    # five property queries, the annotation role, the issuing body: the
    # relationship path was never walked.
    assert len(session.statements) == 7


@pytest.mark.asyncio
async def test_an_undeclared_role_switches_its_rule_off_and_says_so(caplog):
    """Off, and said out loud, once. A role nothing declares and a corpus
    holding nothing look identical in the output and want opposite fixes."""
    for role in (dr.DATE, dr.STATUS, dr.ISSUING_BODY):
        dr._announced.discard(role)
    with caplog.at_level("WARNING"):
        roles = await dr.resolve(_Session())
    assert roles == dr.NONE
    said = " ".join(r.getMessage() for r in caplog.records)
    assert dr.DATE in said and dr.STATUS in said
    assert "issuing body" in said


def test_nothing_declared_is_a_shape_a_caller_can_hold():
    """Pure code called without a resolved set gets every rule off rather
    than any of them guessed."""
    assert dr.NONE.date == {} and dr.NONE.tier_annotations == ()
    assert dr.NONE.issuing_body_annotation is None
    assert dr.NONE.value(dr.NONE.status, {"how_it_stands": "repealed"},
                         "Filing") is None
    assert ROLES.value(ROLES.status, {"how_it_stands": "repealed"},
                       "Filing") == "repealed"
