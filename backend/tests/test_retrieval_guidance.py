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


def test_a_dossier_only_scope_is_a_restriction_not_the_sentinel():
    """Reading document_class_id alone cannot tell them apart.

    The sentinel the importer writes for an everywhere-scoped playbook has
    both class refs null. A dossier-scoped playbook has a null document
    class and a real dossier class. Looking at the document class alone
    makes the second look like the first, and a playbook restricted to one
    dossier class is then injected into every plan.
    """
    import inspect
    from app import retrieval_first as rf

    src = inspect.getsource(rf._retrieval_guidance)
    assert "dossier_class_id" in src, (
        "the scope query must read both class refs; reading one makes a "
        "dossier-only scope indistinguishable from the everywhere sentinel")
    assert "cls_id is not None or dossier_id is not None" in src, (
        "either ref being set is a restriction")


def test_the_second_pass_merges_finds_without_duplicating_them():
    """A document both passes found rises; one only the second found enters."""
    from app.retrieval_first import merge_ranked

    ranked = [{"document_id": "a", "filename": "a.md", "score": 9.0,
               "reason": "matched 'x'"},
              {"document_id": "b", "filename": "b.md", "score": 4.0,
               "reason": "mentions Y x3"}]
    extra = {"b": {"filename": "b.md", "score": 6.0,
                   "reasons": ["'virtual data room' (summary)"]},
             "c": {"filename": "c.md", "score": 12.0,
                   "reasons": ["'virtual data room' (summary)"]}}

    out = merge_ranked(ranked, extra)
    assert [r["document_id"] for r in out] == ["c", "b", "a"]
    assert len(out) == 3, "a document both passes found must not appear twice"
    # provenance survives the merge, both halves of it
    b = next(r for r in out if r["document_id"] == "b")
    assert "mentions Y" in b["reason"] and "virtual data room" in b["reason"]
    assert b["score"] == 10.0
    # one only the second pass found carries the term as its whole reason
    c = next(r for r in out if r["document_id"] == "c")
    assert c["reason"].startswith("second pass:")


def test_the_merge_is_stable_on_equal_scores():
    """Membership must not be decided by the order rows came back in."""
    from app.retrieval_first import merge_ranked

    ranked = [{"document_id": "z", "filename": "z.md", "score": 5.0,
               "reason": "r"},
              {"document_id": "a", "filename": "a.md", "score": 5.0,
               "reason": "r"}]
    assert [r["document_id"] for r in merge_ranked(ranked, {})] == ["a", "z"]


def test_a_second_pass_that_finds_nothing_changes_nothing():
    from app.retrieval_first import merge_ranked

    ranked = [{"document_id": "a", "filename": "a.md", "score": 1.0,
               "reason": "r"}]
    assert merge_ranked(ranked, {}) == ranked
