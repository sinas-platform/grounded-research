"""Front-matter prompt: fixed-class rendering (CNAI-1167).

A document whose class is already assigned (source-declared, rule-written
or previously classified) must not be asked to pick a class — the prompt
states the class and the pick-one list disappears.

Run from the backend directory:
`python -m pytest tests/test_oneshot_prompt.py`
"""

from app.services.ingestion_oneshot import _front_matter_prompt

_CLASSES = [("Regulatory Decision", "authority decision"), ("Bulletin article", "case note")]
_TYPES = [{"name": "Company / Undertaking", "guidance": "named companies"}]


def test_fixed_class_replaces_pick_list():
    p = _front_matter_prompt(
        filename="m1234.md", content="text", classes=_CLASSES,
        entity_types=_TYPES, known_entities=[],
        class_hint=("Regulatory Decision", 1.0, "already assigned"),
        properties=None,
    )
    assert "already assigned" in p
    assert '"Regulatory Decision"' in p
    assert "pick exactly one" not in p
    assert "Bulletin article" not in p  # the list is gone entirely


def test_uncertain_hint_keeps_pick_list():
    p = _front_matter_prompt(
        filename="m1234.md", content="text", classes=_CLASSES,
        entity_types=_TYPES, known_entities=[],
        class_hint=("Regulatory Decision", 0.8, "filename pattern"),
        properties=None,
    )
    assert "pick exactly one" in p
    assert "Bulletin article" in p
    assert "confirm or overrule" in p
