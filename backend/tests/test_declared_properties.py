"""A document that states a property is read, not asked about.

A property its class does not declare is asked of a model: ingestion puts the
whole document, header included, into a prompt and asks for the properties, so
a stored value can disagree with the document's own header. The fixtures
below have a header saying 1987-10-12 and a stored 1987-09-07, weeks out,
which is what a date taken from the body looks like.

A code is copied as it stands. A field the model has to decide about, such as
which of several dates in the body is the document's own, is where it goes
wrong, and that field decides which of two judgments is the later one.
Recency is the date and nothing else.

What is checked here is the precedence, because overwriting is a decision:

  no value            write it
  method `auto`       replace it, keeping the old value in `reason`
  method `manual`     leave it, and count it
  locked              leave it, and count it

and the `on_conflict` a class declares per property, so a deployment decides
which fields the header is the better source for. `replace` for a date the
document states outright; `fill_only` for a title, where the header is not
obviously the better source.
"""

from __future__ import annotations

import pytest

from app.services.declared_properties import (
    DECLARED_METHOD,
    Existing,
    plan_declared_values,
    replacement_reason,
)

MAPPING = [
    {"key": "decision_date", "property": "decision_date", "on_conflict": "replace"},
    {"key": "title", "property": "title", "on_conflict": "fill_only"},
    {"key": "language", "property": "language"},
]
HEADER = {"decision_date": "1987-10-12", "title": "A judgment",
          "language": "en", "seat": "somewhere"}


def _plan(existing=None):
    return plan_declared_values(HEADER, MAPPING, existing or {})


def test_a_property_with_no_value_is_written():
    w = {p.property: p for p in _plan().write}
    assert w["decision_date"].value == "1987-10-12"
    assert w["decision_date"].replaces is None


def test_a_declared_value_is_written_with_its_own_method_and_full_confidence():
    """Not `auto`. A value read from the document is not a value a model
    guessed, and the two must be told apart afterwards."""
    p = _plan().write[0]
    assert p.method == DECLARED_METHOD != "auto"
    assert p.confidence == 1.0


def test_a_key_the_class_does_not_declare_is_ignored():
    """`seat` is in the header and in no mapping. The platform does not decide
    what a header key means; the class does."""
    assert "seat" not in {p.property for p in _plan().write}


def test_a_mapped_key_absent_from_the_header_is_not_invented():
    plan = plan_declared_values({"language": "fr"}, MAPPING, {})
    assert {p.property for p in plan.write} == {"language"}


def test_an_auto_value_that_disagrees_is_replaced():
    """Nothing about an `auto` value is a human decision, so a header that
    disagrees with it wins."""
    plan = _plan({"decision_date": Existing(value="1987-09-07", method="auto",
                                            locked=False)})
    p = {x.property: x for x in plan.write}["decision_date"]
    assert p.value == "1987-10-12"
    assert p.replaces == "1987-09-07"


def test_a_manual_value_is_never_replaced():
    """A person deciding a value outranks a header."""
    plan = _plan({"decision_date": Existing(value="1987-09-07", method="manual",
                                            locked=False)})
    assert "decision_date" not in {p.property for p in plan.write}
    assert plan.kept_manual == ["decision_date"]


def test_a_locked_value_is_never_replaced():
    plan = _plan({"decision_date": Existing(value="1987-09-07", method="auto",
                                            locked=True)})
    assert "decision_date" not in {p.property for p in plan.write}
    assert plan.kept_locked == ["decision_date"]


def test_fill_only_writes_into_a_gap_and_never_over_a_value():
    """The title case. The header is not obviously the better source for a
    title, so the class says so and the platform obeys."""
    assert "title" in {p.property for p in _plan().write}
    plan = _plan({"title": Existing(value="Something else", method="auto",
                                    locked=False)})
    assert "title" not in {p.property for p in plan.write}
    assert plan.kept_existing == ["title"]


def test_an_agreeing_value_is_not_rewritten():
    """Rewriting a value to the same value would churn the row and lose the
    method that says where the current one came from."""
    plan = _plan({"decision_date": Existing(value="1987-10-12", method="auto",
                                            locked=False)})
    assert "decision_date" not in {p.property for p in plan.write}
    assert plan.unchanged == ["decision_date"]


def test_a_value_this_path_already_wrote_is_left_alone():
    plan = _plan({"decision_date": Existing(value="1987-10-12",
                                            method=DECLARED_METHOD, locked=False)})
    assert "decision_date" not in {p.property for p in plan.write}


