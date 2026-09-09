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
    assert cap == {"chars": len(text), "cap": MANIFEST_CHAR_CAP,
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
    """`_manifest_line` truncates the summary to 200 characters before the cap
    ever sees it, so no single document is large enough to matter. That is why
    the cap has not bitten yet, and why it starts mattering the moment the
    summary shown to the planner gets longer."""
    r = rows(3)
    r[1]["summary"] = "x" * (MANIFEST_CHAR_CAP + 10)
    manifest_rows["rows"] = r
    text, cap = await qr._doc_manifest("p")
    assert cap["dropped"] == 0 and cap["shown"] == 3
    assert "- doc001.md" in text
    assert "x" * 201 not in text


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


@pytest.mark.asyncio
async def test_the_recorded_length_is_the_length_that_is_sent(manifest_rows):
    """`chars` is the manifest, not the manifest plus a newline nobody writes.
    Counting one per line charged a separator after the last one, which put the
    figure one above the real length and made the effective cap 59,999."""
    for n in (1, 2, 7):
        manifest_rows["rows"] = rows(n, briefing=True)
        text, cap = await qr._doc_manifest("p")
        assert cap["chars"] == len(text), f"{n} documents"

