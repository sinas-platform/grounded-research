"""A deployment declares which of its relationships the engine may act on.

Two features found an edge by the name a deployment happened to give it: the
superseded-source check matched a literal list of names, and the tier was read
off an annotation found by its name. Both are silent when the guess is wrong.
A deployment that called its relations anything else got no findings — which
downstream is byte-identical to a collection that records no supersession —
and no tier, which is byte-identical to a source with no issuing body. Neither
failure has a symptom.

What replaces the guess is one block in the package, `spec.relationship_roles`,
in which the deployment says which of ITS relations carry which engine-
meaningful role. What is checked here is that the block is read where it is
written, that a role naming nothing is refused rather than ignored, and that
an undeclared role leaves the engine doing what it did before rather than
something new.

Run from the backend directory:
`python -m pytest tests/test_a_relationship_says_what_it_means.py`
"""

from __future__ import annotations

import uuid

import pytest
from app.schemas.package import ENGINE_ROLES, PackageRelationshipRoles
from app.services import package as package_service
from app.services import relationship_roles as roles
from app.services.answer_structure import TIER_ANNOTATION, tier_of
from pydantic import ValidationError

# ── the block, as a package writes it ────────────────────────────────────────

_PACKAGE = """
apiVersion: sgr.sinas.co/v1
kind: SgrPackage
metadata:
  name: demo
package:
  name: demo
  version: "0.1.0"
spec:
  entity_types:
    - name: Ruling
    - name: Body
  document_classes:
    - name: Filing
  relationship_definitions:
    - name: replaced_by_a_later_one
      source: {{ type: entity_type, name: Ruling }}
      target: {{ type: entity_type, name: Ruling }}
    - name: outranks
      source: {{ type: entity_type, name: Body }}
      target: {{ type: entity_type, name: Body }}
    - name: records_the_whole_of
      source: {{ type: document_class, name: Filing }}
      target: {{ type: entity_type, name: Ruling }}
      cardinality: one
{roles}
"""

_DECLARED = """  relationship_roles:
    supersession: [replaced_by_a_later_one]
    standing: [outranks]
    full_text: [records_the_whole_of]
"""


def _validate(roles_block: str = ""):
    return package_service.validate(_PACKAGE.format(roles=roles_block))


def test_a_package_declaring_its_roles_validates():
    """The names are the deployment's own and mean nothing to the engine; the
    ROLES are the engine's and mean the same in every deployment."""
    result = _validate(_DECLARED)
    assert result.valid, result.errors


def test_a_package_that_declares_nothing_still_validates():
    """The block is additive against a schema that forbids extras, and
    deployment manifests live in client repos: a package written before this
    existed must keep installing."""
    assert _validate().valid


def test_a_role_naming_a_relationship_the_package_does_not_define_is_refused():
    """The failure this block exists to remove is a name that matches
    nothing, so a name that matches nothing is not allowed through it."""
    result = _validate(
        "  relationship_roles:\n    supersession: [no_such_relationship]\n")
    assert not result.valid
    assert any("no_such_relationship" in e for e in result.errors), result.errors


def test_a_role_the_engine_does_not_read_is_refused():
    """A role nothing acts on would be a declaration that does nothing, which
    is the shape of the defect, not a fix for it."""
    result = _validate("  relationship_roles:\n    vibes: [outranks]\n")
    assert not result.valid


def test_one_definition_does_not_carry_two_roles():
    with pytest.raises(ValidationError):
        PackageRelationshipRoles(standing=["outranks"], supersession=["outranks"])


def test_a_role_takes_several_names_because_a_meaning_can_need_several():
    """A definition's name is its unique key, so one meaning between two pairs
    of types is two definitions — and both carry the role."""
    block = PackageRelationshipRoles(full_text=["records_x", "records_y"])
    assert block.names_for("full_text") == ["records_x", "records_y"]
    assert block.names_for("standing") == []


def test_the_engine_reads_exactly_the_roles_it_declares():
    """ENGINE_ROLES is the contract both sides hold: the schema offers these
    fields and the readers ask for these names."""
    assert set(ENGINE_ROLES) == {roles.STANDING, roles.SUPERSESSION,
                                 roles.FULL_TEXT}
    assert set(PackageRelationshipRoles.model_fields) == set(ENGINE_ROLES)


# ── what the readers do with it ──────────────────────────────────────────────

class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows

    def all(self):
        return self._rows


class _Session:
    """Returns queued results in call order and keeps the statements."""

    def __init__(self, *results):
        self._queue = list(results)
        self.statements = []

    async def execute(self, stmt, *_a, **_k):
        self.statements.append(stmt)
        return _Rows(self._queue.pop(0) if self._queue else [])


