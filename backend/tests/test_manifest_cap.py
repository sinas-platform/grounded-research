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
             "annotations": "-", "properties": "-", "rank": i,
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
    assert cap == {"chars": len(text), "cap": MANIFEST_CHAR_CAP,
                   "unranked": 0,
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
async def test_one_document_cannot_blow_the_cap_today(manifest_rows):
    """`_manifest_line` truncates the summary before the cap ever sees it, so
    no single document is large enough to matter. The bound is now the band's
    budget rather than a flat 200, and it is still far below the cap."""
    r = rows(3)
    r[1]["summary"] = "x" * (MANIFEST_CHAR_CAP + 10)
    manifest_rows["rows"] = r
    text, cap = await qr._doc_manifest("p")
    assert cap["dropped"] == 0 and cap["shown"] == 3
    assert "- doc001.md" in text
    assert "x" * (qr.HEAD_SUMMARY_CHARS + 1) not in text


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
    assert cap["chars"] == len(text)


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


def test_the_head_keeps_more_of_its_summary_than_the_tail():
    """Measured over 3,854 citations in published answers: 56% name a document
    in the top ten, 72% in the top twenty, and the median citation is rank 9.
    A flat 200 characters each spends the same on rank 3 and rank 97."""
    row = rows(1, summary_chars=2000)[0]
    head = qr._manifest_line(row, qr.HEAD_SUMMARY_CHARS)
    tail = qr._manifest_line(row, qr.TAIL_SUMMARY_CHARS)
    assert len(head) - len(tail) == qr.HEAD_SUMMARY_CHARS - qr.TAIL_SUMMARY_CHARS


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
    banded = (qr.HEAD_DOCUMENTS * qr.HEAD_SUMMARY_CHARS
              + (100 - qr.HEAD_DOCUMENTS) * qr.TAIL_SUMMARY_CHARS)
    assert banded < 100 * 200
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

@pytest.mark.asyncio
async def test_an_unranked_result_is_not_banded(manifest_rows):
    """A curated result can carry rows with no rank at all -- merged in,
    reached through the graph, or attached by hand. One result in the corpus
    has 178 of 178 unranked, and four runs have used such a result as their
    parent. Ordering by a null column leaves the sequence arbitrary, so the
    head band would be spent on whichever ten arrived first.
    """
    built = rows(qr.HEAD_DOCUMENTS + 5, summary_chars=2000)
    for r in built:
        r["rank"] = None
    manifest_rows["rows"] = built
    text, rec = await qr._doc_manifest("p")
    lines = [l for l in text.split("\n") if l.startswith("- ")]
    assert len({len(l) for l in lines}) == 1, "every line the same budget"
    assert rec["unranked"] == len(built)


@pytest.mark.asyncio
async def test_a_ranked_result_is_still_banded(manifest_rows):
    built = rows(qr.HEAD_DOCUMENTS + 5, summary_chars=2000)
    for i, r in enumerate(built):
        r["rank"] = i
    manifest_rows["rows"] = built
    text, rec = await qr._doc_manifest("p")
    lines = [l for l in text.split("\n") if l.startswith("- ")]
    assert len(lines[0]) > len(lines[-1]), "the head keeps more than the tail"
    assert rec["unranked"] == 0


def test_a_mapping_of_toc_entries_still_renders():
    """Iterating a dict yields its keys, and the digest drops non-dictionaries,
    so a mapping would render as nothing. Not present in this corpus; pinned so
    it cannot start being true quietly."""
    as_list = qr._toc_digest({"entries": [{"line": 1, "line_to": 8, "title": "One"}]})
    as_map = qr._toc_digest({"entries": {"a": {"line": 1, "line_to": 8, "title": "One"}}})
    assert as_list == as_map == "1-8 One"


@pytest.mark.asyncio
async def test_one_unranked_document_does_not_cost_the_others_their_budget(manifest_rows):
    """Deciding per result rather than per row would let a single attached
    document strip the head band from every ranked one beside it. No result in
    this corpus mixes the two; merge and graph expansion are how one would."""
    built = rows(qr.HEAD_DOCUMENTS + 5, summary_chars=2000)
    for i, r in enumerate(built):
        r["rank"] = i
    built[-1]["rank"] = None
    manifest_rows["rows"] = built
    text, rec = await qr._doc_manifest("p")
    lines = [l for l in text.split("\n") if l.startswith("- ")]
    assert len(lines[0]) > len(lines[-1]), "the ranked head still keeps more"
    assert rec["unranked"] == 1


@pytest.mark.asyncio
async def test_the_recorded_length_is_the_length_that_is_sent(manifest_rows):
    """`chars` is the manifest, not the manifest plus a newline nobody writes.
    Counting one per line charged a separator after the last one, which put the
    figure one above the real length and made the effective cap 59,999."""
    for n in (1, 2, 7):
        manifest_rows["rows"] = rows(n, briefing=True)
        text, cap = await qr._doc_manifest("p")
        assert cap["chars"] == len(text), f"{n} documents"

