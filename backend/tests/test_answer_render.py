

def test_a_test_renders_as_a_test_whatever_its_kind():
    """The kind and the test object come apart. `normalise_claim` keeps a test
    only for a claim of kind `test`, but a revision that changes the kind and
    does not resend the test leaves the object on the row, because the update
    writes `claim_kind` and only reaches `test` when the patch carries one.

    Measured on a published answer: one claim held a full two-condition test object,
    name and two ordered cumulative conditions, with `claim_kind` set to
    `rule`. It printed as a sentence. That is the defect a review of
    published answers reported first: a test not set out as a test.
    """
    from app.services.answer_render import _has_test

    orphaned = {
        "claim_kind": "rule",
        "claim_text": "The tribunal stated the two-limb test.",
        "test": {"name": "Kestrel two-limb test", "conditions": [
            {"text": "the exchange concerns the matter in dispute",
             "cumulative": True},
            {"text": "the exchange comes from an outside adviser",
             "cumulative": True}]},
    }
    assert _has_test(orphaned), "a test object renders as a test whatever the kind says"


def test_a_claim_with_no_test_object_is_not_a_test_block():
    from app.services.answer_render import _has_test

    assert not _has_test({"claim_kind": "test", "test": None})
    assert not _has_test({"claim_kind": "rule", "claim_text": "x"})


def test_one_condition_is_a_rule_not_a_test():
    """The same floor `normalise_test` applies: a test of one condition is a
    rule and is stored as one, so the block must not claim otherwise."""
    from app.services.answer_render import _has_test

    assert not _has_test({"test": {"conditions": [{"text": "only one"}]}})
    assert not _has_test({"test": {"conditions": [{"text": "a"}, {"text": "  "}]}})
    assert _has_test({"test": {"conditions": [{"text": "a"}, {"text": "b"}]}})


def _orphaned_claim(seq=1, kind="rule"):
    """A claim carrying a test object under a kind that is not `test`.

    The state a revision leaves behind: it writes `claim_kind` and reaches
    `test` only when the patch carries one, so the object outlives the kind.
    """
    return {
        "id": f"c{seq}", "sequence": seq, "claim_kind": kind,
        "section": "analysis", "position": seq, "part_index": None,
        "claim_text": "The court stated the test.",
        "test": {"name": "The two-condition test", "conditions": [
            {"text": "the first condition holds", "cumulative": True},
            {"text": "the second condition holds", "cumulative": True}]},
    }


def test_an_orphaned_test_renders_as_a_block_not_a_sentence():
    """The regression itself, through `render_markdown` rather than through
    the helper: a test object under kind `rule` must reach the block."""
    from app.services.answer_render import render_markdown

    out = render_markdown(
        {"question": "Q?", "question_parts": None, "open_notes": None,
         "law_stated_as_at": None},
        [_orphaned_claim()], [], {}).markdown

    assert "the conditions, in order (cumulative)" in out
    assert "1. the first condition holds" in out
    assert "2. the second condition holds" in out


def test_an_orphaned_test_breaks_the_paragraph_around_it():
    """A test block is its own paragraph. A neighbouring claim must not be
    glued onto it, which is what happens when the block is inlined."""
    from app.services.answer_render import render_markdown

    before = {"id": "c0", "sequence": 0, "claim_kind": "rule",
              "section": "analysis", "position": 0, "part_index": None,
              "claim_text": "A sentence before.", "test": None}
    after = {"id": "c2", "sequence": 2, "claim_kind": "application",
             "section": "analysis", "position": 2, "part_index": None,
             "claim_text": "A sentence after.", "test": None}

    out = render_markdown(
        {"question": "Q?", "question_parts": None, "open_notes": None,
         "law_stated_as_at": None},
        [before, _orphaned_claim(seq=1), after], [], {}).markdown

    assert "A sentence before. **The two-condition test**" not in out
    assert "the second condition holds A sentence after." not in out


def test_a_claim_of_kind_test_still_renders_as_a_block():
    """The kind path must keep working; this widens the gate, it does not
    move it."""
    from app.services.answer_render import render_markdown

    out = render_markdown(
        {"question": "Q?", "question_parts": None, "open_notes": None,
         "law_stated_as_at": None},
        [_orphaned_claim(kind="test")], [], {}).markdown

    assert "1. the first condition holds" in out
