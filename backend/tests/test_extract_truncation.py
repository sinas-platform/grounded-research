"""Unit tests for the passage extractor's document cap and its telemetry.

Pure helper only: `_number_and_cap` takes text and a cap and returns text plus
a record, so no DB and no network. The wiring into `_extract_passages` is
exercised against the real stack elsewhere, same convention as the other
runner tests.

Run from the backend directory: `python -m pytest tests/test_extract_truncation.py`
"""

from app.services.query_runner import EXTRACT_DOC_CHAR_CAP, _number_and_cap


def _numbered_len(lines: list[str]) -> int:
    """What the helper will produce for these lines, counted independently."""
    return len("\n".join(f"{i+1}: {line}" for i, line in enumerate(lines)))


# ── the cap constant ─────────────────────────────────────────────────────────


def test_cap_is_named_and_unchanged():
    """The value is unchanged by this work; only its name and record are new."""
    assert EXTRACT_DOC_CHAR_CAP == 140_000


# ── under the cap: nothing changes ───────────────────────────────────────────


def test_short_document_is_numbered_and_not_cut():
    text, cut = _number_and_cap("alpha\nbeta\ngamma", cap=1000)
    assert text == "1: alpha\n2: beta\n3: gamma"
    assert cut is None


def test_document_exactly_at_the_cap_is_not_cut():
    """The boundary is inclusive: equal to the cap still fits."""
    lines = ["x" * 10 for _ in range(5)]
    exact = _numbered_len(lines)
    text, cut = _number_and_cap("\n".join(lines), cap=exact)
    assert cut is None
    assert len(text) == exact


def test_empty_document_is_not_cut():
    text, cut = _number_and_cap("", cap=100)
    assert cut is None
    assert text == ""


# ── over the cap: cut on a line boundary ─────────────────────────────────────


def test_cut_lands_on_a_line_boundary_never_mid_line():
    """The reason this change exists: a sliced line keeps its number and reads
    as a complete line to the model."""
    lines = [f"{'a' * 40}" for _ in range(20)]
    text, cut = _number_and_cap("\n".join(lines), cap=200)
    assert cut is not None
    # every line that survived is whole
    for numbered_line in text.split("\n"):
        _, _, body = numbered_line.partition(": ")
        assert body == "a" * 40
    assert not text.endswith("\n")


def test_record_counts_what_was_lost():
    lines = [f"line{i}" for i in range(100)]
    full = _numbered_len(lines)
    text, cut = _number_and_cap("\n".join(lines), cap=200)
    assert cut == {
        "numbered_chars": full,
        "cap": 200,
        "dropped_chars": full - len(text),
        "dropped_lines": 100 - (text.count("\n") + 1),
    }
    assert cut["dropped_chars"] > 0
    assert cut["dropped_lines"] > 0


def test_kept_text_is_within_the_cap():
    lines = [f"{'z' * 60}" for _ in range(50)]
    text, cut = _number_and_cap("\n".join(lines), cap=500)
    assert cut is not None
    assert len(text) <= 500


def test_dropped_lines_and_kept_lines_account_for_every_line():
    lines = [f"row {i}" for i in range(37)]
    text, cut = _number_and_cap("\n".join(lines), cap=120)
    kept_lines = text.count("\n") + 1 if text else 0
    assert kept_lines + cut["dropped_lines"] == 37


# ── the degenerate case ──────────────────────────────────────────────────────


def test_a_single_line_longer_than_the_cap_yields_nothing():
    """Sending a fragment is the failure this removes, so nothing is sent and
    the record says the whole document was dropped."""
    one_long_line = "q" * 5000
    text, cut = _number_and_cap(one_long_line, cap=100)
    assert text == ""
    assert cut["dropped_lines"] == 1
    assert cut["dropped_chars"] == cut["numbered_chars"]


def test_first_line_over_cap_drops_the_rest_too():
    content = "\n".join(["w" * 5000, "short", "shorter"])
    text, cut = _number_and_cap(content, cap=100)
    assert text == ""
    assert cut["dropped_lines"] == 3


# ── shape the telemetry aggregate depends on ─────────────────────────────────


def test_record_carries_every_key_the_aggregate_sums():
    """`_extract_passages` sums dropped_chars and dropped_lines across
    documents and reports cap and numbered_chars per document; a missing key
    would surface as a KeyError mid-run."""
    _, cut = _number_and_cap("\n".join("x" * 30 for _ in range(40)), cap=150)
    assert set(cut) == {"numbered_chars", "cap", "dropped_chars", "dropped_lines"}
    assert all(isinstance(v, int) for v in cut.values())
