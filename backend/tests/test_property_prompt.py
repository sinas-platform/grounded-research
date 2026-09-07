"""What the extractor is told about a class property.

The prompt interpolated `description`, the schema author fills `guidance`, and
nothing reconciled them — so every property instruction in this deployment was
written, stored, and never sent. Entity types have always read
`guidance or description`; properties never did.

Cardinality was never sent either, so a property declared `many` did not tell
the model it may return more than one while the JSON schema it did send said
`"type": "string"`.

Run from the backend directory:
`python -m pytest tests/test_property_prompt.py`
"""

from types import SimpleNamespace

from app.services import ingestion_oneshot as io


def prop(**kw):
    base = dict(name="case_number", description=None, guidance=None,
                schema={"type": "string"}, cardinality="one", id="p1")
    base.update(kw)
    return SimpleNamespace(**base)


def test_guidance_is_used_when_description_is_absent():
    assert _prop(prop(guidance="EU courts T-XXX/YY."))["description"] == "EU courts T-XXX/YY."


def test_guidance_wins_when_both_are_present():
    """`guidance` is the extraction-specific instruction; `description` is the
    generic one. Nothing here has a description, so getting this backwards
    would have been latent rather than live, and missed later."""
    got = _prop(prop(description="the generic one", guidance="the extraction one"))
    assert got["description"] == "the extraction one"


def test_neither_leaves_it_empty():
    assert _prop(prop())["description"] == ""


def _prop(p):
    io._SILENT_PROPS.clear()
    return io._prop_for_prompt(p)


def test_a_property_with_no_instruction_warns(caplog):
    io._SILENT_PROPS.clear()
    with caplog.at_level("WARNING"):
        io._prop_for_prompt(prop(name="outcome"))
    assert "neither description nor guidance" in caplog.text
    assert "outcome" in caplog.text


def test_the_warning_fires_once_per_property_not_once_per_document(caplog):
    io._SILENT_PROPS.clear()
    with caplog.at_level("WARNING"):
        for _ in range(5):
            io._prop_for_prompt(prop(name="outcome"))
    assert caplog.text.count("neither description nor guidance") == 1


def test_a_property_with_guidance_does_not_warn(caplog):
    io._SILENT_PROPS.clear()
    with caplog.at_level("WARNING"):
        io._prop_for_prompt(prop(guidance="something"))
    assert "neither description nor guidance" not in caplog.text


def test_cardinality_is_carried_through():
    assert _prop(prop(cardinality="many"))["cardinality"] == "many"


def test_a_many_property_is_told_it_may_return_several():
    line = io._prompt_property_lines([_prop(prop(name="outcome", cardinality="many"))])
    assert "cardinality: many" in line
    assert "JSON array" in line


def test_a_one_property_is_not():
    line = io._prompt_property_lines([_prop(prop(cardinality="one"))])
    assert "cardinality" not in line


def test_the_bulk_pipeline_builds_its_properties_the_same_way():
    """Bulk ingestion is the path most documents arrive by. It used to build its
    own property dict without guidance or cardinality, so a prompt fix landing
    only in the one-shot never reached them."""
    import inspect
    from app import bulk_pipeline

    src = inspect.getsource(bulk_pipeline.stage_extract)
    assert "_prop_for_prompt(p) for p in rows" in src
    assert '"description": p.description' not in src
