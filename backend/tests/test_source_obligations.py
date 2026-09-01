"""The obligation ledger's reviser-facing contract.

A gate-named source is a debt: cited, or waived with a recorded rationale
— never forgotten. These tests pin the parseable halves: the reviser can
discharge by waive, a waive without a real rationale is not a waive, and
the prompt/contract carry the obligation language the loop relies on.
"""

from pathlib import Path

from app.services import obligations
from app.services.query_runner import _parse_patch

SRC = Path(obligations.__file__).with_name("query_runner.py").read_text()


def test_a_waive_with_a_rationale_parses():
    patch = _parse_patch(
        '{"revise": [], "add": [], "drop": [], "keep": [],'
        ' "waive": [{"doc": "62020TJ0451.md", "rationale": '
        '"its paragraphs concern access requests, not on-site sealing"}]}')
    assert patch["waive"] == [{
        "doc": "62020TJ0451.md",
        "rationale": "its paragraphs concern access requests, not on-site sealing"}]


def test_a_waive_without_a_real_rationale_is_ignored():
    # A one-word rationale is not "read the passages and here is why" —
    # filtered out, and a reply containing nothing else is no patch at all.
    patch = _parse_patch(
        '{"revise": [], "add": [], "drop": [], "keep": [],'
        ' "waive": [{"doc": "x.md", "rationale": "no"}]}')
    assert patch is None


def test_the_gate_feeds_obligations_and_marks_them():
    assert "Owed source unused" in SRC
    assert "[obligated document:" in SRC
    assert "obligations.record" in SRC and "obligations.to_feed" in SRC


def test_the_reviser_contract_offers_the_waive_path():
    assert '"waive": [{"doc"' in SRC
