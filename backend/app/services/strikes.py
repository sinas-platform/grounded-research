"""Strikes — how many times the review has failed the same claim, and what
that still permits the drafter to do about it.

A revision round finds a defect in a claim and asks for it to be fixed. The
model that just wrote the claim fixes it the cheapest way it can: it rewords
the same assertion, keeps the same citation, and sends it back. The next round
finds the same defect, because the defect was never in the wording.

Measured on one live run: seven revision rounds, each changing one to three
claims. One claim failed the evidence judge in the first validation round, was
revised in the fifth round, revised again in the sixth, and dropped in the
seventh with the drafter's own reason — that the claim asserted a step follows
automatically where the actual trigger is discretionary. That reason was
available at the second round. Three rounds bought the conclusion the second
one already had.

A sentence in the prompt already asked for this and did not get it, for three
reasons this module removes:

- it was keyed on the claim's SEQUENCE NUMBER. A sequence is a position in a
  list the engine reorders and renumbers — `order_claims` assigns it, drops
  leave gaps, additions take the next free number, and compaction closes the
  gaps. Claim 16 in round one and claim 16 in round five need not be the same
  proposition, and the same proposition need not keep the number.
- it was keyed on state held in LOCAL VARIABLES of the validation loop, and
  that loop restarts itself once per gate cycle. Every restart began the
  history at zero, so a claim could be failed in round one, failed again after
  a gate cycle, and be greeted both times as if it were new.
- it was ADVICE. Nothing in the engine declined a reword, so a drafter that
  reworded anyway was obeyed.

So the count lives here, keyed on the claim's row id, which survives
renumbering because it is not a number the engine assigns for reading. On a
claim's second finding the permitted dispositions are three, and rewording is
not among them:

- DROP it, with a reason.
- DEFEND it, by rebinding it to evidence it does not already cite — a
  different passage, or a different document. A claim that a second passage
  genuinely carries is a claim worth keeping, and finding that passage is work
  the reword was standing in for.
- REFUSE, by keeping it with a reason saying why it stands as written.

A revision that returns the same citation is not applied, and the refusal is
recorded rather than dropped on the floor — `screen_patch` is where that
happens, and it is pure, so what the rule does to a patch can be read without
a database.

State lives in query_run.telemetry, on the objection ledger's precedent: no
migration, survives a restart, and the runner is the only writer per run.
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.db import AsyncSessionLocal
from app.models.query import QueryRun

_KEY = "claim_strikes"

#: Findings a claim may attract before rewording stops being a move. The
#: first finding is a defect to fix however the drafter sees fit; the second
#: says the fix did not work, and a third attempt at the same fix is a round
#: spent to learn what the second one already showed.
STRIKE_LIMIT = 2

#: How a struck claim ended.
DROPPED = "dropped"
DEFENDED = "defended"
REFUSED = "refused"
#: Removed by the engine when the rounds ran out, not by a disposition.
EXHAUSTED = "removed_at_exhaustion"

_log = logging.getLogger(__name__)


def _best_effort(neutral):
    """The ledger is bookkeeping: it must never fail the run it serves.

    A storage fault degrades the loop to the behaviour this replaces — the
    review finds, the drafter rewords — rather than failing a run over a
    counter. Logged, so a silent ledger is visible in the logs.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapped(*a, **k):
            try:
                return await fn(*a, **k)
            except Exception:  # noqa: BLE001
                _log.warning("strike ledger %s failed", fn.__name__,
                             exc_info=True)
                return neutral() if callable(neutral) else neutral
        return wrapped
    return deco


