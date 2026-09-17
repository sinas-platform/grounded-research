"""Re-read what the answer cites before calling a part of the question
unanswered.

A reviewer asked for four specific things and the answer addressed one. All
four were in documents the answer already cited. Extraction reads per planned
claim from that claim's anchor documents, not whole documents, so a document
opened for one passage can hold the material for a part in a passage nobody
read. The gate then judges coverage on the claims, sees nothing about the
part, and reports it, correctly, about an answer whose own sources contain it.

So this is a re-read, not a retrieval. The documents are identified, their text
is stored, and the condition that fires it is already computed: the gate knows
which parts it has just marked uncovered. What was missing is opening those
documents again, whole, before that verdict leaves.

Two properties this has to keep, and both are about honesty rather than
recall. It may not reach a document the answer does not cite, because that
would be the gate answering the question instead of checking it. And a
re-read that found nothing has to be distinguishable from a re-read that never
ran, or a later reader cannot tell an absent limb from an unchecked one.

ONE MECHANISM, TWO VERDICTS. The second caller is the standing gate, which
opens the HIGHER-STANDING documents of the retrieved set before a claim is
allowed to keep resting on a lower-standing one. The condition differs and
so does the boundary — that one may reach a document the answer does not
cite, because the whole point is that the drafter was never shown it — but
the read is the same read: whole document, verbatim quote or none, one reply
shape. `look_prompt` is that read, and the two wrappers are the two
questions. A second copy of it would drift, and it would drift towards
finding things that are not there.

Pure. Deciding what to re-read, building the prompt and applying the result
are separate from making the call, so the policy is testable without a model.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Below this a stored document is not text worth re-reading, and its absence
#: is not evidence that a part is unaddressed.
_MIN_CHARS = 40


@dataclass(frozen=True)
class Cited:
    """One document the answer cites, with the text as stored."""
    filename: str
    text: str


def needs_reread(parts: list[dict], cited: list[Cited]) -> list[dict]:
    """The parts worth re-reading for, which is none unless something failed.

    Nothing runs while every part is covered, and that is most answers. The
    cost is bounded by the documents the answer already cites, so an answer
    that cites four documents and misses one part re-reads four documents
    once, and one that misses nothing re-reads nothing.
    """
    if not any(c.text and len(c.text) >= _MIN_CHARS for c in cited):
        return []
    return [p for p in parts if not p.get("covered")]


def look_prompt(finding: str, label: str, ask: str, source: Cited) -> str:
    """Ask one document one question, with the document whole.

    Whole is the point. Reading a window is what produced the defect this
    exists for: the material was in the document and outside the window that
    was extracted for some other claim.

    `finding` says what a review concluded and `ask` is the thing to look
    for, under the heading `label`. Everything after that — read it in full,
    quote verbatim or answer none, reply as this object — is the same for
    every caller, and is the half that must not be written twice: a second
    copy that drifts into summarising, or into a different reply shape,
    fails quietly in the direction of finding things that are not there.
    """
    return (
        f"{finding}\n\n"
        f"{label}: {ask.strip()}\n\n"
        "Read the document below in full and say whether it contains a "
        "passage that states that. Quote verbatim or answer none; do not "
        "summarise, and do not reason from what the document implies.\n\n"
        'Reply ONLY JSON: {"found": true|false, "line_from": <int>, '
        '"line_to": <int>, "quote": "<verbatim>"}\n\n'
        f"DOCUMENT: {source.filename}\n{source.text}"
    )


def reread_prompt(part: dict, source: Cited) -> str:
    """Ask one CITED document whether it addresses a part called uncovered."""
    return look_prompt(
        "A review of an answer concluded that this part of the question is "
        "not addressed. The answer cites the document below.",
        "PART", str(part.get("asks") or ""), source)


def standing_prompt(proposition: str, source: Cited) -> str:
    """Ask one HIGHER-STANDING document whether it carries a proposition.

    The mirror image of the re-read above and the same mechanism, pointed the
    other way. There, an answer's own sources are opened again because the
    material may sit outside the window that was extracted. Here, a document
    the answer did NOT cite is opened for the same reason: extraction reads
    per planned claim from that claim's anchors, so the drafter resting a
    general proposition on a lower-standing source may never have been shown
    the higher-standing one at all. An objection refused by a drafter that
    was never shown the passage is not an argument, it is an accident.
    """
    return look_prompt(
        "A review of an answer concluded that this general proposition rests "
        "on a source that stands lower than one retrieved beside it. The "
        "document below is the higher-standing one.",
        "PROPOSITION", proposition, source)


def owed_prompt(point: str, source: Cited) -> str:
    """Ask one document the review named as UNUSED whether it carries a point.

    The third caller of the same look, and the one that was missing. When the
    review finds a retrieved document that settles something the answer left
    thin, the engine records it as owed and puts it to the drafter: cite it,
    waive it "with a rationale you can only give after reading its passages",
    or refuse it. Nothing opened the document. Extraction reads per planned
    claim from that claim's anchors, so a document the plan never pointed at
    has no passages at all — and a drafter with nothing verbatim to quote can
    only refuse, whatever the document actually says.

    Measured against an expert's review: her findings name the missing
    material by its rank in the retrieved set — 11, 26, 31, 33, 41, 51. It
    was retrieved every time. It was never read.
    """
    return look_prompt(
        "A review of an answer named the document below as one the answer "
        "should have used and did not.",
        "POINT IT IS SAID TO CARRY", point, source)


def apply_reread(
    parts: list[dict],
    found: dict[int, dict | None],
    cited: set[str] | None = None,
) -> tuple[list[dict], int]:
    """Fold the re-read's results back into the parts. Pure.

    `found` maps a part's index to what was found for it, or None where
    nothing was. A part that gains a passage stops being uncovered and records
    where it came from, so the finding is checkable rather than asserted. A
    part that gains nothing stays uncovered and records that it was looked
    for, which is the difference between an absent limb and an unchecked one.
    """
    out = [dict(p) for p in parts]
    hits = 0
    for i, hit in found.items():
        if not 0 <= i < len(out):
            continue
        if out[i].get("covered"):
            # Never touched. This runs only for parts the gate called
            # uncovered, and a covered part arriving here means the caller
            # got its indices wrong, which must not silently rewrite a verdict.
            continue
        if hit is None:
            out[i]["reread"] = "no passage in any cited source addresses it"
            continue
        filename = str(hit.get("filename") or "")
        if cited is not None and filename not in cited:
            raise ValueError(
                f"re-read returned {filename!r}, which the answer has not "
                "cited; a re-read may not reach outside the citations")
        out[i]["covered"] = True
        out[i]["gap"] = ""
        out[i]["reread_from"] = {
            "filename": filename,
            "line_from": hit.get("line_from"),
            "line_to": hit.get("line_to"),
            "quote": str(hit.get("quote") or "")[:2000],
        }
        hits += 1
    return out, hits
