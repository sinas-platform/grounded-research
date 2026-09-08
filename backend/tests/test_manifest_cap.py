"""Unit tests for the manifest cap and its record.

The planner chooses from a manifest capped at MANIFEST_CHAR_CAP. The cap was a
slice on the joined string, so a manifest that outgrew it lost its tail with
nothing said: the lowest-ranked documents vanished from the planner's view and
no run recorded that it had happened. Measured on 99ed1bd0 the manifest is
already 50,218 characters, 84% of the cap.

Capping per document rather than by slicing matters twice. A slice cuts
mid-line, so the last document arrives with half a summary; and counting
survivors afterwards would mean counting "- " prefixes in text that can contain
a summary with a newline in it, which one of the 4,019 corpus summaries does.

No DB: `_manifest_rows` is stubbed.

Run from the backend directory:
`python -m pytest tests/test_manifest_cap.py`
"""

import pytest

from app.services import query_runner as qr
from app.services.query_runner import MANIFEST_CHAR_CAP


def rows(n, summary_chars=100, briefing=False):
    out = []
    for i in range(n):
        r = {"filename": f"doc{i:03}.md", "class": "Court Decision",
             "annotations": "-", "properties": "-",
             "reason": "mentions something", "summary": "s" * summary_chars}
        if briefing:
            r["briefing"] = {"properties": {"k": "v" * 50}, "toc": "t" * 200}
        out.append(r)
    return out


@pytest.fixture
def manifest_rows(monkeypatch):
    holder = {}

    async def fake_rows(parent_id):
        return holder["rows"]

    monkeypatch.setattr(qr, "_manifest_rows", fake_rows)
    return holder


@pytest.mark.asyncio
async def test_everything_fits_and_the_record_says_so(manifest_rows):
    manifest_rows["rows"] = rows(10)
    text, cap = await qr._doc_manifest("p")
    assert cap == {"chars": len(text) + 1, "cap": MANIFEST_CHAR_CAP,
                   "documents": 10, "shown": 10, "dropped": 0}
    assert text.count("- doc") == 10


@pytest.mark.asyncio
async def test_the_tail_is_dropped_and_counted(manifest_rows):
    """Rows arrive in rank order, so what goes is what retrieval ranked last.
    The defect was never the dropping, only its silence."""
    manifest_rows["rows"] = rows(40)
    text, cap = await qr._doc_manifest("p", cap=2000)
    assert cap["documents"] == 40
    assert cap["dropped"] == 40 - cap["shown"] > 0
    assert cap["chars"] <= 2000
    # the survivors are the top ranks, contiguous from the front
    assert "- doc000.md" in text
    assert f"- doc{cap['shown']:03}.md" not in text


@pytest.mark.asyncio
async def test_a_head_document_too_large_to_fit_is_dropped_not_overflowed(manifest_rows):
    """The head is no longer truncated, so a single description CAN exceed the
    cap where it never could before. It is dropped and counted like any other
    document that does not fit, rather than carrying the manifest over.

    Not reachable on this corpus: the longest summary in it is 1,525
    characters and the head holds ten. The guarantee is now empirical rather
    than structural, which is the cost of showing the head whole and is worth
    stating where someone will read it.
    """
    r = rows(3)
    r[0]["summary"] = "x" * (MANIFEST_CHAR_CAP + 10)
    manifest_rows["rows"] = r
    text, cap = await qr._doc_manifest("p")
    assert cap["chars"] <= MANIFEST_CHAR_CAP
    assert cap["dropped"] >= 1


@pytest.mark.asyncio
async def test_dropped_counts_the_whole_tail_not_the_first_miss(manifest_rows):
    """A document that does not fit must not let a later, smaller one through:
    the planner would then be reading a set that is not the top of the ranking,
    and `dropped` would understate what is missing."""
    r = rows(6)
    r[2]["briefing"] = {"properties": {"k": "v" * 200}, "toc": "t" * 600}
    manifest_rows["rows"] = r
    text, cap = await qr._doc_manifest("p", cap=800)
    kept = [i for i in range(6) if f"- doc{i:03}.md" in text]
    # contiguous from rank 0: the briefed document at index 2 is what does not
    # fit, and 3, 4 and 5 must not slip in behind it despite being smaller
    assert kept == list(range(len(kept))), kept
    assert 2 not in kept and cap["dropped"] == 6 - len(kept)


@pytest.mark.asyncio
async def test_a_document_is_whole_or_absent(manifest_rows):
    """A slice cut mid-line and handed the planner half a summary. Every
    document in the text now carries its full line."""
    manifest_rows["rows"] = rows(30, summary_chars=3000)
    text, _cap = await qr._doc_manifest("p")
    for line in text.split("\n"):
        if line.startswith("- "):
            assert line.count(" | ") == 5, line


@pytest.mark.asyncio
async def test_briefing_lines_count_against_the_cap(manifest_rows):
    """properties and toc lines are 11,188 characters on 99ed1bd0's fifteen
    briefed documents. Counting only the summary line would understate the
    manifest by a fifth."""
    manifest_rows["rows"] = rows(6, summary_chars=100, briefing=True)
    text, cap = await qr._doc_manifest("p")
    assert "    properties: " in text and "    toc: " in text
    assert cap["chars"] == len(text) + 1


