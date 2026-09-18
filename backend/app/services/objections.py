"""Objections — a run-scoped ledger of what the review asked for and how it ended.

The review and the drafter used to talk in one direction. The review asked
for something (cite this source, close this gap); the drafter either obeyed
or changed nothing, and "changed nothing" was indistinguishable from not
having read the request. So a drafter that had a reason — measured on one
run: *commentary alone cannot establish a rule, so no passage can carry it*
— wrote that reason into a dropped claim, where it went nowhere, and the
review asked for the same source twice more.

This makes the reply a first-class move:

- every request gets a stable id, so a reply can name what it answers;
- the drafter may REFUSE a request with a reason, which is a reply and not
  a silence;
- the next review cycle is shown each outstanding refusal WITH its reason
  and must either ACCEPT it — the point is settled and may never be raised
  again — or RESTATE it with something it has not said before;
- a restatement that adds nothing is an acceptance, so an argument cannot
  be made of repetition;
- after `MAX_EXCHANGES` exchanges the argument stops whatever the review
  says. The point is recorded as unsettled and the run moves on.

Every request carries an IMPORTANCE the review must choose:

- `supporting` — relevant, would improve the answer. An unresolved
  supporting objection is a ledger note and nothing else.
- `essential` — a part of the question is not properly answered without
  this source. The review must say in one line which part and why; an
  `essential` mark with no such line is read as `supporting`, because an
  importance nobody had to justify is not a judgment.

Only an unresolved `essential` objection may affect a run's verdict, and
even then it does not make the run a failure: the answer is written, and it
carries a reader-visible reservation naming the source and what it bears
on. That is a disagreement about completeness between two readers, which is
a thing to show a human, not a thing to hide behind a status meaning "a
part could not be answered".

State lives in query_run.telemetry (the obligation ledger's precedent): no
migration, survives restarts, and the runner is the only writer per run.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
import uuid
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.db import AsyncSessionLocal
from app.models.query import QueryRun

_KEY = "objections"

#: How many times one point may be put to the drafter. The first ask is one
#: exchange; a restatement that carries something new buys a second. A third
#: is refused however good the review's next thought is — two readers who
#: have each said their piece twice are not converging, they are cycling,
#: and the run has an answer to publish.
MAX_EXCHANGES = 2

ESSENTIAL = "essential"
SUPPORTING = "supporting"

#: Asked and not yet answered by the drafter.
OPEN = "open"
#: The drafter refused, with a reason; the next review cycle must rule.
ANSWERED = "answered"
#: The review accepted the refusal. Settled: never raised, never fed again.
ACCEPTED = "accepted"
#: The bound was reached with the two sides still apart.
STALLED = "stalled"
#: The request was met — the source is cited, or discharged by a waiver.
RESOLVED = "resolved"

#: States that end the argument. A subject here is never asked for again.
_SETTLED = (ACCEPTED, STALLED, RESOLVED)

_log = logging.getLogger(__name__)


def _best_effort(neutral):
    """The ledger is bookkeeping: it must never fail the run it serves.

    A storage fault degrades the run to the one-way behaviour this replaces —
    the review asks, the drafter obeys or does not — instead of failing it.
    Logged, so a silent ledger is visible in the run's logs rather than
    invisible.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapped(*a, **k):
            try:
                return await fn(*a, **k)
            except Exception:  # noqa: BLE001
                _log.warning("objection ledger %s failed", fn.__name__,
                             exc_info=True)
                return neutral() if callable(neutral) else neutral
        return wrapped
    return deco


