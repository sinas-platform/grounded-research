"""What the source already knows is written, not guessed.

The bulk route takes a class name so an operator's knowledge of a source
sets every document's class at confidence 1.0, and front matter whose keys
match the class's properties seeds locked manual values — the exporter's
exact strings, which the citation resolver later matches on.

Pure where possible; the wiring is pinned by reading the source, the
suite's pattern for DB-bound paths.
"""

import inspect
import types
import uuid

from app.api.v1 import bulk
from app.services import document_registry
from app.services.front_matter import front_matter_property_values


def _prop(name, cardinality="one"):
    return types.SimpleNamespace(id=uuid.uuid4(), name=name, cardinality=cardinality)


# -- the pure seeding rules ----------------------------------------------------


def test_a_matching_key_becomes_the_exact_declared_value():
    p = _prop("Case Number")
    out = front_matter_property_values({"case-number": "AT.40099"}, [p])
    assert out == [(p.id, {"_": "AT.40099"})]


def test_matching_folds_case_and_separators():
    p = _prop("decision_date")
    out = front_matter_property_values({"Decision-Date": "2021-01-12"}, [p])
    assert out == [(p.id, {"_": "2021-01-12"})]


def test_an_unknown_key_is_ignored():
    assert front_matter_property_values({"content_hash": "abc"}, [_prop("title")]) == []


def test_a_list_fits_a_many_property_whole():
    p = _prop("keywords", cardinality="many")
    out = front_matter_property_values({"keywords": ["a", "b"]}, [p])
    assert out == [(p.id, {"_": ["a", "b"]})]


def test_a_list_into_a_single_property_takes_the_first():
    p = _prop("language")
    out = front_matter_property_values({"language": ["en", "fr"]}, [p])
    assert out == [(p.id, {"_": "en"})]


def test_dicts_are_entity_candidates_not_values():
    p = _prop("authors")
    fm = {"authors": [{"id": 1, "name": "X"}]}
    assert front_matter_property_values(fm, [p]) == []


def test_dates_stay_the_iso_strings_the_exporter_wrote():
    p = _prop("date")
    out = front_matter_property_values({"date": "2021-06-30"}, [p])
    assert out[0][1] == {"_": "2021-06-30"}  # a string, unparsed


# -- the wiring, pinned --------------------------------------------------------

BULK = inspect.getsource(bulk)
REG = inspect.getsource(document_registry)


def test_an_unknown_class_fails_the_whole_upload():
    """An operator's typo must fail loudly before anything is registered,
    not classify half a corpus as None."""
    assert "unknown document class" in BULK
    assert BULK.index("unknown document class") < BULK.index("zf.namelist()")


def test_the_class_reaches_every_registration():
    assert "document_class_id=document_class_id" in BULK


def test_seeded_values_are_locked_manual_at_full_confidence():
    """`_wipe_extracted_artifacts` deletes only auto+unlocked, so exactly
    these flags are what makes a seeded value survive re-extraction."""
    assert 'method="manual", locked=True, confidence=1.0' in REG
    assert 'reason="front-matter declared"' in REG


def test_an_existing_value_is_left_alone():
    assert "if prop_id in existing:" in REG


def test_seeding_needs_a_known_class():
    """Properties belong to a class; without one there is nothing to match
    against, and the function says so by returning before reading."""
    assert "if doc.document_class_id is None:" in REG
