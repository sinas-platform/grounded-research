"""A general proposition rests on the highest-standing source that carries it.

THE DEFECT. An expert review of published answers found claims stating a
general proposition — a claim of kind `rule`, in the engine's neutral claim
kinds — resting on a source of a class the deployment had labelled as
describing rather than laying down, while a source of a class that stands
higher, and says the same thing, sat in the same retrieved set, uncited. The
label was already printed after the sentence. Labelling made the violation
visible and did nothing else about it.

THE RULE IS RELATIVE, NOT CATEGORICAL. Nothing here forbids a class. A rule
claim must cite the highest-standing source AMONG THE DOCUMENTS RETRIEVED FOR
THIS ANSWER that actually carries the proposition, and "actually carries it"
is not something arithmetic can settle — so a gap found here is an OBJECTION
put to the drafter, never a verdict. Three things follow from that and all
three are the point:

  * A claim resting on the lowest-standing class in the corpus is perfectly
    correct when nothing better was retrieved. The comparison is against what
    came back, not against what exists.
  * Before the objection may be refused, the higher-standing documents are
    read for the proposition. The drafter only ever sees passages extracted
    for its planned claims, so it may genuinely never have been shown the
    better source — that re-read is the caller's, and it is what turns "you
    should have cited this" into either a passage or a settled point.
  * A refusal with a reason ends it. If nothing in the higher-standing
    documents carries the proposition, the honest answer is the one the
    drafter already gave: the claim stands, and it says so in its own words.

STANDING COMES FROM THE DEPLOYMENT. A class declares a rank (`standing` on
the class, 1 highest, ties allowed) and this module compares two integers. It
holds no class name and no term of art, and it cannot: the ordering of one
collection's kinds of source is knowledge about that collection.

AN UNRANKED CLASS IS INERT. A class that declares no standing neither
satisfies the rule nor breaches it — a claim citing only unranked sources
raises nothing, and an unranked document is never named as the better source
the claim should have used. Absence of a declaration is absence of an
opinion, never a default position at the bottom of the order.

Pure. Finding the gaps, wording the objection and deciding a gap has closed
are all functions of their arguments, so the policy is testable without a
model, a database or a run.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The claim kinds this rule judges. One kind, and the narrow one: `rule` is
#: "a general proposition the source lays down", which is exactly the thing
#: the review found resting on material that describes rather than lays down.
#: Everything else is untouched — a claim reporting what a source SAYS, what
#: happened, or how a process ran is not weakened by the standing of the
#: source it reports, and a conclusion rests on the claims it follows from.
#:
#: `test` is deliberately not here, though it is a rule stated as ordered
#: conditions. Adding it is this tuple plus a test; it is left out until the
#: rule has been measured on the narrow case, because a gate that fires on
#: more claims than anyone has checked is how a finding gets switched off.
RULE_KINDS = ("rule",)

#: The ledger kind these objections carry. Its own kind, not `source`: the
#: subject is a claim rather than a document, the two are settled by
#: different things, and one flat kind would let a standing argument retire a
#: source obligation that was never made.
KIND = "standing"

#: How many higher-standing documents one objection names. The drafter has to
#: read what it is handed, and a list of twelve is a list nobody reads; the
#: best-standing ones lead, so the cut is from the bottom.
MAX_NAMED = 3


@dataclass(frozen=True)
class Gap:
    """One rule claim resting lower than the retrieved set allows.

    `cited_standing` is the best (numerically lowest) rank among the ranked
    sources the claim cites; `better_standing` the rank of the best documents
    retrieved that beat it. `better` names those documents, best first.
    """

    claim_id: str
    sequence: int
    text: str
    cited: tuple[str, ...]
    cited_standing: int
    better: tuple[str, ...]
    better_standing: int

    @property
    def subject(self) -> str:
        """What this objection is ABOUT, for the ledger's stable id.

        The CLAIM, by its row id. Two properties are needed and only the id
        has both. A sequence is a position in a list the run is still editing,
        so a drop renumbers it and a point settled in cycle 1 would come back
        wearing a new name in cycle 3 — the exact failure the ledger exists to
        stop. And a filename would collide with the source obligations, whose
        subjects ARE filenames: `settled_subjects` is one flat set of strings
        across every kind, so a standing refusal about a document would
        silently retire the separate request to cite it.
        """
        return f"claim {self.claim_id}"


def gaps(claims: list[dict], standing_by_file: dict[str, int | None]
         ) -> list[Gap]:
    """Which rule claims a higher-standing retrieved document stands over.

    Each claim is `{"id", "sequence", "kind", "text", "cites"}` — `cites`
    being the filenames its evidence binds to. `standing_by_file` is every
    document retrieved for this answer against the rank its class declared,
    `None` for a document whose class declared none.

    A claim raises nothing when it cites no ranked source at all: that is
    either a claim resting on unranked material, which is inert by
    declaration, or a derived claim resting on other claims, which cites
    nothing. Pure.
    """
    ranked = {fn: int(v) for fn, v in (standing_by_file or {}).items()
              if v is not None and not isinstance(v, bool)}
    if not ranked:
        return []
    out: list[Gap] = []
    for c in claims or []:
        if str(c.get("kind") or "") not in RULE_KINDS:
            continue
        cites = tuple(str(f) for f in (c.get("cites") or ()))
        mine = [ranked[f] for f in cites if f in ranked]
        if not mine:
            continue
        rests_at = min(mine)
        better = sorted(
            (fn for fn, v in ranked.items() if v < rests_at and fn not in cites),
            key=lambda fn: (ranked[fn], fn))
        if not better:
            continue
        out.append(Gap(
            claim_id=str(c.get("id")),
            sequence=int(c.get("sequence") or 0),
            text=str(c.get("text") or ""),
            cited=tuple(f for f in cites if f in ranked),
            cited_standing=rests_at,
            better=tuple(better[:MAX_NAMED]),
            better_standing=ranked[better[0]],
        ))
    return out


def closed(subjects: set[str], open_gaps: list[Gap]) -> list[str]:
    """Which still-running arguments the answer itself has ended. Pure.

    The gaps are recomputed every cycle from the claims as they now stand, so
    a claim that has since been re-cited, revised into another kind or
    dropped stops appearing here. All three are the same outcome and it is
    the one `RESOLVED` already means in the ledger: the claim no longer rests
    lower than the retrieved set allows, so there is nothing left to argue
    about. That is what the resolved count says, and it is deliberately not a
    narrower claim about HOW — a claim that now cites the higher-standing
    source and a claim that was withdrawn have both stopped being the defect.

    `subjects` must be the arguments still running. A point the drafter
    argued its way out of is settled by the review's ruling, and passing an
    accepted or stalled subject in here would rewrite that history as though
    the answer had fixed it.
    """
    still = {g.subject for g in open_gaps}
    return sorted(s for s in subjects if s not in still)


#: What the ledger stores for one request, and therefore what this may write.
#: An ask longer than this is truncated where it is stored, and what the
#: drafter is then shown is a sentence cut in half — so the ask says the whole
#: of the finding and nothing else, and the moves it may answer with are the
#: caller's to spell out in the feedback line around it.
ASK_CHARS = 400
#: Room the quote may take inside that, so naming the passage cannot be what
#: pushes the finding over the edge.
QUOTE_CHARS = 150


def objection(gap: Gap, found: dict | None) -> str:
    """What is put to the drafter about one gap, after the read. Pure.

    Two messages, because the two cases are different findings. With a
    passage found, the finding is that the higher-standing source says this,
    here, in these words — an answerable request rather than an aspiration.
    With nothing found, the finding is that the documents were opened for
    this proposition and nothing came back, which is what lets a refusal be
    made on the record instead of against it. One pass over whole documents
    is not proof, and the wording does not claim it is.

    `found` is `{"filename", "line_from", "line_to", "quote"}` or None.
    """
    lead = (
        f"Claim {gap.sequence} states a general proposition and rests on "
        f"{', '.join(gap.cited)} (standing {gap.cited_standing}). Retrieved "
        f"for this same answer and standing higher at {gap.better_standing}: "
        f"{', '.join(gap.better)}."
    )
    if found:
        where = str(found.get("filename") or "")
        lines = ("" if found.get("line_from") is None else
                 f" lines {found.get('line_from')}-{found.get('line_to')}")
        quote = str(found.get("quote") or "").strip()[:QUOTE_CHARS]
        tail = f" {where}{lines} states it: \"{quote}\"."
    else:
        tail = (" Those were read in full for this proposition and no passage "
                "stating it came back.")
    return (lead + tail)[:ASK_CHARS]


def point(gap: Gap) -> str:
    """The gap as a point the reviser is given passages for. Pure.

    Revision may cite only passages it is shown, so a request naming a
    document without opening it is a request that can be obeyed by doing
    nothing. The marker is the one the extraction planner already reads, so a
    standing gap reaches the extractor exactly as an owed source does.
    """
    return (f"The proposition of claim {gap.sequence}: {gap.text[:300]} "
            f"[obligated document: {gap.better[0]}]")


def summary(ledger: list[dict]) -> dict[str, int]:
    """The three counts this rule has to be able to show, from the ledger.

    Raised, resolved, refused — and the reason all three are wanted together
    is that the two-way argument had never once been used: across three live
    runs the drafter refused nothing, because evidence findings are usually
    right and there was nothing to argue. A standing objection is the case
    where a reasoned refusal is the correct move, so "how often is it
    refused" is the measurement that says whether the path is real.

    `ledger` is `objections.record(run_id)` — every objection of every kind;
    only the standing ones are counted. Pure.
    """
    mine = [e for e in (ledger or []) if str(e.get("kind") or "") == KIND]
    return {
        "raised": len(mine),
        # Ended by the answer rather than by the argument: the claim stopped
        # resting lower than the retrieved set allows. See `closed`.
        "resolved": sum(1 for e in mine if e.get("state") == "resolved"),
        # Answered with a reason. Counted whether or not the review has ruled
        # yet, because the drafter's move is what is being measured here and
        # a refusal the review has not reached is still a refusal made.
        "refused": sum(1 for e in mine if str(e.get("reason") or "").strip()),
    }