async def _load(run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if run is None:  # unit tests and dry paths run rounds without a row
            return {}
        return dict(((run.telemetry or {}).get(_KEY)) or {})


async def _store(run_id: uuid.UUID, entries: dict[str, dict[str, Any]]) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if run is None:
            return
        tel = dict(run.telemetry or {})
        tel[_KEY] = entries
        run.telemetry = tel
        flag_modified(run, "telemetry")
        await session.commit()


# -- identity of the evidence -------------------------------------------------


def fingerprint(spans: Iterable[Any]) -> list[str]:
    """What a claim cites, as comparable tokens. Pure.

    One token per passage: the document it comes from and the lines it covers.
    That is the grain the rule needs — "a different passage or document" — and
    it is the grain both sides are available at, since a patch item names its
    spans the same way a bound evidence row stores them.

    Sorted and deduplicated, so the same citation written in a different order
    is the same citation.
    """
    out: set[str] = set()
    for s in spans or []:
        if not isinstance(s, Mapping):
            continue
        fn = str(s.get("filename") or "").strip()
        if not fn:
            continue
        try:
            lf = int(s["line_from"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            lt = int(s.get("line_to"))
        except (TypeError, ValueError):
            lt = lf
        out.add(f"{fn}:{lf}-{lt}")
    return sorted(out)


def rebinds(before: Iterable[str], after: Iterable[str]) -> bool:
    """Does the offered evidence reach past what the claim already cites? Pure.

    True when the revision names at least one passage the claim does not
    already stand on. That is the whole of the defence: the claim survives
    because a source carries it, not because the sentence changed shape.

    A revision offering NO evidence is not a defence. An inference claim rests
    on other claims rather than on a passage, and a struck claim rebuilt into
    one is the reword in its purest form — the same assertion, now resting on
    nothing the judge can read.
    """
    later = set(after or [])
    if not later:
        return False
    return bool(later - set(before or []))


# -- the rule, applied to a patch ---------------------------------------------


def screen_patch(
    patch: Mapping[str, Any] | None,
    on_strike: Mapping[int, Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Strip the revisions that reword a struck claim. Pure.

    `on_strike` maps a claim's CURRENT sequence — the number the patch keys on
    — to what the ledger knows about it, including the evidence it stands on
    now. Returns the patch as it will be applied, and one record per rejected
    revision, which is the thing worth reading afterwards: the loop did not
    quietly work, it declined a move and said so.

    Every other disposition passes through untouched. A drop is a permitted
    move, a keep is a permitted move, and an addition is not about this claim
    at all.
    """
    if not patch or not on_strike:
        return patch, []
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in patch.get("revise") or []:
        entry = on_strike.get(item.get("seq"))
        if entry is None:
            kept.append(item)
            continue
        cited = list(entry.get("evidence") or [])
        offered = fingerprint(item.get("evidence") or [])
        if rebinds(cited, offered):
            kept.append(item)
            continue
        rejected.append({
            "claim_id": entry.get("claim_id"),
            "sequence": item.get("seq"),
            "findings": int(entry.get("findings") or 0),
            "cited": cited,
            "offered": offered,
            "why": "a claim on its second finding may be dropped, defended on "
                   "different evidence, or kept with a reason — not reworded",
        })
    if not rejected:
        return dict(patch), []
    return {**patch, "revise": kept}, rejected


# -- counting -----------------------------------------------------------------


@_best_effort(dict)
async def record_findings(
    run_id: uuid.UUID,
    subjects: Sequence[tuple[str, Any]],
    round_no: int = 0,
) -> dict[str, int]:
    """Count one round's findings, one strike per claim, and return the totals.

    `subjects` is `(claim id, the sequence it was named by)` for every finding
    the round actually PUTS TO THE DRAFTER. A finding held back — by the cap on
    how many a round carries — is not a strike, because the drafter was never
    asked about it and cannot have failed to act.

    One strike per claim per round however many of its spans failed: three
    failing passages on one claim is one claim the review has failed, and
    counting it three times would strike a claim out in a single round.
    """
    entries = await _load(run_id)
    seen: set[str] = set()
    for claim_id, sequence in subjects or []:
        cid = str(claim_id or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        entry = entries.get(cid) or {
            "claim_id": cid, "findings": 0, "rejected_rewords": 0,
            "disposition": None, "history": [],
        }
        entry["findings"] = int(entry.get("findings") or 0) + 1
        # The sequence is kept for READING the record, never for matching on.
        # It is the number the claim wore when it was last found, and the
        # whole reason this ledger exists is that the number moves.
        entry["sequence"] = sequence
        entry["history"] = (entry.get("history") or [])[:20] + [
            {"round": int(round_no), "sequence": sequence}]
        entries[cid] = entry
    if seen:
        await _store(run_id, entries)
    return {cid: int(e.get("findings") or 0) for cid, e in entries.items()}


@_best_effort(dict)
async def entries(run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """The ledger as stored, keyed on claim id."""
    return await _load(run_id)


@_best_effort(set)
async def struck(run_id: uuid.UUID) -> set[str]:
    """The claims rewording is no longer a move on.

    A disposition does not lift the ban. A claim defended on new evidence and
    then failed again is a claim on its third finding, and the one thing it
    must still not do is go round once more on its wording.
    """
    return {cid for cid, e in (await _load(run_id)).items()
            if int(e.get("findings") or 0) >= STRIKE_LIMIT}


@_best_effort(None)
async def rejected_reword(run_id: uuid.UUID, claim_id: str,
                          sequence: Any = None, round_no: int = 0) -> None:
    """A reword of a struck claim was declined. Recorded on the claim."""
    cid = str(claim_id or "").strip()
    if not cid:
        return
    entries_ = await _load(run_id)
    entry = entries_.get(cid)
    if entry is None:
        return
    entry["rejected_rewords"] = int(entry.get("rejected_rewords") or 0) + 1
    entry["history"] = (entry.get("history") or [])[:20] + [
        {"round": int(round_no), "sequence": sequence, "rejected": "reword"}]
    entries_[cid] = entry
    await _store(run_id, entries_)


@_best_effort(None)
async def settle(run_id: uuid.UUID, claim_id: str, disposition: str,
                 round_no: int = 0) -> None:
    """How a claim's argument ended, and in which round.

    The last disposition stands. A claim defended in one round and dropped in
    a later one ended by being dropped, and that is what a reader wants from
    one field; the rounds it took are in `history`.
    """
    cid = str(claim_id or "").strip()
    if not cid or not disposition:
        return
    entries_ = await _load(run_id)
    entry = entries_.get(cid)
    if entry is None:
        return
    entry["disposition"] = str(disposition)
    entry["settled_round"] = int(round_no)
    entries_[cid] = entry
    await _store(run_id, entries_)


# -- reading it back ----------------------------------------------------------


def as_record(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One claim's strike history as a row a reader can scan. Pure."""
    findings = int(entry.get("findings") or 0)
    return {
        "claim_id": entry.get("claim_id"),
        "last_sequence": entry.get("sequence"),
        "findings": findings,
        "struck": findings >= STRIKE_LIMIT,
        "disposition": entry.get("disposition"),
        "settled_round": entry.get("settled_round"),
        "rejected_rewords": int(entry.get("rejected_rewords") or 0),
        "history": [h for h in (entry.get("history") or [])
                    if isinstance(h, dict)],
    }


@_best_effort(list)
async def record(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every claim the review found fault with, worst first.

    Not only the struck ones. A ledger showing only the claims that went round
    twice cannot say how rare that is, which is the first thing anyone reading
    it wants to know.
    """
    return sorted((as_record(e) for e in (await _load(run_id)).values()),
                  key=lambda r: (-r["findings"], str(r["claim_id"])))
