"""A class must declare the properties it points at.

The import reconciles a class's properties delete-then-add, so the manifest is
the whole set: a property absent from it is deleted. A class naming a property
it does not declare therefore points at nothing, and every reader that scopes
the property to the class stops seeing it.

That state cost 293 documents a week of invisibility to the naming checks with
nothing reporting it. It is cheap to refuse at import and it cannot be noticed
later, so it is refused here.

Run from the backend directory:
`python -m pytest tests/test_package_class_declarations.py`
"""

import pytest
from pydantic import ValidationError

from app.schemas.package import PackageDocumentClassEntry


def _entry(**kw):
    base = dict(name="Court Decision", properties=[{"name": "case_number"},
                                                   {"name": "title"}])
    return PackageDocumentClassEntry(**{**base, **kw})


def test_a_class_pointing_at_a_property_it_declares_is_accepted():
    e = _entry(identifier_property="case_number", identifier_pattern=r"([CT])-(\d+)",
               name_property="title")
    assert e.identifier_property == "case_number"
    assert e.name_property == "title"


def test_a_class_declaring_neither_is_accepted():
    """Both checks are opt-in and off by default."""
    assert _entry().identifier_property is None


def test_an_identifier_property_the_class_does_not_declare_is_refused():
    """The Advocate General Opinion class shipped in exactly this state."""
    with pytest.raises(ValidationError, match="identifier_property"):
        _entry(identifier_property="case_number", identifier_pattern=r"(\d+)",
               properties=[{"name": "title"}])


def test_a_name_property_the_class_does_not_declare_is_refused():
    with pytest.raises(ValidationError, match="name_property"):
        _entry(name_property="titel")


def test_a_class_with_no_properties_at_all_cannot_point_anywhere():
    """The shape the import leaves behind when a class is written with an
    empty property list: the rows are deleted and the pointer survives."""
    with pytest.raises(ValidationError):
        _entry(identifier_property="case_number", identifier_pattern=r"(\d+)",
               properties=[])


def test_an_identifier_property_without_a_pattern_is_still_refused():
    """The rule that landed with the pattern, pinned here because nothing
    pinned it."""
    with pytest.raises(ValidationError, match="identifier_pattern"):
        _entry(identifier_property="case_number")


def test_a_pattern_that_does_not_compile_is_still_refused():
    with pytest.raises(ValidationError, match="not a regex"):
        _entry(identifier_property="case_number", identifier_pattern="([unclosed")
