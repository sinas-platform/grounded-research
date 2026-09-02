"""Unit tests for reading the answer gate's verdict.

The gate decides whether an answer is publishable, and it is the only stage
that asks whether the claims address the question rather than whether one
claim matches one passage. So the two ways its reply can be unusable, text
that is not JSON and JSON that is not an object, both have to arrive as one
exception type: the caller repairs once and then stops the run, and it can
only do that if it has a single thing to catch.

Pure: the function takes a string and returns a dict or raises, so no DB and
no network.

Run from the backend directory:
`python -m pytest tests/test_gate_verdict_parse.py`
"""

import json

import pytest

from app.services.query_runner import _gate_json

VERDICT = (
    '{"publishable": true, "parts": [{"asks": "whether it applies", '
    '"covered": true, "gap": ""}], "missing": "", "unused_sources": []}'
)


# -- replies that are a verdict ----------------------------------------------


def test_a_plain_object_is_read():
    assert _gate_json(VERDICT)["publishable"] is True


def test_the_parts_survive_the_read():
    parts = _gate_json(VERDICT)["parts"]
    assert len(parts) == 1
    assert parts[0]["covered"] is True


def test_a_fenced_reply_is_read():
    assert _gate_json("```json\n" + VERDICT + "\n```")["publishable"] is True


def test_prose_around_the_object_is_ignored():
    """The model sometimes explains itself first. The object is still there."""
    assert _gate_json("Here is my verdict:\n" + VERDICT + "\nHope that helps.")


def test_whitespace_only_padding_is_ignored():
    assert _gate_json("\n\n  " + VERDICT + "  \n")["publishable"] is True


# -- replies that are not a verdict ------------------------------------------


def test_malformed_json_raises():
    with pytest.raises(ValueError):
        _gate_json('{"publishable": true, "parts": [}')


def test_a_json_array_is_not_a_verdict():
    """It parses. It is not an object, so it carries no publishable and no
    parts, and letting it through would be the silent pass again."""
    with pytest.raises(ValueError):
        _gate_json('[{"asks": "x", "covered": true}]')


def test_a_bare_string_is_not_a_verdict():
    with pytest.raises(ValueError):
        _gate_json('"the answer looks fine to me"')


def test_prose_with_no_object_raises():
    with pytest.raises(ValueError):
        _gate_json("The answer covers every part of the question.")


def test_an_empty_reply_raises():
    with pytest.raises(ValueError):
        _gate_json("")


def test_a_missing_reply_raises():
    with pytest.raises(ValueError):
        _gate_json(None)


def test_an_opening_brace_with_no_close_raises():
    with pytest.raises(ValueError):
        _gate_json('{"publishable": true, "parts": [')


# -- the contract the caller depends on --------------------------------------


def test_both_failures_are_one_exception_type():
    """The caller catches ValueError alone. A JSONDecodeError is one, and the
    not-an-object check raises one, so a single except covers both and
    neither can slip past as an unhandled error."""
    assert issubclass(json.JSONDecodeError, ValueError)
    for bad in ('{"a": }', "[1, 2]", "no object here", ""):
        with pytest.raises(ValueError):
            _gate_json(bad)


def test_the_error_says_what_broke():
    """The message is recorded as telemetry and read later by a person."""
    with pytest.raises(ValueError) as e:
        _gate_json("nothing here")
    assert str(e.value).strip()

    with pytest.raises(ValueError) as e:
        _gate_json('["not", "an", "object"]')
    assert "object" in str(e.value)
