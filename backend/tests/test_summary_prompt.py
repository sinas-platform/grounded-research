"""What the summariser is told about how to summarise.

`summarization_guidance` is a column on document_class, a field on the package
schema, and is carried through import and export. Nothing read it. The only
summary instruction the model ever received was a line fixed in the platform:

    "summary": "<8-12 sentence factual summary: parties, authority, dates,
                outcome, legal basis>"

Two defects in one line. Every instruction a deployment wrote was stored and
never sent, and the order that WAS sent put parties and authority first, which
is the opening every description in this corpus spends its first 200
characters on. It is also domain vocabulary in a platform that must not carry
any.

Run from the backend directory:
`python -m pytest tests/test_summary_prompt.py`
"""

from app.services import ingestion_oneshot as io

GUIDANCE = "Lead with the substance. Say what the chapter establishes."


def _prompt(**kw):
    base = dict(filename="a.md", content="body", classes=[("Court Decision", "d")],
                entity_types=[{"name": "Company", "guidance": "firms"}],
                known_entities=[], class_hint=None, properties=None)
    base.update(kw)
    return io._front_matter_prompt(**base)


def test_the_class_instruction_reaches_the_model():
    assert GUIDANCE in _prompt(summary_guidance=GUIDANCE)


def test_a_class_with_no_instruction_still_asks_for_a_summary():
    """Most classes declare none, and they must keep working."""
    assert "summary" in _prompt(summary_guidance=None)


def test_the_platform_states_no_order_of_its_own():
    """The order is the deployment's to choose. `parties, authority, dates,
    outcome, legal basis` is competition-law vocabulary, and a corpus of
    clinical trials has none of those things."""
    p = _prompt(summary_guidance=None)
    for word in ("parties", "authority", "legal basis", "outcome"):
        assert word not in p.lower(), word


def test_the_instruction_does_not_displace_the_reply_schema():
    p = _prompt(summary_guidance=GUIDANCE)
    assert '"summary"' in p and '"document_class"' in p and '"entities"' in p


def test_an_empty_instruction_is_treated_as_none():
    """A class that declares the field and leaves it blank must not send an
    empty instruction block."""
    assert _prompt(summary_guidance="   ") == _prompt(summary_guidance=None)


# ── whether an existing summary may be replaced ──────────────────────────────


def test_a_document_with_no_summary_is_summarised():
    assert io._should_write_summary(existing=None, incoming="s", resummarise=False)


def test_an_existing_summary_is_kept_by_default():
    """Re-running ingestion must not silently rewrite descriptions someone
    may have read or corrected."""
    assert not io._should_write_summary(
        existing="already there", incoming="new", resummarise=False)


def test_resummarise_replaces_it():
    """Without an explicit way in, a description written under the wrong
    instructions could never be corrected: the guard that protects it also
    seals it."""
    assert io._should_write_summary(
        existing="already there", incoming="new", resummarise=True)


def test_nothing_is_written_when_the_model_returned_nothing():
    assert not io._should_write_summary(existing=None, incoming="", resummarise=True)
    assert not io._should_write_summary(existing="a", incoming=None, resummarise=True)


def test_a_blank_existing_summary_counts_as_none():
    assert io._should_write_summary(existing="   ", incoming="s", resummarise=False)