def test_the_reason_follows_the_backfill_convention():
    """`backfill <date> <operation>; prior value: {"_": "<old>"}`, so a
    reader who finds a replaced value knows where the old one is."""
    r = replacement_reason("2026-09-10", "1987-09-07")
    assert r.startswith("backfill 2026-09-10 declared_properties; prior value:")
    assert '{"_": "1987-09-07"}' in r


def test_a_first_write_says_where_the_value_came_from_without_a_prior():
    r = replacement_reason("2026-09-10", None)
    assert "prior value" not in r
    assert "declared_properties" in r


def test_an_unknown_on_conflict_is_refused_rather_than_guessed():
    """A typo in the deployment's file must not silently become `fill_only`
    and quietly stop replacing the field a must-have depends on."""
    with pytest.raises(ValueError, match="on_conflict"):
        plan_declared_values(HEADER, [{"key": "decision_date",
                                       "property": "decision_date",
                                       "on_conflict": "overwrite"}], {})


# -- reading what is already stored ------------------------------------------
#
# Values are written as {"_": x}. The reader used to be
# `str((value or {}).get("_", ""))`, which raised on any value that is not a
# dict, and the per-document isolation around ingestion turned that into a
# failed document with nothing to say why.

from app.services.declared_properties import stored_text  # noqa: E402


def _old_reader(value):
    return str((value or {}).get("_", ""))


@pytest.mark.parametrize("value", [
    {"_": "2019-01-01"}, {"_": 1987}, {"_": ["a", "b"]}, {"_": None}, {},
])
def test_every_stored_shape_reads_exactly_as_it_did(value):
    """Every row ingestion writes is a dict. Changing how one reads
    would change what the header replaces, so the dict path is pinned to the
    old reader, quirks included."""
    assert stored_text(value) == _old_reader(value)


def test_a_bare_value_no_longer_fails_the_document():
    with pytest.raises(AttributeError):
        _old_reader("2019-01-01")
    assert stored_text("2019-01-01") == "2019-01-01"
    assert stored_text(1987) == "1987"
    assert stored_text(None) == ""


def test_a_bare_list_is_not_one_value():
    """Not compared, so never overwritten by a single header value."""
    assert stored_text(["a", "b"]) is None


# -- the path end to end, against a stub session ------------------------------

import asyncio  # noqa: E402
import uuid  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.services import ingestion_oneshot as io  # noqa: E402

HANDED, NOTE = uuid.uuid4(), uuid.uuid4()
CLASS_PROPS = [{"name": "handed_down", "id": HANDED}, {"name": "note", "id": NOTE}]
CONTENT = "---\ndate: 2020-05-01\nnote: from the header\nx: y\n---\nBody.\n"


class _Session:
    """First execute: the class's mapping. Second: the document's rows."""

    def __init__(self, mapping, rows):
        self._results = [mapping, rows]
        self.added = []

    async def execute(self, _stmt):
        payload = self._results.pop(0)
        return SimpleNamespace(
            scalar_one_or_none=lambda: payload,
            scalars=lambda: SimpleNamespace(all=lambda: payload))

    def add(self, obj):
        self.added.append(obj)


def _row(pid, value):
    return SimpleNamespace(property_id=pid, value=value, method="auto",
                           locked=False, confidence=0.5,
                           document_version_id=None, reason=None)


def _run(mapping, rows):
    session = _Session(mapping, rows)
    report = asyncio.run(io._write_declared_properties(
        session, document_id=uuid.uuid4(), version_id=uuid.uuid4(),
        class_id=uuid.uuid4(), content=CONTENT, class_props=CLASS_PROPS,
        write=True))
    return report, session


REPLACE = [{"key": "date", "property": "handed_down", "on_conflict": "replace"},
           {"key": "note", "property": "note", "on_conflict": "replace"}]


def test_a_bare_stored_value_is_compared_and_replaced_like_any_other():
    handed = _row(HANDED, "2019-01-01")
    report, _ = _run(REPLACE, [handed])
    assert handed.value == {"_": "2020-05-01"}
    assert report.get("replaced") == 1


def test_a_stored_list_is_left_alone_and_reported():
    """Leaving it out of `existing` alone would read as nothing stored, and
    the row would be overwritten by the header's single value."""
    note = _row(NOTE, ["a", "b"])
    report, session = _run(REPLACE, [note])
    assert note.value == ["a", "b"]
    assert report["left_unreadable"] == ["note"]
    assert not any(getattr(a, "property_id", None) == NOTE for a in session.added)


