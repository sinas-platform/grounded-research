"""A paragraph reference is read off the source, then checked.

A citation that gives a document and a line range leaves a reader with no
paragraph to turn to. Nothing in this system can supply one: which label a
source prints, and where, is knowledge about a corpus, and a pattern here
that claimed to know would be asserting something about documents it has
never seen. So the label is READ — the extractor stops trimming it off the
front of a quote, the drafter copies it into `locator` — and then CHECKED,
deterministically, against the span's own lines, before any judging call.
Found, and it becomes `paragraph_ref`; absent, and the span fails and the
label goes with it.

These tests pin the check's three answers (yes, no, nothing asserted), the
folding that decides what counts as the same label, the fact that the check
runs before the model is asked, and the two prompt changes that make a
locator available to read at all. The last test pins the absence of the
design that was rejected: there is no `paragraph_pattern` anywhere.

Run from the backend directory:
`python -m pytest tests/test_a_locator_is_found_or_the_span_fails.py`
"""

import ast
import inspect
import pathlib
import textwrap

from app.services import faithfulness as fa
from app.services import query_runner as qr

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _prompt(fn) -> str:
    """The prompt as the model receives it — adjacent literals joined, so an
    assertion is on the sentence and not on the column a line broke at."""
    src = inspect.getsource(fn)
    return "".join(
        node.value
        for node in ast.walk(ast.parse(textwrap.dedent(src)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


# ── the check ────────────────────────────────────────────────────────────────

def test_a_label_the_passage_shows_is_accepted():
    assert fa.locator_holds("42", "42. The deciding body held that...") is True
    assert fa.locator_holds("recital 14", "Recital 14 provides that...") is True


def test_a_label_the_passage_does_not_show_is_refused():
    assert fa.locator_holds("recital 99", "Recital 14 provides that...") is False
    assert fa.locator_holds("r.o. 4.3", "r.o. 4.2 Het hof oordeelt") is False


def test_a_span_that_claims_no_label_is_not_judged_on_one():
    """The check is on what was asserted. Asserting nothing is not a defect,
    and failing a span for it would throw away every citation from a source
    that does not number its paragraphs."""
    assert fa.locator_holds(None, "anything") is None
    assert fa.locator_holds("", "anything") is None
    assert fa.locator_holds("   ", "anything") is None
    assert fa.locator_holds("—", "anything") is None


def test_punctuation_and_spacing_do_not_decide_whether_a_label_matches():
    """What varies between a source printing a label and a faithful copy of
    it is exactly the punctuation and the spacing."""
    assert fa.locator_holds("r.o. 4.2", "R.O.4.2 — het hof") is True
    assert fa.locator_holds("  42.  ", "para 42 of the judgment") is True
    assert fa._fold("R.O. 4.2 ") == "ro42"


def test_the_label_is_checked_against_the_spans_own_lines():
    """Not the ±5-line judging window. The margin exists so a judge can read
    around a passage; a locator has to be IN the passage it labels, or the
    check would accept a paragraph number belonging to the passage next
    door."""
    content = "40. earlier\n41. earlier\n42. the cited sentence\n43. later"
    span = {"line_from": 3, "line_to": 3}
    assert fa._span_lines(content, span) == "42. the cited sentence"
    assert fa.locator_holds("42", fa._span_lines(content, span)) is True
    assert fa.locator_holds("41", fa._span_lines(content, span)) is False


def test_coordinates_that_name_no_lines_yield_no_text_rather_than_raising():
    content = "a\nb\nc"
    assert fa._span_lines(content, {"line_from": 9, "line_to": 10}) == ""
    assert fa._span_lines(content, {"line_from": 3, "line_to": 1}) == ""
    assert fa._span_lines(content, {"line_from": "x"}) == ""


# ── where it runs ────────────────────────────────────────────────────────────

def _validate_src() -> str:
    return inspect.getsource(fa.validate_answer_evidence)


def test_the_check_runs_before_any_model_is_asked():
    """It is deterministic, so paying for a judgment on a span that is about
    to fail on arithmetic is paying for nothing."""
    src = _validate_src()
    assert src.index("locator_holds(") < src.index("asyncio.gather(")


def test_a_refused_label_fails_the_span_and_is_discarded():
    src = _validate_src()
    i = src.index("locator_holds(")
    body = src[i:i + 1200]
    assert "ev.paragraph_ref = None" in body
    assert '"validated": False' in body


def test_an_accepted_label_becomes_the_rows_paragraph_ref():
    src = _validate_src()
    assert "ev.paragraph_ref = str((ev.span or {}).get(\"locator\"))" in src


# ── where it comes from ──────────────────────────────────────────────────────

def test_the_drafter_is_asked_for_the_locator_and_told_not_to_invent_one():
    src = _prompt(qr._draft_from_extracts)
    assert '"locator"' in src
    assert "OWN label for the paragraph the passage sits in" in src
    assert "Never derive one, never count paragraphs" in src


def test_the_reviser_is_asked_for_it_too():
    """The reviser writes claims and evidence rows the same way the drafter
    does, and the same check runs over them.

    It is asked once, not twice: drafting and revising are one conversation,
    so the rule is stated in the brief and the patch schema asks for the
    field. A second copy on every round would be the same sentence paid for
    ten times.
    """
    assert '"locator"' in qr._PATCH_SCHEMA
    brief = _prompt(qr._draft_from_extracts)
    assert "Never derive one, never count paragraphs" in brief


def test_the_extractor_is_told_to_keep_the_label_on_the_front_of_the_quote():
    """"Quote the sentence, not the paragraph" was trimming off the one
    token that locates the sentence, and nothing downstream can put it
    back."""
    src = _prompt(qr._extract_passages)
    assert "begin the quote with that label, exactly as printed" in src
    assert "Where the passage shows none, do not invent one" in src


def test_the_locator_reaches_the_stored_span():
    assert '"locator": _locator_of(ev_)' in inspect.getsource(qr._draft_from_extracts)
    assert '"locator": sp.get("locator")' in inspect.getsource(qr._bind_spans)
    assert '"locator": _locator_of(e)' in inspect.getsource(qr._spans_of)


def test_a_locator_that_is_not_a_short_label_is_not_stored_as_one():
    assert qr._locator_of({}) is None
    assert qr._locator_of({"locator": None}) is None
    assert qr._locator_of({"locator": "null"}) is None
    assert qr._locator_of({"locator": ["42"]}) is None
    assert qr._locator_of({"locator": True}) is None
    assert qr._locator_of({"locator": " r.o. 4.2 "}) == "r.o. 4.2"
    assert qr._locator_of({"locator": 42}) == "42"
    assert len(qr._locator_of({"locator": "x" * 400})) == 50


# ── the design that was not taken ────────────────────────────────────────────

def test_no_configuration_key_claims_to_know_what_a_paragraph_looks_like():
    """The rejected design had each document class declare a
    `paragraph_pattern` regex. It cannot work: a class spans sources that
    number themselves differently, a pattern that matches "42." matches a
    monetary figure at the start of a line, and a deployment that gets it
    wrong produces confident wrong paragraph numbers on every citation. The
    key must not come back by accident."""
    skip = ("node_modules", "__pycache__", ".git", "tests")
    hits = []
    for path in list(REPO.rglob("*.py")) + list(REPO.rglob("*.yaml")) \
            + list(REPO.rglob("*.ts")) + list(REPO.rglob("*.tsx")):
        # Tests are excluded: this one and the package-schema one both name
        # the key in order to refuse it, and a guard that cannot be written
        # down is not a guard. Code and configuration are what is scanned.
        if any(p in path.parts for p in skip):
            continue
        if "paragraph_pattern" in path.read_text(errors="ignore"):
            hits.append(str(path.relative_to(REPO)))
    assert not hits, "paragraph_pattern is back in: " + ", ".join(hits)


def test_a_wild_line_range_does_not_stop_the_server():
    """A passage claiming a huge range is located in bounded time.

    The extractor reports where it believes a quote sits and is sometimes very
    wrong: across the stored runs the claimed ranges reach 1,967 lines. The
    locator used to scan every start line and re-canonicalise a growing
    accumulator inside that scan, which on a range that size is millions of
    passes over a string that grows to the length of the span. It runs on the
    event loop, so one such passage froze the server — 55 minutes of one core
    with every concurrent run stopped behind it.

    Two seconds is not a performance target; it is far enough below the
    minutes the old shape took that a regression cannot hide under it.
    """
    import time

    from app.services.query_runner import _locate_passage

    lines = [f"Paragraph {i} of a long chapter, saying something unremarkable "
             f"about the procedure and its limits." for i in range(1, 2001)]
    lines[1500] = ("The tribunal held that the requirement as to position and "
                   "status must be fulfilled by the person from whom the "
                   "communication comes.")
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(lines, start=1))
    quoted = ("The tribunal held that the requirement as to position and "
              "status must be fulfilled by the person from whom the "
              "communication comes.")

    start = time.perf_counter()
    found = _locate_passage(numbered, 1, 2000, quoted)
    elapsed = time.perf_counter() - start

    assert found == (1501, 1501), found
    assert elapsed < 2.0, f"took {elapsed:.1f}s"
