"""A malformed drafting reply gets one retry, not a dead run.

Drafting is the last call in a run that has already paid for retrieval,
planning and extraction. One unescaped quote inside a claim threw all of it
away: json.loads raised, nothing caught it, and the run ended `failed` with
"Expecting ',' delimiter: line 1 column 9424".

The risk grew when claims started carrying a rationale — longer replies,
more prose, more chances for a stray quote. So the parse is retried once with
the error handed back, and a second failure still raises: a run that cannot
draft must say so.

Run from the backend directory: `python -m pytest tests/test_draft_json_retry.py`
"""

from __future__ import annotations

import json

import pytest

from app.services.query_runner import _claims_json

GOOD = '{"claims": [{"text": "A claim.", "rationale": "Because.", "evidence": []}]}'


def test_a_clean_reply_parses():
    assert _claims_json(GOOD)["claims"][0]["text"] == "A claim."


def test_fenced_and_prefixed_replies_parse():
    for wrapped in (f"```json\n{GOOD}\n```", f"json {GOOD}",
                    f"Here you go:\n{GOOD}\nLet me know."):
        assert _claims_json(wrapped)["claims"], wrapped[:30]


def test_a_malformed_reply_raises_a_decode_error_naming_the_position():
    """The caller retries on JSONDecodeError specifically, so a broken reply
    must not surface as some other exception type."""
    broken = '{"claims": [{"text": "He said "yes" to it.", "evidence": []}]}'
    with pytest.raises(json.JSONDecodeError):
        _claims_json(broken)


def test_a_reply_with_no_object_at_all_raises_the_same_error():
    for empty in ("", "I cannot draft this.", "```\n```"):
        with pytest.raises(json.JSONDecodeError):
            _claims_json(empty)


def test_the_drafter_retries_once_and_then_gives_up():
    import inspect

    from app.services import query_runner as qr

    src = inspect.getsource(qr._draft_from_extracts)
    body = src[src.index("    try:\n        data = _claims_json(reply)"):]
    # the retry hands the model the error it made
    assert "was not valid JSON" in body
    # and the second parse is unguarded, so a second failure ends the run
    second = body[body.index("PREVIOUS REPLY"):]
    assert "data = _claims_json(reply)" in second
    assert "except" not in second.split("claims = data.get")[0]
