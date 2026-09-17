"""A source the review names as unused is opened before it is asked for.

THE FAILURE. The gate reads the whole retrieved set and names documents the
answer should have used. Each becomes an obligation put to the drafter: cite
it, waive it "with a rationale you can only give after reading its passages",
or refuse it with a reason. Nothing opened the document.

Extraction reads per planned claim, from that claim's own anchors. A document
the plan never pointed at therefore has no extracted passages at all — so a
drafter told to cite one has nothing verbatim to quote and can only refuse,
however apt the document is. The refusal then looks like a considered
judgement and is an artefact of never having seen the text.

WHY IT MATTERED. An expert reviewer named this failure six times over, and
each time identified the missing material by its RANK in the retrieved set:
"sources at ranks 11 and 31, unused"; "T-125/03, rank 33"; "ranks 26, 41, 51".
The documents were retrieved every time. They were never read.

The standing check has always opened its documents before objecting, for
exactly this reason. This is the same look, pointed at the third case.
"""

from types import SimpleNamespace

import pytest

from app.services.reread import Cited, owed_prompt


def test_the_ask_carries_the_point_and_the_whole_document():
    """One document, one point, the text in full — not a window."""
    prompt = owed_prompt(
        "It states the rule on internal documents drawn up to seek advice.",
        Cited(filename="a-judgment.md", text="LINE ONE\nLINE TWO"))
    assert "It states the rule on internal documents" in prompt
    assert "LINE ONE\nLINE TWO" in prompt
    assert "a-judgment.md" in prompt
    # the shared look's guarantees, not restated per caller
    assert "verbatim" in prompt
    assert '"found"' in prompt


@pytest.mark.asyncio
async def test_a_named_document_is_opened_and_its_passage_comes_back(monkeypatch):
    """The point of the whole change: the drafter is handed the text."""
    from app.services import query_runner as qr

    asked: list[str] = []

    class _Sinas:
        async def invoke(self, agent: str, prompt: str) -> str:
            asked.append(agent)
            return ('{"found": true, "line_from": 12, "line_to": 14, '
                    '"quote": "The protection extends to documents drawn up '
                    'exclusively to seek external advice."}')

    _stub_documents(monkeypatch, {"a-judgment.md": "x" * 500})
    found = await qr._look_owed(
        _Sinas(), [{"doc": "a-judgment.md", "note": "It states the rule."}])

    assert asked == ["sgr/passage-extractor-agent"]
    assert found["a-judgment.md"]["quote"].startswith("The protection extends")
    assert found["a-judgment.md"]["line_from"] == 12


@pytest.mark.asyncio
async def test_a_document_that_carries_nothing_is_not_a_hit(monkeypatch):
    """Silence is an answer, and an informed refusal is the right outcome."""
    from app.services import query_runner as qr

    class _Sinas:
        async def invoke(self, _agent: str, _prompt: str) -> str:
            return '{"found": false, "line_from": 0, "line_to": 0, "quote": ""}'

    _stub_documents(monkeypatch, {"a-judgment.md": "x" * 500})
    found = await qr._look_owed(
        _Sinas(), [{"doc": "a-judgment.md", "note": "It states the rule."}])
    assert found == {}


@pytest.mark.asyncio
async def test_a_document_with_no_stored_text_costs_no_call(monkeypatch):
    """Nothing to read is not something to pay a model to read."""
    from app.services import query_runner as qr

    calls: list[str] = []

    class _Sinas:
        async def invoke(self, agent: str, _prompt: str) -> str:
            calls.append(agent)
            return "{}"

    _stub_documents(monkeypatch, {"a-judgment.md": ""})
    found = await qr._look_owed(
        _Sinas(), [{"doc": "a-judgment.md", "note": "It states the rule."}])
    assert found == {} and calls == []


@pytest.mark.asyncio
async def test_nothing_owed_reads_nothing(monkeypatch):
    from app.services import query_runner as qr

    class _Sinas:
        async def invoke(self, _agent: str, _prompt: str) -> str:
            raise AssertionError("must not be called")

    assert await qr._look_owed(_Sinas(), []) == {}


def _stub_documents(monkeypatch, texts: dict[str, str]) -> None:
    """The document/version read the look makes, and nothing else."""
    from contextlib import asynccontextmanager

    from app.services import query_runner as qr

    rows = list(texts.items())

    class _Session:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
