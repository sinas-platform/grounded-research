"""Source text must not reference a runtime record by its identifier.

A comment saying a defect was measured on a particular run tells a reader
something they cannot act on: the record is in a deployment's database, not in
this repository, and nobody reading the code can open it. The measurement is
the part that travels. Deleting the identifier costs the comment nothing and
keeps the repository readable by people with no access to whoever ran it.

This is structural, and deliberately so. It matches the SHAPE of an identifier
rather than any list of forbidden words: a list of a deployment's vocabulary
would itself be the thing this repository must not carry, and it would go
stale the moment a deployment changed. What is checked here is that source
text does not name a specific record, which is true of every deployment there
will ever be.

It runs on every test run because the three times this was found by hand were
three times somebody happened to look, and the places it hides are comments,
docstrings and test fixtures, which is where nobody is looking.

Two shapes are matched, both of them structural:

  UUIDs, and their first segment used alone as a short reference, which is
  how a run is named in conversation and therefore how it reaches a comment.

  Question or case reference tags of the form Q<digits>, which name an entry
  in a deployment's own benchmark. `Qnn` is left alone: it is a template, not
  a reference, and the difference is exactly the point.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

# A full UUID, or the eight-hex short form a run is referred to by. Bounded by
# non-hex so a longer hash is not clipped into a false positive.
# Case-insensitive in the body, not only in the boundaries. The classes were
# lower-case while the look-arounds already allowed either case, so an
# upper-case or mixed-case identifier passed the guard that exists to catch it,
# and a guard that misses the shape it is named for is worse than none.
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|(?<![0-9a-fA-F-])[0-9a-fA-F]{8}(?![0-9a-fA-F-])"
)
_QUESTION_TAG = re.compile(r"\bQ\d{1,3}\b")

# Alembic revision identifiers are the file's whole purpose and are not
# references to a runtime record.
_REVISION_LINE = re.compile(r"^\s*(down_)?revision\s*[:=]")


def _comment_and_string_text(path: Path) -> list[tuple[int, str]]:
    """Every line that can carry prose: a comment, or a line inside a triple
    quoted block. Code is not read, so a literal in a query is not a finding.
    """
    out: list[tuple[int, str]] = []
    in_block = False
    for n, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1):
        fences = line.count('"""') + line.count("'''")
        if in_block or fences:
            out.append((n, line))
            if fences % 2:
                in_block = not in_block
            continue
        if "#" in line:
            out.append((n, line[line.index("#"):]))
    return out


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        for n, text in _comment_and_string_text(path):
            if _REVISION_LINE.match(text):
                continue
            for m in pattern.finditer(text):
                found.append(
                    f"{path.relative_to(APP.parent)}:{n}: {m.group(0)}")
    return found


def test_no_comment_names_a_run_by_its_identifier():
    """A run id in a comment is a reference nobody outside one deployment can
    follow. Say what was measured, not which record it was measured on."""
    offenders = _offenders(_UUID)
    assert not offenders, (
        "source text names a runtime record; keep the measurement and drop "
        "the identifier:\n  " + "\n  ".join(offenders))


def test_no_comment_names_a_deployments_benchmark_question():
    """A tag with digits in it names an entry in some deployment's benchmark.
    `Qnn` as a format template is fine, and that difference is why this
    matches the digits rather than the letter."""
    offenders = _offenders(_QUESTION_TAG)
    assert not offenders, (
        "source text names a deployment's benchmark question; a template "
        "reads `Qnn`:\n  " + "\n  ".join(offenders))


def test_the_check_can_see_an_offender():
    """The failure mode this whole file exists to prevent is a check that
    matches nothing and cannot be told from a clean tree, so the patterns are
    exercised against text that must trip them."""
    # Fabricated, and deliberately so. A guard against naming a real record
    # that names one to prove it works would be the joke version of itself.
    assert _UUID.search("# measured on run deadbeef, which burned $23.81")
    assert _UUID.search("# see abcdef01-2345-6789-abcd-ef0123456789 for the trace")
    assert _QUESTION_TAG.search("# Q99 swept twice and its first finding is gone")
    assert not _QUESTION_TAG.search('# titled "Qnn — Topic — sub-topic"')
    assert not _UUID.search("revision = '0036'")


def test_an_upper_case_identifier_is_caught_too():
    """The classes were lower-case while the boundaries allowed either case,
    so the one shape the guard is named for slipped past it."""
    for ident in ("A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
                  "A1B2C3D4", "a1B2c3D4"):
        assert _UUID.search(ident), ident
