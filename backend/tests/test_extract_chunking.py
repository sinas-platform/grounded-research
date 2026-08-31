"""Unit tests for splitting an over-cap document into chunks on its sections.

Pure helpers only: they take text, a table of contents and a cap, and return
chunks, so no DB and no network. The wiring into `_extract_passages` is
exercised against the real stack elsewhere, same convention as the other
runner tests.

Run from the backend directory: `python -m pytest tests/test_extract_chunking.py`
"""

from app.services.query_runner import (
    EXTRACT_DOC_CHAR_CAP,
    EXTRACT_MAX_ROUNDS,
    _chunk_numbered,
    _chunk_telemetry,
    _line_windows,
    _numbered_lines,
    _span_chars,
    _toc_starts,
)

DOC = "\n".join(f"line {i}" for i in range(1, 101))
TOC = [
    {"level": 1, "title": "A", "line": 1, "line_to": 50},
    {"level": 2, "title": "B", "line": 20, "line_to": 50},
    {"level": 1, "title": "C", "line": 51, "line_to": 100},
]


# ── the constants ────────────────────────────────────────────────────────────


def test_cap_is_unchanged_by_this_work():
    assert EXTRACT_DOC_CHAR_CAP == 140_000


def test_rounds_are_bounded():
    """Unbounded, one claim on the corpus's worst document would make 27
    extraction calls."""
    assert EXTRACT_MAX_ROUNDS == 4


# ── numbering is absolute, which is the whole point ──────────────────────────


def test_lines_are_numbered_from_one_over_the_whole_document():
    assert _numbered_lines("a\nb\nc") == ["1: a", "2: b", "3: c"]


def test_line_numbers_survive_the_split():
    """A passage returned as lines 4,100-4,120 has to mean that in the
    document. Every chunk's first line carries its own absolute number."""
    chunks, _ = _chunk_numbered(DOC, TOC, 400)
    assert len(chunks) > 1
    for c in chunks:
        assert c["text"].split("\n")[0].startswith(f"{c['line_from']}: ")


# ── under the cap: byte-identical to the path taken before ───────────────────


def test_document_under_the_cap_is_one_chunk():
    chunks, cut = _chunk_numbered(DOC, TOC, EXTRACT_DOC_CHAR_CAP)
    assert cut is None
    assert len(chunks) == 1
    assert chunks[0]["text"] == "\n".join(_numbered_lines(DOC))
    assert (chunks[0]["line_from"], chunks[0]["line_to"]) == (1, 100)
    assert chunks[0]["strategy"] == "whole"


def test_document_exactly_at_the_cap_is_not_split():
    exact = len("\n".join(_numbered_lines(DOC)))
    chunks, cut = _chunk_numbered(DOC, TOC, exact)
    assert len(chunks) == 1 and cut is None


def test_empty_document_yields_nothing():
    assert _chunk_numbered("", None, EXTRACT_DOC_CHAR_CAP) == ([], None)


# ── the table of contents supplies split points, not content ─────────────────


def test_toc_starts_ignore_ranges_because_ranges_nest():
    """`_close_ranges` ends an entry where the next same-or-higher level
    starts, so a level-1 entry spans its level-2 children. Reading `line_to`
    would emit those lines once per level."""
    assert _toc_starts(TOC, 100) == [20, 51]


def test_toc_starts_drop_out_of_range_and_malformed_entries():
    assert _toc_starts([{"line": 0}, {"line": 5}, {"line": 999}], 100) == [5]
    assert _toc_starts([{"line": "x"}, "nope", {"nope": 1}, {"line": 7}], 100) == [7]
    assert _toc_starts(None, 100) == []


# ── chunking on section boundaries ───────────────────────────────────────────


def test_chunks_cover_the_document_without_gap_or_overlap():
    chunks, _ = _chunk_numbered(DOC, TOC, 400)
    assert chunks[0]["line_from"] == 1
    assert chunks[-1]["line_to"] == 100
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a["line_to"] + 1 == b["line_from"]


