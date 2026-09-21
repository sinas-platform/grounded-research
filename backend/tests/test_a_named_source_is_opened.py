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

WHY IT MATTERED. A review of published answers named this failure six times over, and
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


@pytest.mark.asyncio
async def test_a_cited_source_is_asked_what_else_it_carries(monkeypatch):
    """The reviewer's findings are mostly 'thin', not 'wrong'.

    T-125/03 was retrieved, cited three times by the answer, and paragraph 123
    of it states the rule the reviewer asked for. No claim said it, and
    nothing in the run ever asked that document about that part of the
    question — extraction reads per planned claim from that claim's anchors,
    and the plan is written before any document is read. The gate cannot
    catch it either: it names sources the answer did NOT use.
    """
    from app.services import query_runner as qr

    asked: list[str] = []

    class _Sinas:
        async def invoke(self, agent: str, prompt: str) -> str:
            asked.append(prompt)
            return ('{"found": true, "line_from": 123, "line_to": 123, '
                    '"quote": "Preparatory documents drawn up exclusively for '
                    'the purpose of seeking legal advice may be covered."}')

    _stub_class_documents(monkeypatch, [
        ("a-judgment.md", "x" * 500, 10),
        ("a-commentary.md", "y" * 500, 40),
    ])
    found = await qr._look_deeper(
        _Sinas(), ["a-judgment.md", "a-commentary.md"],
        [{"asks": "whether preparatory documents are protected"}])

    # only the top declared rank is opened — a commentary re-read yields
    # more commentary
    assert [d["doc"] for d in found] == ["a-judgment.md"]
    assert "whether preparatory documents are protected" in asked[0]
    assert "already cites the document below" in asked[0]


@pytest.mark.asyncio
async def test_the_deeper_look_is_bounded(monkeypatch):
    """A cycle that opened everything would be a second retrieval pass."""
    from app.services import query_runner as qr

    calls: list[str] = []

    class _Sinas:
        async def invoke(self, _agent: str, _prompt: str) -> str:
            calls.append("x")
            return ('{"found": true, "line_from": 1, "line_to": 2, '
                    '"quote": "Something the document says about it."}')

    _stub_class_documents(monkeypatch,
                          [(f"j{i}.md", "x" * 500, 10) for i in range(6)])
    parts = [{"asks": f"part {i}"} for i in range(6)]
    found = await qr._look_deeper(_Sinas(), [f"j{i}.md" for i in range(6)], parts)
    assert len(calls) <= qr.MAX_DEEPER_LOOKS
    assert len(found) <= qr.MAX_DEEPER_LOOKS


@pytest.mark.asyncio
async def test_a_deployment_that_declares_no_standing_is_not_guessed_at(monkeypatch):
    """Absence is announced, never guessed — as everywhere else."""
    from app.services import query_runner as qr

    class _Sinas:
        async def invoke(self, _agent: str, _prompt: str) -> str:
            raise AssertionError("must not be called")

    _stub_class_documents(monkeypatch, [("a.md", "x" * 500, None)])
    assert await qr._look_deeper(_Sinas(), ["a.md"], [{"asks": "anything"}]) == []


def _stub_class_documents(monkeypatch, rows) -> None:
    """(filename, content, class standing) for the deeper look's one read."""
    from contextlib import asynccontextmanager

    from app.services import query_runner as qr

    class _Session:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: list(rows))

    @asynccontextmanager
    async def _session_local():
        yield _Session()

    monkeypatch.setattr(qr, "AsyncSessionLocal", _session_local)
