"""Unit tests for the fallback that feeds this round's gate findings.

The fallback exists so a ledger failure cannot stop the gate's own output from
reaching the reviser: `unmet` is best-effort and returns an empty list when the
ledger cannot be read. What it must not do is treat a deliberate exclusion as
an unavailable ledger, because a waived entry is absent from `unmet` by design.

Pure: the selection is a comprehension over three inputs, reproduced here as
`_fallback` so it can be read and tested without a run.

Run from the backend directory:
`python -m pytest tests/test_obligation_fallback.py`
"""


def _fallback(owed, fresh, known):
    """What query_runner appends to `owed` from this round's gate findings."""
    seen = {u["doc"] for u in owed}
    return [
        {"doc": d, "note": n, "fed": int((known.get(d) or {}).get("fed") or 0)}
        for d, n in fresh.items()
        if d not in seen and not (known.get(d) or {}).get("waived")
    ]


def entry(fed=0, waived=None):
    return {"note": "n", "fed": fed, "waived": waived}


# ── what the fallback is for ─────────────────────────────────────────────────


def test_an_unknown_document_is_added():
    """The gate named something the ledger has never seen."""
    got = _fallback([], {"a.md": "why"}, {})
    assert [u["doc"] for u in got] == ["a.md"]


def test_an_unreadable_ledger_still_feeds_this_round():
    """`unmet` is best-effort and yields nothing when the ledger cannot be
    read, which is the case this fallback exists for."""
    got = _fallback([], {"a.md": "why", "b.md": "why"}, {})
    assert len(got) == 2


def test_a_document_already_owed_is_not_duplicated():
    got = _fallback([{"doc": "a.md", "note": "n", "fed": 1}], {"a.md": "why"}, {})
    assert got == []


# ── what it must not revive ──────────────────────────────────────────────────


def test_a_waived_document_is_not_added_back():
    """A waiver is the reviser deciding not to cite. The document stays
    uncited and keeps qualifying, so without this it returns every round."""
    known = {"a.md": entry(fed=2, waived={"by": "reviser", "rationale": "r"})}
    assert _fallback([], {"a.md": "why"}, known) == []


def test_a_system_waived_document_is_not_added_back():
    known = {"a.md": entry(fed=3, waived={"by": "system", "rationale": "r"})}
    assert _fallback([], {"a.md": "why"}, known) == []


def test_a_waived_document_does_not_consume_a_slot_beside_a_live_one():
    known = {"waived.md": entry(waived={"by": "reviser", "rationale": "r"})}
    got = _fallback([], {"waived.md": "why", "new.md": "why"}, known)
    assert [u["doc"] for u in got] == ["new.md"]


# ── the count the cap reads ──────────────────────────────────────────────────


def test_a_known_document_carries_the_ledger_count_not_zero():
    """The cap compares against this number. Hard-coding zero meant a document
    re-added here was never capped however many times it had been fed."""
    got = _fallback([], {"a.md": "why"}, {"a.md": entry(fed=2)})
    assert got[0]["fed"] == 2


def test_a_document_at_the_cap_is_reported_at_the_cap():
    got = _fallback([], {"a.md": "why"}, {"a.md": entry(fed=3)})
    assert got[0]["fed"] == 3


def test_an_unknown_document_starts_at_zero():
    got = _fallback([], {"a.md": "why"}, {})
    assert got[0]["fed"] == 0


def test_a_ledger_entry_without_a_count_reads_as_zero():
    got = _fallback([], {"a.md": "why"}, {"a.md": {"note": "n"}})
    assert got[0]["fed"] == 0
