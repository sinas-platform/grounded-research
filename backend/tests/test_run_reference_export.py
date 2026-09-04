"""Unit tests for run identity (reference/tags) and the sgr-review/1 export.

The export's field paths are a contract external consumers configure
against, so the shape tests here are load-bearing: a rename that slips
through them breaks anchored feedback and regression checks elsewhere.

Pure where possible: `select_runs`' latest-per-reference reduction and the
passage slicing take plain values. Route-shape assertions read the router,
not a server.
"""

import datetime as dt
import types
import uuid

from app.api.v1.query_runs import router
from app.services.run_export import SCHEMA, _passage_text


def _run(reference=None, completed=True, created=0):
    r = types.SimpleNamespace()
    r.id = uuid.uuid4()
    r.reference = reference
    r.completed_at = dt.datetime(2026, 1, 1) if completed else None
    r.created_at = dt.datetime(2026, 1, 1) + dt.timedelta(minutes=created)
    return r


def _reduce(runs):
    """The latest-per-reference reduction, as select_runs applies it to a
    newest-first list."""
    picked, loose = {}, []
    for r in sorted(runs, key=lambda x: x.created_at, reverse=True):
        if not r.reference:
            loose.append(r)
        elif r.reference not in picked and r.completed_at is not None:
            picked[r.reference] = r
    return list(picked.values()) + loose


def test_schema_string_is_versioned():
    assert SCHEMA == "sgr-review/1"


def test_latest_per_reference_keeps_newest_completed():
    old = _run("q16", created=0)
    new = _run("q16", created=10)
    unfinished = _run("q16", completed=False, created=20)
    kept = _reduce([old, new, unfinished])
    assert kept == [new]  # newest COMPLETED wins; the running one never


def test_runs_without_reference_are_kept_as_themselves():
    a, b = _run(None), _run(None, created=5)
    assert len(_reduce([a, b])) == 2


def test_passage_is_the_verbatim_lines_of_the_span():
    content = "one\ntwo\nthree\nfour"
    assert _passage_text(content, {"line_from": 2, "line_to": 3}) == "two\nthree"


def test_passage_of_nothing_is_empty_not_an_error():
    assert _passage_text(None, {"line_from": 1}) == ""
    assert _passage_text("text", None) == ""


def test_export_routes_precede_the_run_id_catchall():
    """GET /{run_id} would swallow /export/by-tag as a malformed UUID if it
    registered first — the order is behavior, so it is pinned."""
    paths = [r.path for r in router.routes]
    assert paths.index("/query-runs/export/by-tag") < paths.index("/query-runs/{run_id}")
    assert paths.index("/query-runs/export") < paths.index("/query-runs/{run_id}")


# -- title must be reachable through the API -----------------------------------
#
# The column, the model field and the export line were added together and the
# API was not, so a title could only be set by writing to the database. That is
# how the first round-3 export was produced, and it is not something the
# product should require: `PATCH /query-runs/{id}` is the path a re-export of
# an earlier round goes through.


def api_source() -> str:
    import pathlib

    from app.api.v1 import query_runs

    return pathlib.Path(query_runs.__file__).read_text(encoding="utf-8")


def test_a_run_can_be_born_with_a_title():
    from app.api.v1.query_runs import QueryRunIn

    payload = QueryRunIn(
        question="a question long enough",
        reference="benchmark-q16",
        title="Q16 — Dawn raids — personal data",
    )
    assert payload.title == "Q16 — Dawn raids — personal data"


def test_a_title_can_be_stamped_after_the_fact():
    from app.api.v1.query_runs import QueryRunMetaIn

    fields = QueryRunMetaIn(title="Q41 — Dawn raids — electronic data").model_dump(
        exclude_unset=True
    )
    assert fields == {
        "title": "Q41 — Dawn raids — electronic data"
    }, "only what was sent: an omitted field must be left alone, not cleared"


def test_an_omitted_title_is_left_alone_and_an_explicit_null_clears_it():
    from app.api.v1.query_runs import QueryRunMetaIn

    assert "title" not in QueryRunMetaIn(reference="x").model_dump(exclude_unset=True)
    assert QueryRunMetaIn(title=None).model_dump(exclude_unset=True) == {"title": None}


def test_the_patch_handler_assigns_it():
    src = api_source()
    assert 'if "title" in fields:' in src
    assert 'run.title = (fields["title"] or "").strip()[:300] or None' in src


def test_a_title_can_be_read_back():
    """`QueryRunOut` is how a caller confirms a stamp landed. Without the field
    a title set through the API was invisible through it."""
    from app.api.v1.query_runs import QueryRunOut

    assert "title" in QueryRunOut.model_fields


def test_the_create_handler_passes_it_through():
    assert 'title=((payload.title or "").strip()[:300] or None),' in api_source()


def test_title_is_bounded_like_the_column():
    """varchar(300). A schema that accepts more than the column stores turns a
    long title into a database error instead of a validation one."""
    import pytest
    from app.api.v1.query_runs import QueryRunIn, QueryRunMetaIn
    from pydantic import ValidationError

    for model, kw in ((QueryRunIn, {"question": "a question long enough"}), (QueryRunMetaIn, {})):
        model(title="x" * 300, **kw)
        with pytest.raises(ValidationError):
            model(title="x" * 301, **kw)
