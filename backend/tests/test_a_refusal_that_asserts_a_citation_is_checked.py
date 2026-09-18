"""A refusal that says "the answer already cites X" is checked against the
answer before it counts as a reply.

Measured on one run (review of #169): the gate asked that the founding
judgment be cited for the two conditions it laid down; the drafter refused
with "claim 5 already cites" that judgment; the review accepted the refusal;
and no evidence row anywhere in the answer cited that file — it had been
retrieved, at rank 29, and was cited by nothing. The objection was right,
the refusal was false, and the record said the point was settled.

What these pin:

- the files a piece of prose names are read off it, once each, whatever
  surrounds them;
- a refusal naming a file no claim cites is rejected: the request stays open,
  the failed reply is kept, and the next round tells the drafter why;
- a refusal naming a file the answer does cite — or that a claim in this
  same patch cites — is a reply like any other; a file named only in the
  patch's replies does not count, or the assertion would vouch for itself;
- a refusal that names no file is not checked here at all: arguing is the
  review's to judge, asserting is checkable.

No database and no model: the ledger's storage is a dict and the answer's
citations are a stubbed set.
"""

import uuid

import pytest

from app.services import drafting_chat, objections
from app.services import query_runner as qr

RUN = uuid.uuid4()
ANSWER = uuid.uuid4()
FOUNDING = "founding-judgment.md"
LATER = "later-appeal.md"


@pytest.fixture
def ledger(monkeypatch):
    store: dict = {}

    async def _load(_run_id):
        return dict(store)

    async def _store(_run_id, entries):
        store.clear()
        store.update(entries)

    monkeypatch.setattr(objections, "_load", _load)
    monkeypatch.setattr(objections, "_store", _store)
    return store


@pytest.fixture
def cites(monkeypatch):
    """What the answer's claims cite, and a fixed cycle number."""
    cited: set[str] = set()

    async def _cited(_answer_id):
        return set(cited)

    async def _cycle(_run_id, _stage, _prefix):
        return "gate_3"

    monkeypatch.setattr(qr, "_cited_filenames", _cited)
    monkeypatch.setattr(qr, "_next_cycle_key", _cycle)
    return cited


async def _ask(subject=FOUNDING):
    return await objections.raise_objection(
        RUN, kind="source", subject=subject,
        asked="cite the founding judgment for the two conditions", cycle=1)


def _refusal(oid, rationale):
    return {"refuse": [{"objection": oid, "rationale": rationale}]}


# ─────────────────────────────────────────────────────────────
# Reading filenames off prose
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("prose,files", [
    (f"Claim 5 already cites the founding judgment ({FOUNDING}) for both "
     "conditions.", [FOUNDING]),
    (f"see {FOUNDING} and {LATER}; also {FOUNDING} again", [FOUNDING, LATER]),
    ('"notes_v2.final.md" carries it', ["notes_v2.final.md"]),
    ("[chapter-one.md]", ["chapter-one.md"]),
    ("no higher-standing source states the proposition", []),
    ("", []),
])
def test_the_files_a_reply_names_are_read_off_it(prose, files):
    assert qr._named_files(prose) == files


# ─────────────────────────────────────────────────────────────
# The check
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_refusal_naming_a_file_nothing_cites_is_rejected(ledger, cites):
    oid = await _ask()
    cites.add(LATER)                          # the later appeal is cited; the founding judgment is not
    recorded = await qr._record_refusals(RUN, ANSWER, _refusal(
        oid, f"Claim 5 already cites the founding judgment ({FOUNDING}) for "
             "both conditions."))
    assert recorded == 0, "a false assertion is not a reply"
    entry = ledger[oid]
    assert entry["state"] == objections.OPEN, "the request stands"
    assert not entry.get("reply"), "nothing was recorded as the drafter's answer"
    assert entry["rejected_replies"][0]["named"] == [FOUNDING]
    assert entry["rejected_replies"][0]["cycle"] == 2
    # The review never gets to rule on it, because there is nothing to rule on.
    assert await objections.outstanding(RUN) == []
    assert [p["id"] for p in await objections.open_points(RUN)] == [oid]


@pytest.mark.asyncio
async def test_the_drafter_is_told_why_the_request_still_stands(ledger, cites):
    oid = await _ask()
    await qr._record_refusals(RUN, ANSWER, _refusal(
        oid, f"Claim 5 already cites {FOUNDING} for the conditions."))
    block = drafting_chat.objections_block(await objections.open_points(RUN))
    assert oid in block
    assert FOUNDING in block
    assert "did not count" in block


@pytest.mark.asyncio
async def test_a_refusal_naming_a_file_the_answer_cites_is_a_reply(ledger, cites):
    oid = await _ask()
    cites.add(FOUNDING)
    recorded = await qr._record_refusals(RUN, ANSWER, _refusal(
        oid, f"Claim 5 already cites {FOUNDING} for both conditions of the test."))
    assert recorded == 1
    assert ledger[oid]["state"] == objections.ANSWERED


@pytest.mark.asyncio
async def test_a_file_a_claim_in_this_same_patch_cites_counts_as_cited(ledger, cites):
    """The refusal and the revision that makes it true can land together."""
    oid = await _ask()
    patch = {
        "revise": [{"seq": 5, "text": "the two conditions, as first laid down",
                    "evidence": [{"filename": FOUNDING, "line_from": 40,
                                  "line_to": 44, "locator": None}]}],
        **_refusal(oid, f"Claim 5 now cites {FOUNDING}, revised in this round."),
    }
    assert await qr._record_refusals(RUN, ANSWER, patch) == 1
    assert ledger[oid]["state"] == objections.ANSWERED


@pytest.mark.asyncio
async def test_a_file_named_only_in_the_patch_replies_does_not_vouch_for_itself(ledger, cites):
    oid = await _ask()
    patch = {
        "keep": [{"seq": 5, "objection": oid,
                  "rationale": f"claim 5 stands: it cites {FOUNDING} already"}],
        "refuse": [],
    }
    assert await qr._record_refusals(RUN, ANSWER, patch) == 0
    assert ledger[oid]["state"] == objections.OPEN


@pytest.mark.asyncio
async def test_a_refusal_that_argues_rather_than_asserts_is_not_checked_here(ledger, cites):
    oid = await _ask()
    assert await qr._record_refusals(RUN, ANSWER, _refusal(
        oid, "The higher-standing sources were read for this point and none "
             "of them states it; the proposition is the commentator's own.")) == 1
    assert ledger[oid]["state"] == objections.ANSWERED


@pytest.mark.asyncio
async def test_a_rejected_reply_is_on_the_record(ledger, cites):
    oid = await _ask()
    await qr._record_refusals(RUN, ANSWER, _refusal(
        oid, f"Claim 5 already cites {FOUNDING} for both conditions."))
    rec = [r for r in await objections.record(RUN) if r["id"] == oid][0]
    assert rec["state"] == objections.OPEN
    assert rec["rejected_replies"][0]["named"] == [FOUNDING]
