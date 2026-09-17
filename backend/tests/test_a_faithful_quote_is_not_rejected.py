"""A quote that is in the source is not thrown away for how the source is typed.

THE FAILURE. The verifier exists to stop a fabricated quote, and it rejected
1,043 of 3,933 proposed passages across the stored runs. Four of those
rejections were drawn at random from the runs and looked up by hand in the
documents they name: three were present, verbatim, on exactly the line the
extractor claimed. They failed because the stored markdown carries emphasis
markers and HTML entities, and the rendered text a faithful copy reproduces
does not.

WHY IT MATTERED MORE THAN THE COUNT. The documents carrying the most of that
markup are the judgments. So the check silently preferred sources it could
verify — bulletins and commentary — over the ones that decide the question,
which is exactly what the expert review kept objecting to: an answer resting
on a bulletin reporting a judgment, with the judgment itself cited nowhere.

The cases below are the real ones, reduced to the characters that broke them.
"""

from app.services.query_runner import _canonical, _verify_passage


def _numbered(*lines: str) -> str:
    return "\n".join(f"{i}: {t}" for i, t in enumerate(lines, start=1))


def test_emphasis_markers_are_not_part_of_the_sentence():
    """`**14.** The Court held` is quoted by any reader as `14. The Court held`."""
    stored = "**14.** The tribunal explained that the requirement is fulfilled."
    quoted = "14. The tribunal explained that the requirement is fulfilled."
    assert _canonical(stored) == _canonical(quoted)


def test_an_entity_is_the_character_it_draws():
    """A non-breaking space is a space, and a numeric quote mark is a quote."""
    stored = "**14.**&nbsp;The tribunal said &#8220;the requirement applies&#8221;."
    quoted = 'The tribunal said "the requirement applies".'
    assert _canonical(quoted) in _canonical(stored)


def test_an_ampersand_in_running_text_is_left_alone():
    """The entity rule ends at a semicolon, so a party name keeps its `&`."""
    assert "&" in _canonical("Kestrel & Northmoor Holdings v the authority")


def test_the_quote_that_was_wrongly_rejected_now_verifies():
    """The shape of the real rejection, from a document in the collection.

    Stored with emphasis and entities; quoted as it is drawn. The extractor
    named the line correctly and the passage was discarded anyway.
    """
    numbered = _numbered(
        "Some earlier paragraph of no interest here.",
        "**14.**&nbsp;The tribunal explained in *Kestrel Holdings v.* "
        "*Northmoor* that &#8220;*the requirement as to the position and "
        "status of an independent adviser must be fulfilled by the person "
        "from whom the communication comes*&#8221;.",
        "A later paragraph, equally uninteresting.")
    quoted = ('14. The tribunal explained in Kestrel Holdings v. Northmoor '
              'that "the requirement as to the position and status of an '
              'independent adviser must be fulfilled by the person from whom '
              'the communication comes".')
    assert _verify_passage(numbered, 2, 2, quoted)


def test_a_fabricated_quote_is_still_rejected():
    """The folding must not make the check permissive.

    Nothing added here removes, adds or reorders a word — so a sentence that
    is not in the source still does not match one that is.
    """
    numbered = _numbered("**14.**&nbsp;The tribunal explained the requirement.")
    invented = ("14. The tribunal explained that the requirement does not "
                "apply to an employed adviser at all.")
    assert not _verify_passage(numbered, 1, 1, invented)


def test_a_quote_from_a_different_line_is_still_rejected():
    """Widening is two lines, not the whole document."""
    numbered = _numbered(
        "**1.** The first paragraph, which is about one thing entirely.",
        *[f"Filler line {i} standing between them." for i in range(2, 9)],
        "**9.** The ninth paragraph, which is about something else entirely.")
    quoted = "9. The ninth paragraph, which is about something else entirely."
    assert not _verify_passage(numbered, 1, 1, quoted)