def test_no_chunk_exceeds_the_cap():
    chunks, _ = _chunk_numbered(DOC, TOC, 400)
    assert all(len(c["text"]) <= 400 for c in chunks)


def test_boundaries_fall_on_section_starts_when_every_section_fits():
    chunks, _ = _chunk_numbered(DOC, TOC, 800)
    starts = {c["line_from"] for c in chunks[1:]}
    assert starts and starts <= set(_toc_starts(TOC, 100))


def test_sections_are_packed_not_sent_one_per_call():
    """The corpus averages 85 sections a document and reaches 400. A call per
    section would trade a truncation problem for a cost one."""
    many = [{"level": 1, "title": str(i), "line": i, "line_to": i} for i in range(1, 101)]
    packed, _ = _chunk_numbered(DOC, many, 100_000)
    assert len(packed) == 1
    packed_small, _ = _chunk_numbered(DOC, many, 400)
    assert 1 < len(packed_small) < 100


# ── a section bigger than the cap, and a document with no sections ───────────


def test_a_section_larger_than_the_cap_is_windowed_on_line_boundaries():
    big = "\n".join("x" * 90 for _ in range(20))
    one = [{"level": 1, "title": "solo", "line": 1, "line_to": 20}]
    chunks, _ = _chunk_numbered(big, one, 300)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= 300 for c in chunks)
    assert chunks[0]["line_from"] == 1 and chunks[-1]["line_to"] == 20


def test_a_document_with_no_toc_entries_falls_back_to_line_windows():
    chunks, _ = _chunk_numbered(DOC, [], 400)
    assert len(chunks) > 1
    assert chunks[0]["strategy"] == "lines"
    assert chunks[0]["line_from"] == 1 and chunks[-1]["line_to"] == 100
    assert all(len(c["text"]) <= 400 for c in chunks)


def test_windows_never_cut_mid_line():
    numbered = _numbered_lines(DOC)
    for a, b in _line_windows(numbered, 1, 100, 400):
        assert _span_chars(numbered, a, b) <= 400


# ── the one case where content still cannot be represented ───────────────────


def test_a_line_longer_than_the_whole_budget_is_dropped_and_recorded():
    """The only residual truncation after this change. Sending part of the
    line would be the fragment the boundary cut exists to remove."""
    patho = "short\n" + "y" * 5000 + "\nalso short"
    chunks, cut = _chunk_numbered(patho, [], 100)
    assert cut is not None
    assert cut["dropped_lines"] == 1
    assert all(len(c["text"]) <= 100 for c in chunks)


def test_the_dropped_line_is_not_reinstated_by_packing():
    """A dropped line leaves a hole. A chunk is emitted as the slice from its
    first line to its last, so merging across the hole would put the line back
    and blow the cap it was dropped for."""
    patho = "short\n" + "y" * 5000 + "\nalso short"
    chunks, _ = _chunk_numbered(patho, [], 100)
    assert not any("yyyy" in c["text"] for c in chunks)


# ── telemetry ────────────────────────────────────────────────────────────────


def test_no_chunking_emits_no_keys():
    assert _chunk_telemetry([{"chunked": []}]) == {}


def test_chunk_telemetry_deduplicates_by_filename():
    """A document anchoring several claims is chunked once per claim, and
    counting it per claim would report a corpus property as a run one."""
    rows = [
        {"chunked": [{"filename": "a.md", "chunks": 3, "strategy": "toc", "read": 3}]},
        {
            "chunked": [
                {"filename": "a.md", "chunks": 3, "strategy": "toc", "read": 3},
                {"filename": "b.md", "chunks": 2, "strategy": "lines", "read": 2},
            ]
        },
    ]
    t = _chunk_telemetry(rows)
    assert t["documents_chunked"] == 2
    assert t["chunks_total"] == 5
    assert t["extra_extraction_calls"] == 4
    assert t["rounds_capped"] == 0


def test_chunk_telemetry_flags_when_the_round_cap_bit():
    t = _chunk_telemetry(
        [{"chunked": [{"filename": "c.md", "chunks": 9, "strategy": "toc", "read": 4}]}]
    )
    assert t["rounds_capped"] == 1
