"""Source text and fixtures name no real party and no real document.

A list of names was tried twice in one day and was incomplete both times. The
first pass missed three undertakings, the second missed two more and two
filenames, and each gap was found only because somebody looked again. A list
of a deployment's vocabulary is also the thing this repository must not carry,
so it could never have been the answer.

What is checked instead is SHAPE. Both checks are deliberately narrow, because
the first draft of this file flagged `Regulatory Decision`, `Google Sheets`
and `The Court` and a guard that cries wolf gets turned off and then defends
nothing. Narrow means it will miss some; quiet means it will still be running
when it catches the next one.

  A FILENAME is suspect when it carries a collection's own scheme: the
  register-style `62018CJ0606.md`, a long multi-hyphen slug like
  `amazon-deliveroo-merger-inquiry--p04.md`, or a bare six-digit serial.
  Short descriptive names are what a fixture wants and are not matched.

  A PARTY is suspect in a case caption, meaning capitalised words either side
  of ` v ` or ` v. `. That is the one context where a proper name is certainly
  a party rather than a class name, an institution or a product.

The invented names this suite uses are listed, and that IS a list, but not the
kind that goes stale silently: adding one is a deliberate line in this file,
and forgetting to add it makes the suite fail loudly rather than pass quietly.

Bare identifiers are untouched on purpose. `C-606/18` and `AT.39796` name
nobody, a pattern cannot be tested without one, and this suite is full of them
for exactly that reason.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

_INVENTED = {"Ashgrove", "Bellhaven", "Carwood", "Dunmore", "Kestrel",
             "Northmoor", "Phantom", "Systems", "Group", "Holdings", "Retail",
             "Utilities", "Authority", "Tribunal", "Case"}

# A caption at the start of a sentence sweeps the sentence's first word into
# itself. These are English, not anyone's vocabulary, and leaving them out
# would make every "In X v Y" a false alarm.
_SENTENCE_LEAD = {"In", "The", "See", "On", "At", "For", "Per", "From"}

_STRING = re.compile(r'"([^"\n]{6,300})"')

# A filename carrying a collection's scheme rather than a fixture's label.
_REGISTER = re.compile(r'"(\d{5}[A-Z]{2}\d{4}\.md)"')
_LONG_SLUG = re.compile(r'"([a-z0-9]+(?:-+[a-z0-9]+){3,}\.md)"')
_BARE_SERIAL = re.compile(r'"(\d{6,}\.md)"')

# A case caption: capitalised words, ` v `/` v. `, capitalised words.
_CAPTION = re.compile(
    r"\b((?:[A-Z][A-Za-z.]+\s+){0,3}[A-Z][A-Za-z.]+)\s+v\.?\s+"
    r"([A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+){0,3})\b")


def _sources():
    for d in ("app", "tests"):
        for f in sorted((BACKEND / d).rglob("*.py")):
            if f.name == Path(__file__).name:
                continue
            yield f, f.read_text(encoding="utf8").splitlines()


def test_no_fixture_names_a_document_from_a_real_collection():
    """A fixture needs a filename. It does not need one that exists."""
    bad = []
    for f, lines in _sources():
        for n, line in enumerate(lines, 1):
            for rx, why in ((_REGISTER, "register scheme"),
                            (_LONG_SLUG, "multi-hyphen slug"),
                            (_BARE_SERIAL, "bare serial")):
                for m in rx.finditer(line):
                    bad.append(f"{f.relative_to(BACKEND)}:{n}: "
                               f"{m.group(1)} ({why})")
    assert not bad, (
        "these carry a collection's naming scheme; a fixture wants a short "
        "descriptive name:\n  " + "\n  ".join(bad))


def test_no_case_caption_names_a_real_party():
    """`X v Y` is the one place a capitalised name is certainly a party."""
    bad = []
    for f, lines in _sources():
        for n, line in enumerate(lines, 1):
            for lit in _STRING.findall(line):
                for m in _CAPTION.finditer(lit):
                    words = [w for w in (m.group(1) + " " + m.group(2)).split()
                             if w not in _SENTENCE_LEAD]
                    if any(w.strip(".") not in _INVENTED for w in words):
                        bad.append(f"{f.relative_to(BACKEND)}:{n}: "
                                   f"{m.group(0)}")
    assert not bad, (
        "these read as a real case caption; a fixture wants an invented "
        "party:\n  " + "\n  ".join(bad))


def test_the_checks_can_see_an_offender():
    """A guard that matches nothing cannot be told from a clean tree, which is
    the failure this repository keeps meeting. Both patterns are exercised
    against text that must trip them and text that must not, so a narrowing
    that silently empties them fails here first."""
    assert _REGISTER.search('"62018CJ0606.md"')
    assert _LONG_SLUG.search('"amazon-deliveroo-merger-inquiry--p04.md"')
    assert _BARE_SERIAL.search('"108587.md"')
    for ok in ('"a.md"', '"owed.md"', '"a-judgment.md"', '"an-inquiry--p04.md"'):
        assert not (_REGISTER.search(ok) or _LONG_SLUG.search(ok)
                    or _BARE_SERIAL.search(ok)), ok

    assert _CAPTION.search("In Ferriere Nord v Commission the tribunal held")
    assert _CAPTION.search("Strintzis Lines Shipping v. Commission")
    assert not _CAPTION.search("the tribunal held in Case C-606/18")
    caption = _CAPTION.search("In Ashgrove Systems v Authority")
    words = [w for w in caption.group(0).replace(" v ", " ").split()
             if w not in _SENTENCE_LEAD]
    assert caption and all(w in _INVENTED for w in words), words
