"""The assembled answer is served, and the publish path stores it.

Two halves of one contract. `GET /answers/{id}/markdown` re-assembles from
the rows on every call — so an answer whose claims changed after publication
reads as it now is — and hands back the `[n]` → document mapping beside the
text, which is what lets a consumer keep its own bibliography style without
renumbering. An answer with nothing to render is a 404 and not an empty
document, because an empty document reads as an answer.

The other half is the publish path: the text is written onto the row once
the claim numbering is final, and a failure to render must not unpublish an
answer whose rows are complete. That half is asserted over the source, the
way the run pipeline's other structural rules are — it is a transaction
against a live database and the shape is what the test is about.

Run from the backend directory:
`python -m pytest tests/test_the_answer_is_served_as_markdown.py`
"""

import inspect
import uuid
from types import SimpleNamespace

import pytest
from app.api.v1.answers import get_answer_markdown
from app.services import answer_render
from app.services import query_runner as qr
from fastapi import HTTPException


class _FakeCaller:
    def __init__(self):
        self.user_id = uuid.uuid4()
        self.roles = []
        self.is_admin = True

    async def has_permission(self, permission):
        return True


class _ExecResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._results.pop(0)


class _Answer:
    def __init__(self, rendered_markdown=None, status="draft"):
        self.id = uuid.uuid4()
        self.question = "Does it apply?"
        self.question_parts = None
        self.law_stated_as_at = None
        self.rendered_markdown = rendered_markdown
        # Draft by default: these tests are about rendering, and the
        # published path carries a validation check of its own.
        self.status = status


RENDERED = answer_render.render_markdown(
    {"question": "Does it apply?"},
    [{"id": "c1", "sequence": 1, "section": "conclusion", "part_index": None,
      "position": 1, "claim_text": "It applies."}],
    [{"claim_id": "c1", "document_id": "d1", "span": {"paragraph_ref": "42"}}],
    {"d1": {"title": "Kestrel Holdings v Northmoor Authority",
            "identifier": "T-100/20"}},
)


async def _call(session, monkeypatch, rendered=RENDERED):
    async def _assemble(_session, _answer_id, fallback_as_at=None):
        return rendered

    monkeypatch.setattr(answer_render, "assemble", _assemble)
    return await get_answer_markdown(
        uuid.uuid4(), session=session, caller=_FakeCaller())


@pytest.mark.asyncio
async def test_the_text_and_the_marker_mapping_come_back_together(monkeypatch):
    """A consumer that renumbers citations into its own style needs to know
    what each marker pointed at. Returning the text alone makes that a
    guess."""
    session = _FakeSession([_ExecResult(scalar=_Answer()),
                            _ExecResult(scalar=uuid.uuid4())])
    out = await _call(session, monkeypatch)
    assert "It applies.[1]" in out.markdown
    assert "Kestrel Holdings v Northmoor Authority (T-100/20), para. 42" in out.markdown
    assert [(c.n, c.document_id) for c in out.citations] == [(1, "d1")]


