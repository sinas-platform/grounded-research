"""A whole-document look reports the lines its quote is actually on.

The look sends a document whole and unnumbered and asks for the lines of the
passage it finds, so the numbers that come back are guessed. On one run every
reported range pointed at unrelated text hundreds of lines from a quote that
was itself verbatim, and the feed told the reviser to cite those lines.
`_placed` finds the quote in the stored document and replaces the span, and a
quote that is not in the document is not a hit.

Run from the backend directory:
`python -m pytest tests/test_a_look_cites_the_lines_its_quote_is_on.py`
"""

import pytest

from app.services import query_runner as qr

DOC = "\n".join([
    "Heading",                                   # 1
    "Some opening text that says nothing.",      # 2
    "A harbour pass admits its holder",          # 3
    "to the quay at any hour.",                  # 4
    "Filler " * 30,                              # 5
    "The fee is charged by the day.",            # 6
])


def hit(quote, lf=900, lt=905):
    return {"filename": "a.md", "line_from": lf, "line_to": lt, "quote": quote}


def test_a_guessed_span_is_replaced_by_where_the_quote_is():
    placed = qr._placed(hit("The fee is charged by the day."), DOC)
    assert (placed["line_from"], placed["line_to"]) == (6, 6)
    assert placed["quote"] == "The fee is charged by the day."
    assert placed["filename"] == "a.md"


def test_a_quote_across_lines_gets_the_whole_span():
    placed = qr._placed(hit("A harbour pass admits its holder to the quay at any hour."), DOC)
    assert (placed["line_from"], placed["line_to"]) == (3, 4)


def test_a_quote_that_is_not_in_the_document_is_not_a_hit():
    assert qr._placed(hit("A harbour pass admits nobody after dark."), DOC) is None


def test_nothing_to_place_is_nothing():
    assert qr._placed(None, DOC) is None
    assert qr._placed(hit("The fee is charged by the day."), "") is None


def test_a_long_quote_is_placed_on_what_verification_guarantees():
    """Verification matches on the first 200 characters, so a quote whose
    opening is verbatim still places, on its opening. What comes back is
    the document's text for that opening, never the unchecked tail."""
    opening = ("Filler " * 30).strip()
    long_quote = opening + " and then several things the document never said " * 5
    placed = qr._placed(hit(long_quote), DOC)
    assert placed is not None and placed["line_from"] == 5
    assert "never said" not in placed["quote"]
    assert placed["quote"] in DOC


def test_the_quote_returned_is_the_documents_own_text():
    placed = qr._placed(hit("A harbour pass admits its holder to the quay at any hour."), DOC)
    assert placed["quote"] == "A harbour pass admits its holder\nto the quay at any hour."


def test_a_quote_under_the_evidence_floor_is_not_placed():
    """The evidence check does not accept a quote under 20 characters, so the
    reviser could not cite one, and one that short cannot be placed without
    ambiguity."""
    assert qr._placed(hit("Heading"), DOC) is None


@pytest.mark.asyncio
async def test_a_look_whose_quote_is_not_there_returns_nothing():
    class _Sinas:
        async def invoke(self, agent, prompt):
            return ('{"found": true, "line_from": 2, "line_to": 2, '
                    '"quote": "A harbour pass admits nobody after dark."}')

    assert await qr._ask_document(_Sinas(), "prompt", "a.md", DOC) is None


@pytest.mark.asyncio
async def test_a_look_returns_the_real_lines():
    class _Sinas:
        async def invoke(self, agent, prompt):
            return ('{"found": true, "line_from": 40, "line_to": 41, '
                    '"quote": "A harbour pass admits its holder to the quay at any hour."}')

    got = await qr._ask_document(_Sinas(), "prompt", "a.md", DOC)
    assert (got["line_from"], got["line_to"]) == (3, 4)


@pytest.mark.asyncio
async def test_the_owed_look_feeds_the_real_lines(monkeypatch):
    """End to end through the owed-source look: the model's span is replaced
    before anything downstream sees it."""
    class _Sinas:
        async def invoke(self, agent, prompt):
            return ('{"found": true, "line_from": 900, "line_to": 903, '
                    '"quote": "The fee is charged by the day."}')

    rows = [("a.md", DOC)]

    class _Result:
        def all(self):
            return rows

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
    found = await qr._look_owed(_Sinas(), [{"doc": "a.md", "note": "the fee"}])
    assert (found["a.md"]["line_from"], found["a.md"]["line_to"]) == (6, 6)
