"""Unit tests for deployment retrieval guidance reaching the planner.

A playbook of kind `retrieval` was installed, 11,975 characters of it, and
nothing read it. Synthesis and validation playbooks both have readers;
retrieval had none, so the guidance was stored, valid, exported, and
addressed to nobody.

The assembly is pure, so these need no DB and no network. The reader that
does the query is a thin wrapper over it.

Run from the backend directory:
`python -m pytest tests/test_retrieval_guidance.py`
"""

import pytest

from app.retrieval_first import (
    _ROUND1_PROMPT,
    _ROUND2_PROMPT,
    _playbook_block,
)

EVERYWHERE = ("house-retrieval", "Use 2 to 4 terms.", True)
SCOPED = ("one-class-conventions", "Numbering for one class.", False)


def test_a_playbook_becomes_a_labelled_block():
    block, skipped = _playbook_block([EVERYWHERE])
    assert "Use 2 to 4 terms." in block
    assert block.startswith("DEPLOYMENT RETRIEVAL GUIDANCE")
    assert skipped == []


def test_nothing_installed_leaves_the_prompt_alone():
    """An empty string, not a header with nothing under it. A deployment that
    installs no retrieval playbook should see the prompt it saw before."""
    block, skipped = _playbook_block([])
    assert block == "" and skipped == []


def test_a_playbook_with_no_content_is_not_a_heading_on_its_own():
    block, _ = _playbook_block([("empty", "   ", True)])
    assert block == ""


def test_a_class_scoped_playbook_is_reported_rather_than_dropped():
    """Planning happens before any document is retrieved, so a playbook scoped
    to a document class has no class to match against and cannot be applied.
    Dropping it silently is the failure this codebase keeps meeting: the
    caller gets the names back so the omission can be recorded."""
    block, skipped = _playbook_block([EVERYWHERE, SCOPED])
    assert "Numbering for one class." not in block
    assert skipped == ["one-class-conventions"]


def test_several_playbooks_are_separated():
    block, _ = _playbook_block([EVERYWHERE, ("second", "Second one.", True)])
    assert "Use 2 to 4 terms." in block and "Second one." in block


@pytest.mark.parametrize("prompt", [_ROUND1_PROMPT, _ROUND2_PROMPT])
def test_both_planning_prompts_take_the_guidance(prompt):
    """Both rounds emit `websearch_queries`, and the guidance is largely about
    how to write them, so both carry it."""
    assert "{guidance}" in prompt


def test_both_prompts_still_format_with_every_slot_filled():
    """A slot added to a prompt without a matching keyword raises KeyError at
    the point of use, which is inside a paid run rather than here."""
    assert _ROUND1_PROMPT.format(
        corpus_map="schema", question="q", domain="some-domain ",
        guidance="G")
    assert _ROUND2_PROMPT.format(matches="m", question="q", guidance="G")
