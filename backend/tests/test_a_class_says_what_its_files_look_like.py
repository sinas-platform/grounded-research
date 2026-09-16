"""A deployment declares which filenames mean which document class.

The free rung of the classification ladder was a module constant in
`ingestion_oneshot`: five regular expressions mapped to four document-class
names one deployment chose — a register scheme, an inquiry slug, a feed's
numeric ids. It is the right first rung, because it costs nothing and
answers instantly, but WHICH filenames mean what is knowledge about a
collection. Every other deployment matched none of those patterns, paid for
a model call on every document it ingested, and had nothing anywhere saying
why the free rung never fired.

So the class declares its own: `filename_rules` on the document class,
written from the package, read once per run and passed down. What is checked
here is that the declaration survives the round trip, that a rule that cannot
work is refused where it is written rather than raised per document, and that
a deployment declaring nothing is left exactly where it was — the ladder's
first rung simply never fires, which is what it did for every deployment but
one.

Run from the backend directory:
`python -m pytest tests/test_a_class_says_what_its_files_look_like.py`
"""

from __future__ import annotations

import asyncio

import pytest
from app.schemas.package import PackageFilenameRule
from app.services import ingestion_oneshot as one
from app.services import package as package_service
from pydantic import ValidationError

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
{rules}
"""

_DECLARED = """      filename_rules:
        - pattern: '^ref-\\\\d{4}'
          confidence: 0.98
          reason: the register's own reference scheme
        - pattern: '\\\\.summary\\\\.md$'
          confidence: 0.6
          reason: a summary sidecar, which the model should confirm
"""


def _validate(rules_block: str = ""):
    return package_service.validate(_PACKAGE.format(rules=rules_block))


# ── the declaration ──────────────────────────────────────────────────────────

def test_a_class_may_declare_the_filenames_that_mean_it():
    result = _validate(_DECLARED)
    assert result.valid, result.errors


def test_a_class_that_declares_nothing_still_validates():
    """Additive against a schema that forbids extras, and deployment
    manifests live in client repos: a package written before this existed
    must keep installing."""
    assert _validate().valid


def test_a_pattern_that_cannot_compile_is_refused_where_it_is_written():
    """Otherwise it raises inside the ingestion path, per document, on the
    rung that is meant to be free."""
    result = _validate(
        "      filename_rules:\n"
        "        - pattern: '^ref-(unclosed'\n"
        "          confidence: 0.9\n"
        "          reason: broken\n")
    assert not result.valid
    assert any("compile" in e for e in result.errors), result.errors


def test_a_confidence_outside_the_scale_is_refused():
    """The number is compared against the engine's write threshold. A value
    off the scale would either assign every matching class outright or never
    assign one, and both read as the rule working."""
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            PackageFilenameRule(pattern="^x", confidence=bad, reason="r")


def test_a_rule_says_what_it_is_for():
    """`reason` is stored on the document and is the only record of why a
    class was assigned without a model ever reading the file."""
    with pytest.raises(ValidationError):
        PackageFilenameRule(pattern="^x", confidence=0.9)


# ── what the engine does with it ─────────────────────────────────────────────

class _Class:
    def __init__(self, name, rules):
        self.name = name
        self.filename_rules = rules


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _Scalars(self._rows)


def _rules(rows):
    return asyncio.run(one.load_class_rules(_Session(rows)))


def test_the_rules_come_from_the_classes_and_nowhere_else():
    """There is no longer a list in this module to fall back on. A
    deployment that declares nothing gets no rule hits — the free rung never
    fires and classification goes to the model, which is what happened to
    every deployment but one before this existed."""
    assert not hasattr(one, "CLASS_RULES")
    assert _rules([_Class("Filing", None), _Class("Note", [])]) == []
    assert one.classify_by_rules("anything.md", []) is None


def test_a_declared_rule_names_its_class_its_confidence_and_its_reason():
    rules = _rules([_Class("Filing", [
        {"pattern": r"^ref-\d{4}", "confidence": 0.98, "reason": "register"}])])
    assert one.classify_by_rules("ref-2019-a.md", rules) == (
        "Filing", 0.98, "register")
    assert one.classify_by_rules("something-else.md", rules) is None


def test_the_surer_rule_wins_when_two_classes_claim_a_filename():
    """Two classes may both match, and which one wins must not depend on the
    order the database returns rows in — that is a classification that
    changes between runs with nothing to show for it."""
    rows = [_Class("Loud", [{"pattern": r"^ref-", "confidence": 0.6,
                             "reason": "loose"}]),
            _Class("Sure", [{"pattern": r"^ref-\d{4}", "confidence": 0.98,
                             "reason": "exact"}])]
    forward = _rules(rows)
    backward = _rules(list(reversed(rows)))
    assert forward == backward
    assert one.classify_by_rules("ref-2019.md", forward)[0] == "Sure"


def test_a_stored_rule_that_cannot_compile_is_dropped_not_raised():
    """The package schema refuses these, so one here predates the check or
    was written around it. Per-document ingestion is not the place to find
    out."""
    rules = _rules([_Class("Filing", [
        {"pattern": "^ref-(unclosed", "confidence": 0.9, "reason": "broken"},
        {"pattern": "^ok-", "confidence": 0.9, "reason": "fine"}])])
    assert [r[1] for r in rules] == ["^ok-"]


def test_the_write_threshold_still_decides_hint_from_assignment():
    """The ladder is unchanged — only where its rungs are written moved. A
    rule at or above the threshold assigns the class; one below it is a hint
    the model is asked to confirm."""
    rules = _rules([_Class("Filing", [
        {"pattern": "^sure-", "confidence": 0.98, "reason": "certain"},
        {"pattern": "^maybe-", "confidence": 0.6, "reason": "a guess"}])])
    sure = one.classify_by_rules("sure-1.md", rules)
    maybe = one.classify_by_rules("maybe-1.md", rules)
    assert sure[1] >= one.RULE_WRITE_CONFIDENCE
    assert maybe[1] < one.RULE_WRITE_CONFIDENCE


# ── and which property holds a document's name ───────────────────────────────

def test_the_title_subquery_reads_the_class_s_declared_name_property():
    """It matched the literal property name `title`, so a deployment whose
    classes call it `heading`, `subject` or `titre` served the storage
    filename on every reader surface — the API document list, the results
    list, the rendered answer's source lines — with nothing saying why. The
    class already declares `name_property` for exactly this."""
    from sqlalchemy import select

    from app.models import Document
    from app.services.document_identity import document_title_subquery

    sql = str(select(Document.id, document_title_subquery()))
    assert "document_class_property.name = document_class.name_property" in sql
    assert '"title"' not in sql and "'title'" not in sql
    # and it is the document's OWN class, not any class with a property of
    # that name — two classes may both declare one and they are different
    # properties.
    assert "document_class.id = document.document_class_id" in sql
    # still correlated on the document, or every row gets the same title
    assert "property_value.document_id = document.id" in sql
