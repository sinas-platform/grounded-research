"""The property-value storage shape contract.

Three parties must agree on how PropertyValue.value wraps a scalar: the
one-shot writer, the API unwrapper (schemas/runtime.PropertyValueOut) and
the SQL filter layer (introspect, which filters on value['_']). The
one-shot originally wrote {"value": x}; both readers silently missed
every property it extracted (13,829 rows in the first at-scale run).
These tests lock the contract so the shapes can never drift apart again.

Run from the backend directory:
`python -m pytest tests/test_property_value_shape.py`
"""

from app.schemas.runtime import PropertyValueOut
from app.services.ingestion_oneshot import wrap_property_value
from app.services.introspect import _unwrap_property_value


def test_oneshot_writes_the_shape_the_api_unwrapper_reads():
    for raw in ["Anticompetitive practices", 42, 3.14, True, ["a", "b"]]:
        wrapped = wrap_property_value(raw)
        assert PropertyValueOut._unwrap_value(wrapped) == raw


def test_oneshot_writes_the_shape_the_sql_filter_layer_reads():
    for raw in ["Mergers", 7]:
        wrapped = wrap_property_value(raw)
        assert _unwrap_property_value(wrapped) == raw
        # the field-filter clauses index value['_'] directly
        assert set(wrapped.keys()) == {"_"}


def test_the_old_wrong_shape_is_not_what_the_readers_unwrap():
    wrong = {"value": "Mergers"}
    assert PropertyValueOut._unwrap_value(wrong) == wrong  # passes through, unusable
    assert _unwrap_property_value(wrong) == wrong