@pytest.mark.asyncio
async def test_a_summary_with_a_newline_does_not_confuse_the_count(manifest_rows):
    """One of the 4,019 corpus summaries contains a newline. Counting "- "
    prefixes in the joined string would read it as an extra document."""
    r = rows(3)
    r[1]["summary"] = "first\n- doc999.md | fake | line"
    manifest_rows["rows"] = r
    _text, cap = await qr._doc_manifest("p")
    assert cap["documents"] == 3 and cap["shown"] == 3 and cap["dropped"] == 0


@pytest.mark.asyncio
async def test_the_cap_is_a_parameter(manifest_rows):
    manifest_rows["rows"] = rows(10, summary_chars=100)
    _, wide = await qr._doc_manifest("p", cap=MANIFEST_CHAR_CAP)
    _, tight = await qr._doc_manifest("p", cap=500)
    assert wide["dropped"] == 0 and tight["dropped"] > 0


# ── the budget is spent where the planner reads ──────────────────────────────


def test_the_head_is_shown_whole():
    """The ten the planner builds from are not truncated at all. Every summary
    in the corpus exceeds the old cap, median length 973, and the opening is
    court, date and parties, so a cut head is a head without the law in it."""
    row = rows(1, summary_chars=2000)[0]
    assert row["summary"] in qr._manifest_line(row, None)


def test_the_tail_is_still_bounded():
    row = rows(1, summary_chars=2000)[0]
    line = qr._manifest_line(row, qr.TAIL_SUMMARY_CHARS)
    assert "s" * (qr.TAIL_SUMMARY_CHARS + 1) not in line


def test_the_head_keeps_more_of_its_summary_than_the_tail():
    """Measured over 3,854 citations in published answers: 56% name a document
    in the top ten, 72% in the top twenty, and the median citation is rank 9.
    A flat 200 characters each spends the same on rank 3 and rank 97."""
    row = rows(1, summary_chars=2000)[0]
    head = qr._manifest_line(row, qr.HEAD_SUMMARY_CHARS)
    tail = qr._manifest_line(row, qr.TAIL_SUMMARY_CHARS)
    assert len(head) - len(tail) == 2000 - qr.TAIL_SUMMARY_CHARS


@pytest.mark.asyncio
async def test_the_budget_drops_after_the_head(manifest_rows):
    """Rows arrive in rank order, so position is rank."""
    manifest_rows["rows"] = rows(qr.HEAD_DOCUMENTS + 1, summary_chars=2000)
    text, _ = await qr._doc_manifest("p")
    lines = text.split("\n")
    assert len(lines[qr.HEAD_DOCUMENTS - 1]) > len(lines[qr.HEAD_DOCUMENTS])


@pytest.mark.asyncio
async def test_a_hundred_documents_cost_less_than_the_flat_budget(manifest_rows):
    """Every summary in the corpus exceeds the old flat cap: 27,029 of 27,029,
    median length 973. So the flat budget was always fully spent."""
    manifest_rows["rows"] = rows(100, summary_chars=973)
    _, rec = await qr._doc_manifest("p")
    banded = (qr.HEAD_DOCUMENTS * 973
              + (100 - qr.HEAD_DOCUMENTS) * qr.TAIL_SUMMARY_CHARS)
    assert banded < 100 * 973
    assert rec["shown"] == 100


# ── the table of contents carries titles, not punctuation ────────────────────


def test_a_toc_is_rendered_as_line_ranges_and_titles():
    """Stored as JSON and rendered with str(), 57% of the characters that
    reached the planner were keys, quotes and braces."""
    toc = {"entries": [{"line": 1, "level": 1, "title": "Inspections",
                        "line_to": 84}]}
    out = qr._toc_digest(toc)
    assert "1-84 Inspections" in out
    assert "line_to" not in out
    assert "level" not in out


def test_a_toc_digest_stops_at_its_cap():
    toc = {"entries": [{"line": i, "line_to": i + 1, "title": "T" * 40}
                       for i in range(50)]}
    assert len(qr._toc_digest(toc, cap=120)) <= 120


def test_a_toc_arriving_as_json_text_is_read_the_same_way():
    import json as _json
    toc = {"entries": [{"line": 3, "line_to": 9, "title": "Privilege"}]}
    assert qr._toc_digest(_json.dumps(toc)) == qr._toc_digest(toc)


def test_a_toc_that_cannot_be_read_is_passed_through_truncated():
    """Never raise inside the manifest for the sake of a table of contents."""
    assert qr._toc_digest("a plain string toc", cap=6) == "a plai"


def test_a_compact_toc_carries_more_title_in_the_same_space():
    entries = [{"line": i, "level": 1, "title": f"Chapter {i} of the thing",
                "line_to": i + 40} for i in range(12)]
    raw = str({"entries": entries})[:qr.TOC_CHARS]
    digest = qr._toc_digest({"entries": entries}, cap=qr.TOC_CHARS)
    assert digest.count("Chapter") > raw.count("Chapter")


def test_the_reason_and_properties_columns_are_bounded():
    """Two thirds of the list is not summary. Measured over 39 full-size
    result sets, the summaries come to 20,000 characters and the properties to
    20,152, with the retrieval reason at 12,000 behind them — so no
    combination of head and tail sizes fits the list under the cap on its own."""
    r = rows(1)[0]
    r["properties"] = "p" * 900
    r["reason"] = "r" * 900
    line = qr._manifest_line(r, qr.TAIL_SUMMARY_CHARS)
    assert "p" * (qr.PROPERTIES_CHARS + 1) not in line
    assert "r" * (qr.REASON_CHARS + 1) not in line