def test_a_target_the_class_no_longer_has_is_recorded():
    """The schema refuses this at import. It can still arise if a class loses
    a property afterwards, and then it must say so."""
    mapping = [{"key": "x", "property": "gone", "on_conflict": "replace"}]
    report, _ = _run(mapping, [])
    assert report["unknown_targets"] == ["gone"]


# -- the declaration is checked where it is written ---------------------------

def _class(**kw):
    from app.schemas.package import PackageDocumentClassEntry
    base = {"name": "A Class", "properties": [{"name": "handed_down"}]}
    return PackageDocumentClassEntry(**{**base, **kw})


def test_a_header_value_may_map_to_a_property_the_class_declares():
    c = _class(declared_properties=[{"key": "date", "property": "handed_down"}])
    assert c.declared_properties[0].property == "handed_down"


def test_a_header_value_may_not_map_to_a_property_the_class_does_not_declare():
    with pytest.raises(Exception) as err:
        _class(declared_properties=[{"key": "date", "property": "handed_dwon"}])
    assert "does not declare" in str(err.value)


# -- review findings on #192 ---------------------------------------------------

from app.services.declared_properties import read_held, unknown_targets  # noqa: E402


def test_an_unknown_target_is_seen_from_the_mapping_not_the_plan():
    """A plan names a property only when the document states that key, so a
    stale declaration on a document without it was never reported."""
    mapping = [{"key": "absent", "property": "gone"}]
    assert unknown_targets(mapping, {"handed_down": 1}) == ["gone"]
    report, _ = _run(mapping, [])
    assert report["unknown_targets"] == ["gone"]


def test_an_unknown_target_is_reported_even_without_front_matter():
    session = _Session([{"key": "x", "property": "gone"}], [])
    report = asyncio.run(io._write_declared_properties(
        session, document_id=uuid.uuid4(), version_id=uuid.uuid4(),
        class_id=uuid.uuid4(), content="No header here.", class_props=CLASS_PROPS,
        write=True))
    assert report["no_front_matter"] is True
    assert report["unknown_targets"] == ["gone"]


def test_a_list_on_a_property_the_mapping_does_not_name_is_not_reported():
    """A list on an unrelated property is a legitimate value. Reporting it
    sends someone to investigate a row this path never meant to touch."""
    only_date = [{"key": "date", "property": "handed_down", "on_conflict": "replace"}]
    note = _row(NOTE, ["a", "b"])
    report, _ = _run(only_date, [_row(HANDED, {"_": "2019-01-01"}), note])
    assert "left_unreadable" not in report
    assert note.value == ["a", "b"]
    held = read_held(only_date, {"handed_down": HANDED, "note": NOTE},
                     {HANDED: _row(HANDED, {"_": "x"}), NOTE: note})
    assert held.unreadable == [] and "note" not in held.existing


# -- the backfill script, which carried a copy of the reader -------------------

import importlib.util  # noqa: E402
import pathlib  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402


def _load_script():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "apply_declared_properties.py"
    spec = importlib.util.spec_from_file_location("apply_declared_properties", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ScriptSession:
    """Answers the script's queries in the order it makes them."""

    def __init__(self, cls, props, docs, rows):
        self._queue = [("first", cls), ("all", props), ("rows", docs), ("all", rows)]
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        kind, payload = self._queue.pop(0)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: payload, all=lambda: payload),
            all=lambda: payload)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def test_the_backfill_no_longer_aborts_on_the_values_ingestion_now_handles(monkeypatch):
    """The finding: the script still did `.get()` on every stored value, so a
    run over the same bare values would raise and stop."""
    mod = _load_script()
    mapping = REPLACE + [{"key": "x", "property": "gone"}]
    cls = SimpleNamespace(id=uuid.uuid4(), declared_properties=mapping)
    props = [SimpleNamespace(name="handed_down", id=HANDED),
             SimpleNamespace(name="note", id=NOTE)]
    handed, note = _row(HANDED, "2019-01-01"), _row(NOTE, ["a", "b"])
    session = _ScriptSession(cls, props, [(uuid.uuid4(), CONTENT, uuid.uuid4())],
                             [handed, note])

    @asynccontextmanager
    async def _local():
        yield session

    monkeypatch.setattr(mod, "AsyncSessionLocal", _local)
    totals = asyncio.run(mod.run("A Class", apply=True, limit=None))
    assert handed.value == {"_": "2020-05-01"}, "a bare scalar is compared and replaced"
    assert note.value == ["a", "b"], "a list is left alone"
    assert totals["left_unreadable"] == 1
    assert totals["unknown_targets"] == ["gone"]
    assert session.committed