@pytest.mark.asyncio
async def test_a_role_the_engine_does_not_know_is_a_programming_error():
    with pytest.raises(ValueError):
        await roles.definition_ids(_Session(), "vibes")


@pytest.mark.asyncio
async def test_an_undeclared_supersession_role_turns_the_check_off(caplog):
    """Off and said out loud. Returning nothing quietly is the exact failure:
    no findings is what a clean answer returns too."""
    with caplog.at_level("WARNING", logger="sgr.relationship_roles"):
        roles._announced.discard(roles.SUPERSESSION)
        assert await roles.supersession_definition_ids(_Session()) == []
    said = " ".join(r.getMessage() for r in caplog.records)
    assert roles.SUPERSESSION in said
    assert "superseded" in said


@pytest.mark.asyncio
async def test_a_declared_supersession_role_is_what_the_check_reads():
    declared = [uuid.uuid4()]
    assert await roles.supersession_definition_ids(_Session(declared)) == declared


@pytest.mark.asyncio
async def test_an_undeclared_full_text_role_falls_back_to_identity_edges():
    """The fallback is structural and names nothing: a document→entity
    definition with cardinality "one" already says a document is the full text
    of one entity, which is the contract the annotation walker reads. So a
    package with no declaration keeps exactly what it had."""
    fallback = [uuid.uuid4(), uuid.uuid4()]
    session = _Session([], fallback)
    assert await roles.full_text_definition_ids(session) == fallback
    clause = str(session.statements[-1].whereclause)
    assert "source_ref_type" in clause and "cardinality" in clause


@pytest.mark.asyncio
async def test_a_declared_full_text_role_wins_over_the_fallback():
    declared = [uuid.uuid4()]
    session = _Session(declared)
    assert await roles.full_text_definition_ids(session) == declared
    assert len(session.statements) == 1  # the fallback was never asked


class _Def:
    def __init__(self, name, path):
        self.name, self.path = name, path


@pytest.mark.asyncio
async def test_the_tier_annotation_is_the_one_that_walks_a_standing_relation():
    """Not the one with a particular name. An annotation is engine-meaningful
    through the edges it walks."""
    session = _Session(
        [uuid.uuid4()],                       # definitions carrying the role
        ["outranks"],                         # their names
        [_Def("how_it_stands", "issued/(^outranks)*"),
         _Def("where_from", "issued")],       # every annotation
    )
    assert await roles.annotation_names(session, roles.STANDING) == [
        "how_it_stands"]


@pytest.mark.asyncio
async def test_an_annotation_whose_path_is_unreadable_is_skipped_not_fatal():
    session = _Session(
        [uuid.uuid4()], ["outranks"],
        [_Def("broken", "((("), _Def("fine", "^outranks")],
    )
    assert await roles.annotation_names(session, roles.STANDING) == ["fine"]


@pytest.mark.asyncio
async def test_no_standing_relation_means_no_tier_and_a_reason(caplog):
    with caplog.at_level("WARNING", logger="sgr.relationship_roles"):
        roles._announced.discard(roles.STANDING)
        assert await roles.standing_annotation_names(_Session()) == []
    said = " ".join(r.getMessage() for r in caplog.records)
    assert roles.STANDING in said and "tier" in said


# ── the readers themselves ───────────────────────────────────────────────────

def test_the_tier_is_read_from_the_annotations_the_caller_resolved():
    """`{"depth": n}` from a length reducer, or a bare number. The name is the
    caller's to supply, and it is whatever the deployment called it."""
    values = {"how_it_stands": {"depth": 2}, TIER_ANNOTATION: {"depth": 9}}
    assert tier_of(values, ["how_it_stands"]) == 2
    assert tier_of(values, ["nothing_here", "how_it_stands"]) == 2
    assert tier_of(values, []) is None


def test_a_caller_that_resolved_nothing_falls_back_to_the_contract_name():
    """Documented, not hidden: a deployment that has declared no role keeps
    the tier it has today rather than losing it to a rename."""
    assert tier_of({TIER_ANNOTATION: {"depth": 3}}) == 3
    assert tier_of({TIER_ANNOTATION: 4}) == 4
    assert tier_of({"how_it_stands": {"depth": 3}}) is None


def test_the_superseded_source_query_names_no_relationship():
    """Both edges are selected by definition id, passed in by the caller from
    the declared roles. A name in this SQL is the defect returning."""
    from app.services.supersession import _CITED_SUPERSEDED

    sql = str(_CITED_SUPERSEDED)
    assert "rd.name" not in sql
    assert ":supersession_defs" in sql and ":full_text_defs" in sql
