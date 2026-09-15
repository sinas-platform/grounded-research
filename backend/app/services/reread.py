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


def reread_prompt(part: dict, source: Cited) -> str:
    """Ask one document about one part, with the document whole.

    Whole is the point. Reading a window is what produced the defect this
    exists for: the material was in the document and outside the window that
    was extracted for some other claim.
    """
    return (
        "A review of an answer concluded that this part of the question is "
        "not addressed:\n\n"
        f"PART: {str(part.get('asks') or '').strip()}\n\n"
        "The answer cites the document below. Read it in full and say whether "
        "it contains a passage that addresses that part. Quote verbatim or "
        "answer none; do not summarise, and do not reason from what the "
        "document implies.\n\n"
        'Reply ONLY JSON: {"found": true|false, "line_from": <int>, '
        '"line_to": <int>, "quote": "<verbatim>"}\n\n'
        f"DOCUMENT: {source.filename}\n{source.text}"
    )


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
