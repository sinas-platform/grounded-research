"""A theme the material carries and the question does not ask.

The splitter reads the question alone, which is what keeps its output stable
across the cycles of a run. Before #105 it read the draft too: the split moved
between cycles, a part stopped being listed and so stopped being checked, and
once a fifth part appeared that the question never asked and the draft happened
to contain.

Reading the working set would recover the limbs a lawyer supplies from knowing
the area, on questions whose wording does not name them. Measured on the runs
held on 10 September: 1 question of 19 splits into a single part restating
itself, and 31 answers split into two, three or four parts correctly. Moving
the splitter would help the one and put the thirty-one at the mercy of what
retrieval returned, and inventing a part from the material is the defect #105
removed.

So the material is read and the split is not touched. What comes back is an
observation: not checked, not coverable, and unable to hold an answer back.

Run from the backend directory:
`python -m pytest tests/test_a_theme_no_part_covers.py`
"""

from __future__ import annotations

from app.services.query_runner import _themes_from_reply


def test_a_clean_reply_gives_themes():
    got = _themes_from_reply('{"themes": ["interim relief under Articles 278 and 279 TFEU"]}')
    assert got == ["interim relief under Articles 278 and 279 TFEU"]


def test_the_ordinary_case_is_nothing_to_report():
    assert _themes_from_reply('{"themes": []}') == []


def test_a_fenced_or_prefixed_reply_still_parses():
    for wrapped in ('```json\n{"themes": ["a route"]}\n```',
                    'json {"themes": ["a route"]}',
                    'Here:\n{"themes": ["a route"]}\nEnd.'):
        assert _themes_from_reply(wrapped) == ["a route"], wrapped[:24]


def test_a_malformed_reply_reports_nothing_rather_than_an_error_string():
    """A theme that is really an error message would read as a finding."""
    for broken in ("", "no json here", '{"themes": ', '{"themes": "not a list"}',
                   '{"parts": ["wrong key"]}'):
        assert _themes_from_reply(broken) == [], repr(broken)


def test_non_strings_are_dropped_not_stringified():
    got = _themes_from_reply('{"themes": ["a route", {"b": 1}, null, 7, "  ", "another"]}')
    assert got == ["a route", "another"]


def test_the_list_is_bounded():
    many = '{"themes": [' + ",".join(f'"t{i}"' for i in range(20)) + ']}'
    assert len(_themes_from_reply(many)) == 5