@pytest.mark.asyncio
async def test_an_answer_with_no_claims_and_no_stored_text_is_a_404(monkeypatch):
    """An answer that was never drafted has no markdown. Serving the question
    and an empty Authorities list would read as an answer that says nothing,
    which is a different and worse thing to publish."""
    session = _FakeSession([_ExecResult(scalar=_Answer()),
                            _ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await _call(session, monkeypatch)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_stored_text_is_served_even_after_its_claims_are_gone(monkeypatch):
    """The stored text is what the answer said when it published. An answer
    whose claims were later removed still has that, and must not 404."""
    session = _FakeSession([_ExecResult(scalar=_Answer(rendered_markdown="# Q")),
                            _ExecResult(scalar=None)])
    out = await _call(session, monkeypatch)
    assert out.markdown


@pytest.mark.asyncio
async def test_an_invisible_answer_is_a_404_before_anything_is_rendered(monkeypatch):
    """Visibility first. Rendering an answer the caller cannot read and then
    refusing to serve it would still have read its claims."""
    session = _FakeSession([_ExecResult(scalar=None)])
    with pytest.raises(HTTPException) as exc:
        await _call(session, monkeypatch)
    assert exc.value.status_code == 404
    assert len(session.statements) == 1


# ── the publish path ─────────────────────────────────────────────────────────

def _publish_src() -> str:
    return inspect.getsource(qr._publish_answer)


def test_the_text_is_rendered_after_the_sequences_are_compacted():
    """Markers and claim numbering both come off the rows. Rendering before
    compaction stores prose built on numbers the answer no longer has."""
    src = _publish_src()
    assert (src.index("_compact_claim_sequences")
            < src.index("answer_render.assemble"))


def test_the_row_stores_the_text_and_the_date_it_states():
    src = _publish_src()
    assert "row.rendered_markdown = rendered.markdown" in src
    assert "row.law_stated_as_at = rendered.law_stated_as_at" in src


def test_the_run_date_is_the_fallback_when_no_source_carries_one():
    """An answer must say how current it is even over a corpus that dates
    nothing. The read endpoint passes no fallback: a reader must not be
    handed a date nothing in the answer supports."""
    assert "fallback_as_at=_now().date()" in _publish_src()
    src = inspect.getsource(answer_render.assemble)
    assert "fallback_as_at: date | None = None" in src


def test_a_failure_to_render_does_not_unpublish_the_answer():
    """The rows are the record and the text is regenerable. Losing a publish
    to a rendering bug loses the run."""
    src = _publish_src()
    assert "except Exception" in src
    assert "render_error" in src
    # The publish commit happens before the render is attempted, so a
    # rollback cannot take it with it.
    assert src.index('row.status = "published"') < src.index("except Exception")


def test_a_judgment_in_joined_cases_cites_every_case_number():
    """A published citation never shows Python's notation for a list.

    A judgment given in joined cases carries every case number it decides, and
    the declared property holds all of them. Handed to `str`, that printed
    `['T-289/11', 'T-290/11', 'T-521/11']` — brackets, quotes and all — into
    the authorities of an answer that went to a reviewer.
    """
    from app.services.answer_render import citation

    said = citation({"title": "Kestrel Holdings and Others v the authority",
                     "identifier": ["T-289/11", "T-290/11", "T-521/11"],
                     "date": "2013-09-06"})
    assert "T-289/11, T-290/11, T-521/11" in said
    for ch in ("[", "]", "'"):
        assert ch not in said


def test_one_identifier_is_unchanged_by_that():
    from app.services.answer_render import citation

    said = citation({"title": "Kestrel Holdings v the authority",
                     "identifier": "T-289/11", "date": "2013-09-06"})
    assert "(T-289/11, 2013-09-06)" in said


@pytest.mark.asyncio
async def test_an_answer_edited_after_publication_is_not_served_as_published(
        monkeypatch):
    """A published answer is still writable, and that is the hole.

    Changing a claim resets its evidence to unvalidated and binding new
    evidence adds another unvalidated row; neither clears `published`. This
    endpoint reassembles from the claims as they NOW stand, so the edited
    answer would go out as published prose that nothing had checked — the one
    thing publication is supposed to mean.
    """
    from app.api.v1 import answers as mod
    from fastapi import HTTPException

    class _Result:
        def __init__(self, value):
            self._v = value

        def scalar_one_or_none(self):
            return self._v

    class _Session:
        """A published answer with one claim whose evidence is unvalidated."""

        def __init__(self):
            self.calls = 0

        async def execute(self, *_a, **_k):
            self.calls += 1
            return _Result("a-claim-id" if self.calls == 1 else "unvalidated-row")

    async def _row(*_a, **_k):
        return SimpleNamespace(status="published", rendered_markdown="# stale")

    # via monkeypatch: a bare assignment here leaks into every later test
    # that calls this function, which is how three unrelated visibility
    # tests started failing only when the suite ran as a whole.
    monkeypatch.setattr(mod, "_visible_answer_or_404", _row)
    with pytest.raises(HTTPException) as exc:
        await mod.get_answer_markdown(
            answer_id=uuid.uuid4(), session=_Session(), caller=None)
    assert exc.value.status_code == 409
    assert "re-validated" in str(exc.value.detail)
