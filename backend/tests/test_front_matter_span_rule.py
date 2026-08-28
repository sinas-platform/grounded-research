"""A span inside the front-matter envelope cannot support a claim.

The expert reviews found claims resting on a document's title block — "the
Court heard an appeal", cited to the header — and the model judge sometimes
passed them. The envelope is written by this pipeline's own ingestion, so its
boundary is exact per document: the rule is deterministic without assuming
anything about the source document's layout.
"""

from app.services.faithfulness import _front_matter_extent

DOC = """---
title: Judgment of the Court in Case C-000/00
court: Court of Justice
date: 2000-01-01
---
1. By its judgment the Court held that the contested decision must be
annulled in part.
"""


def test_extent_covers_the_fenced_block():
    assert _front_matter_extent(DOC) == 5


def test_no_front_matter_means_no_extent():
    assert _front_matter_extent("1. The Court held...") == 0
    assert _front_matter_extent("") == 0


def test_unclosed_fence_is_not_front_matter():
    # An opening --- with no close within the scan window is document text,
    # not an envelope; nothing may be auto-failed on it.
    assert _front_matter_extent("---\n" + "x\n" * 80) == 0


def test_body_lines_are_outside_the_extent():
    # Line 6 is the first body line; a span there must never be caught.
    extent = _front_matter_extent(DOC)
    assert extent < 6