async def _load(run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        if run is None:  # unit tests and dry paths run gates without a row
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


# -- identity -----------------------------------------------------------------


def objection_id(kind: str, subject: str) -> str:
    """A stable id for one point, so a reply can name it. Pure.

    Derived from what the objection is ABOUT rather than from when it was
    raised: the same source named in cycle 1 and again in cycle 4 is one
    argument with a history, not two requests that happen to rhyme. An id
    that changed per cycle would let a settled point come back wearing a new
    name, which is the failure this whole module exists to stop.
    """
    digest = hashlib.sha1(
        f"{(kind or '').strip()}\x00{(subject or '').strip()}".encode()
    ).hexdigest()
    return f"obj-{digest[:8]}"


def _norm(text: str) -> str:
    """Text reduced to its words, for asking whether a restatement is new."""
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def carries_something_new(entry: dict, added: str) -> bool:
    """Is this restatement more than what has already been said? Pure.

    The rule the review is held to: pressing a point costs a new document,
    new evidence, or a narrower ask. Repeating the original request — or a
    previous restatement — is not pressing, it is waiting, and a review that
    can win by waiting never has to read the drafter's reason at all.

    Compared on words rather than characters so re-punctuation does not pass
    as an argument, and a restatement that merely contains the old wording
    plus filler is rejected: substring containment either way counts as the
    same point.
    """
    fresh = _norm(added)
    if len(fresh) < 10:
        return False
    for said in [entry.get("asked") or ""] + [
        r.get("new") or "" for r in (entry.get("rulings") or [])
    ]:
        old = _norm(said)
        if not old:
            continue
        if fresh == old or fresh in old or old in fresh:
            return False
    return True


def state_after_rejection(entry: dict) -> str:
    """Where an objection goes when its refusal is rejected. Pure.

    Back to the drafter while there is an exchange left, and stalled at the
    bound. A stalled essential objection already reaches `contested` and
    prints as a caveat a reader sees, so this adds no new escalation: it puts
    a rejected argument on the same clock as every other one.
    """
    return (STALLED if int(entry.get("exchanges") or 1) >= MAX_EXCHANGES
            else OPEN)


def is_essential(entry: dict) -> bool:
    """Essential, and justified. Pure.

    The justification is the check. `importance` is a word the review chose;
    `why_essential` is the line it had to write to earn it, and an essential
    mark with no line is read as supporting everywhere the two differ.
    """
    return (str(entry.get("importance") or "") == ESSENTIAL
            and bool(str(entry.get("why_essential") or "").strip()))


def importance_of(importance: Any, why_essential: Any) -> tuple[str, str]:
    """`(importance, why_essential)` as the ledger will store them. Pure."""
    why = str(why_essential or "").strip()[:300]
    return (ESSENTIAL if str(importance or "").strip().lower() == ESSENTIAL
            and why else SUPPORTING, why)


# -- the argument -------------------------------------------------------------


@_best_effort(None)
async def raise_objection(
    run_id: uuid.UUID, *, kind: str, subject: str, asked: str,
    importance: Any = SUPPORTING, why_essential: Any = "",
    part: Any = None, cycle: int = 0,
) -> str | None:
    """Put a point to the drafter, and return the id a reply names.

    Returns None when the point is settled — accepted, stalled or resolved.
    A settled point is not re-raised, and the caller reads None as "do not
    ask for this again": that is the rule about a review not re-raising what
    it accepted, enforced where the asking happens rather than trusted to
    the prompt.
    """
    subject = (subject or "").strip()
    if not subject:
        return None
    oid = objection_id(kind, subject)
    entries = await _load(run_id)
    entry = entries.get(oid)
    imp, why = importance_of(importance, why_essential)
    if entry is None:
        entries[oid] = {
            "id": oid, "kind": kind, "subject": subject,
            "asked": (asked or "")[:400], "importance": imp,
            "why_essential": why,
            "part": int(part) if isinstance(part, int) else None,
            "raised_cycle": int(cycle), "exchanges": 1, "state": OPEN,
            "reply": None, "rulings": [],
        }
        await _store(run_id, entries)
        return oid
    if entry.get("state") in _SETTLED:
        return None
    # An open point re-put. The wording may sharpen and the importance may
    # rise — a review that has read more of the corpus can conclude a source
    # it called supporting is in fact essential — but the exchange count and
    # the history are the argument's, not this call's.
    if asked:
        entry["asked"] = str(asked)[:400]
    if imp == ESSENTIAL or entry.get("importance") != ESSENTIAL:
        entry["importance"], entry["why_essential"] = imp, why
    if isinstance(part, int):
        entry["part"] = part
    entries[oid] = entry
    await _store(run_id, entries)
    return oid


@_best_effort(None)
async def refused(run_id: uuid.UUID, oid: str, reason: str,
                  cycle: int = 0) -> None:
    """The drafter declines a request and says why.

    A reason is the whole point, so a refusal without one is not recorded:
    it leaves the objection open, which is what a silence already meant.
    """
    reason = (reason or "").strip()
    if len(reason) < 20:
        return
    entries = await _load(run_id)
    entry = entries.get(oid)
    if entry is None or entry.get("state") in _SETTLED:
        return
    entry["reply"] = {"reason": reason[:400], "cycle": int(cycle)}
    entry["state"] = ANSWERED
    entries[oid] = entry
    await _store(run_id, entries)


@_best_effort(None)
async def rejected(run_id: uuid.UUID, oid: str, reason: str,
                   named: list[str], cycle: int = 0) -> None:
    """A refusal that asserted something the answer does not carry.

    Measured on one run: the review asked that a founding judgment be cited
    for the two conditions it laid down; the drafter refused with "claim 5
    already cites" that judgment; the review accepted; and no evidence row
    in the answer cited it — the file was retrieved, at rank 29, and cited
    by nothing. The point was right, the refusal was false, and the record
    said it was settled.

    A refusal that asserts a citation is checkable against the answer's own
    evidence rows before anyone rules on it, and one that fails the check is
    not a reply: the request goes back to the drafter, and the failed reply
    is kept on the entry so the next round can say why the request still
    stands. A false assertion closing an argument is the same failure shape
    the objections exist to catch, one level up.

    It costs an exchange. Reopening for free lets a drafter hold a point open
    for as long as the run has rounds by asserting the same absent citation
    every time, which is the mirror of the failure the exchange bound already
    stops on the review's side. It is not an infinite loop, because the run
    runs out of cycles, and that is the problem: an objection that never
    stalls never reaches `contested`, so an essential request the drafter
    kept answering falsely ends the run with no reader-visible caveat. The
    point was never settled and nothing said so.
    """
    entries = await _load(run_id)
    entry = entries.get(oid)
    if entry is None or entry.get("state") in _SETTLED:
        return
    entry["rejected_replies"] = (entry.get("rejected_replies") or []) + [
        {"reason": (reason or "").strip()[:400], "named": list(named)[:5],
         "cycle": int(cycle)}]
    # Decided on the count BEFORE this rejection spends one, as `_decide` is
    # on the review's side of the same argument.
    entry["state"] = state_after_rejection(entry)
    entry["exchanges"] = int(entry.get("exchanges") or 1) + 1
    entries[oid] = entry
    await _store(run_id, entries)


@_best_effort(list)
async def outstanding(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Refusals the review has not yet ruled on, oldest first."""
    entries = await _load(run_id)
    return [e for e in entries.values() if e.get("state") == ANSWERED]


@_best_effort(list)
async def open_points(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Requests put to the drafter and not yet answered, oldest first.

    The other side of `outstanding`, and the one the drafter is shown. A
    request reached it buried in the prose of one feedback line among ten,
    inside a call with no memory of ever having been asked; the reply it was
    entitled to make was never made once. This is the list a round presents on
    its own, each entry with the id that names it.
    """
    return [e for e in await ledger(run_id) if e.get("state") == OPEN]


@_best_effort(None)
async def rule(run_id: uuid.UUID, oid: str, ruling: str, added: str = "",
               cycle: int = 0) -> None:
    """The review's decision on one refusal.

    Three ways this ends and only three. ACCEPT settles it. RESTATE with
    something new re-opens it for one more exchange. RESTATE with nothing
    new — or once the exchange bound is spent — ends it too: as an
    acceptance in the first case, because a point pressed without an
    argument has none, and as a stall in the second, because two readers who
    have each spoken twice are cycling rather than converging.
    """
    entries = await _load(run_id)
    entry = entries.get(oid)
    if entry is None or entry.get("state") != ANSWERED:
        return
    decided, added = _decide(entry, ruling, added)
    entry["rulings"] = (entry.get("rulings") or []) + [
        {"cycle": int(cycle), "ruling": decided, "new": added[:300]}]
    if decided == "restate":
        entry["state"] = OPEN
        entry["exchanges"] = int(entry.get("exchanges") or 1) + 1
    else:
        entry["state"] = STALLED if decided == "stalled" else ACCEPTED
    entries[oid] = entry
    await _store(run_id, entries)


def _decide(entry: dict, ruling: str, added: str) -> tuple[str, str]:
    """What this ruling actually is, whatever it was called. Pure.

    Returns `("accept" | "restate" | "stalled", added)`. The review does not
    get to decide that it may keep going: pressing costs something new, and
    the bound is the bound.
    """
    if str(ruling or "").strip().lower() != "restate":
        return "accept", ""
    if not carries_something_new(entry, added):
        return "accept", ""
    if int(entry.get("exchanges") or 1) >= MAX_EXCHANGES:
        return "stalled", added
    return "restate", added


@_best_effort(None)
async def rule_all(run_id: uuid.UUID, rulings: list[dict],
                   cycle: int = 0) -> None:
    """Apply a review cycle's rulings, and treat its silences as acceptances.

    A refusal the review did not rule on is accepted. It is the only reading
    that keeps the loop bounded: reading silence as "still objecting" would
    let a review press every point forever by never answering any of them,
    which is precisely the one-way behaviour this replaces.
    """
    by_id = {}
    for r in rulings or []:
        if isinstance(r, dict) and str(r.get("id") or "").strip():
            by_id[str(r["id"]).strip()] = r
    for entry in await outstanding(run_id):
        r = by_id.get(entry["id"]) or {}
        await rule(run_id, entry["id"], str(r.get("ruling") or "accept"),
                   str(r.get("new") or ""), cycle=cycle)


@_best_effort(None)
async def resolve(run_id: uuid.UUID, subjects: list[str],
                  kind: str = "source") -> None:
    """These requests were met. Terminal, and it outranks every other state.

    Called with what the answer now cites: a source that made it into the
    answer settles its own argument, whatever either side last said about it.
    """
    entries = await _load(run_id)
    changed = False
    for subject in subjects or []:
        entry = entries.get(objection_id(kind, subject))
        if entry is not None and entry.get("state") != RESOLVED:
            entry["state"] = RESOLVED
            changed = True
    if changed:
        await _store(run_id, entries)


# -- reading it back ----------------------------------------------------------


@_best_effort(set)
async def settled_subjects(run_id: uuid.UUID) -> set[str]:
    """What may never be asked for again: accepted, stalled or resolved.

    The review is told not to re-raise an accepted point and not to re-feed
    a source whose refusal it accepted. This is the enforcement, because a
    rule that lives only in a prompt is a request.
    """
    return {e.get("subject") for e in (await _load(run_id)).values()
            if e.get("state") in _SETTLED and e.get("subject")}


@_best_effort(list)
async def ledger(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every objection this run raised and how each ended, oldest first.

    What was asked, what the drafter said, what the review decided and in
    which cycle — so a reader can see what was argued rather than inferring
    it from a count of cycles.
    """
    return sorted(
        (await _load(run_id)).values(),
        key=lambda e: (int(e.get("raised_cycle") or 0), str(e.get("id") or "")),
    )


def as_record(entry: dict) -> dict[str, Any]:
    """One ledger entry flattened into a row a reader can scan. Pure.

    The stored entry is nested — the reply under `reply`, the decisions under
    `rulings` — which is the right shape to reason with and the wrong one to
    read a hundred of. This is the reading shape: what was asked, how much the
    review said it mattered, what the drafter answered, what the review decided
    and in which cycle. The full ruling history rides along, because the last
    ruling alone cannot show a point that was pressed and then let go.
    """
    rulings = [r for r in (entry.get("rulings") or []) if isinstance(r, dict)]
    last = rulings[-1] if rulings else {}
    reply = entry.get("reply") or {}
    return {
        "id": entry.get("id"),
        "kind": entry.get("kind"),
        "source": entry.get("subject"),
        "part": entry.get("part"),
        "asked": entry.get("asked") or "",
        # The earned importance, not the word the review typed: `essential`
        # here always means `essential` with the line that justifies it.
        "importance": ESSENTIAL if is_essential(entry) else SUPPORTING,
        "why_essential": entry.get("why_essential") or "",
        "raised_cycle": int(entry.get("raised_cycle") or 0),
        "reason": str(reply.get("reason") or ""),
        "answered_cycle": reply.get("cycle"),
        "ruling": str(last.get("ruling") or ""),
        "ruled_cycle": last.get("cycle"),
        "rulings": rulings,
        "state": entry.get("state"),
        "exchanges": int(entry.get("exchanges") or 1),
        # Replies that asserted a citation the answer did not carry, and so
        # never counted as replies: see `rejected`.
        "rejected_replies": [r for r in (entry.get("rejected_replies") or [])
                             if isinstance(r, dict)],
    }


@_best_effort(list)
async def record(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """The whole argument as telemetry rows, oldest first.

    Every outcome, not only the ones that changed the answer: a request that
    was met, one that was refused and accepted, one that stalled. A ledger
    that recorded only the disagreements could not show that the loop mostly
    converges, which is the first thing anyone reading it wants to know.
    """
    return [as_record(e) for e in await ledger(run_id)]


@_best_effort(list)
async def contested(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """The disagreements that survived: essential, justified, and stalled.

    These and only these may affect a run's verdict. A stalled supporting
    objection is a note; an accepted refusal of an essential request is
    settled and is not one; and a request that was met is neither.
    """
    return [e for e in await ledger(run_id)
            if e.get("state") == STALLED and is_essential(e)]


@_best_effort(list)
async def notes(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """The ledger as notes on the answer — one per point that did not resolve.

    `caveat` marks the ones a reader must see: an essential request the
    review stood behind and the drafter refused. Everything else is here to
    be read by someone asking what was argued, and changes nothing about the
    answer as it is published.

    Everything that is not `resolved` is here, not only what was argued to a
    conclusion. A request the drafter never answered, and one it refused with
    a reason the review never got to rule on, both ended the run unresolved,
    and an `open_notes` that showed only the finished arguments would say a
    run settled everything it merely ran out of cycles on.
    """
    return [
        {"id": e.get("id"), "kind": e.get("kind"), "state": e.get("state"),
         "source": e.get("subject"), "part": e.get("part"),
         "importance": ESSENTIAL if is_essential(e) else SUPPORTING,
         "why_essential": e.get("why_essential") or "",
         "asked": e.get("asked") or "",
         "reason": ((e.get("reply") or {}).get("reason") or ""),
         "exchanges": int(e.get("exchanges") or 1),
         "caveat": e.get("state") == STALLED and is_essential(e)}
        for e in await ledger(run_id)
        if e.get("state") != RESOLVED
    ]
