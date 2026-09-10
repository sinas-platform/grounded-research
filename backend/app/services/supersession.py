"""Say so when an answer cites an authority the corpus records as superseded.

The reviewer's must-have is to cite the leading authority together with the
most recent decision confirming it, and never to rely on superseded case law
without flagging it. This is the third of those and only the third: nothing
here decides what the leading authority is, and nothing here reads dates.

WHAT IT DOES NOT DO, AND WHY. The obvious wider version is to flag a cited
decision older than another cited decision on the same point, which would fire
constantly and be wrong most of the time: foundational authority is old and not
superseded, and a reader told an authority is superseded will not cite it. On
this question a flag that is usually wrong is worse than no flag, so this fires
only where the corpus actually records the supersession.

THE FILTER IS THE FEATURE. Measured on 10 September 2026, the `supersedes`
relationship holds 188 edges over 149 targets, and those targets reach 21,327
mentions. One target is an entity whose name is the bare word "Decision",
carrying 19,311 of them, and two more are a court and a pair of legal
instruments rather than decisions at all. Reading the relationship without a
filter therefore reports nearly twenty thousand mentions of superseded
authority on the strength of a generic string that became an entity. So a
target counts only where its name carries an identifier the deployment
declares for its class, which is the same machinery the naming checks use and
lives in the same place: the deployment's package, not here.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import text

from app.db import AsyncSessionLocal

log = logging.getLogger("sgr.supersession")

#: At most this many in one answer's feedback. The point is to name the
#: problem, not to inventory it.
MAX_FINDINGS = 5

_CITED_SUPERSEDED = text("""
    WITH ft AS (
        SELECT r.source_id AS doc_id, r.target_id AS ent_id
        FROM relationship r
        JOIN relationship_definition rd ON rd.id = r.relationship_definition_id
        WHERE rd.name IN ('is_full_text_of', 'is_full_text_of_court')
    )
    SELECT DISTINCT d.filename, te.canonical_form AS superseded,
           se.canonical_form AS superseding, dc.identifier_pattern
    FROM claim_evidence ce
    JOIN answer_claim ac ON ac.id = ce.claim_id
    JOIN document d ON d.id = ce.document_id
    LEFT JOIN document_class dc ON dc.id = d.document_class_id
    JOIN ft ON ft.doc_id = d.id
    JOIN relationship r ON r.target_id = ft.ent_id
    JOIN relationship_definition rd ON rd.id = r.relationship_definition_id
                                   AND rd.name = 'supersedes'
    JOIN entity te ON te.id = r.target_id
    JOIN entity se ON se.id = r.source_id
    WHERE ac.answer_id = :a
""")


def identified(rows: list[dict]) -> list[dict]:
    """Keep the rows whose superseded authority is named by an identifier.

    A supersession edge is only worth telling a reader about if the thing
    superseded can be recognised. "Brown Shoe Co. v. United States" can;
    "Decision", "2014 Decision" and "2002 decision" cannot, and those are real
    entries in this relationship carrying most of its weight.

    The test is the class's own `identifier_pattern`, so what counts as a
    recognisable name is declared by the deployment rather than guessed here.
    A row whose class declares no pattern is dropped: not knowing what an
    identifier looks like is not a reason to assume a name is one.
    """
    kept = []
    for row in rows:
        pattern = row.get("identifier_pattern")
        name = str(row.get("superseded") or "")
        if not pattern or not name:
            continue
        try:
            if re.search(pattern, name):
                kept.append(row)
        except re.error:
            log.warning("class identifier_pattern does not compile: %r", pattern)
    return kept


def message(row: dict) -> str:
    """One finding, in the words the reviser is asked to act on."""
    return (
        f"The answer cites {row['filename']}, which the collection records as "
        f"superseded by {row['superseding']}. Say so where the claim relies on "
        f"it, or cite the later authority instead. This is what the collection "
        f"records, not a judgement that the point is no longer good law."
    )


async def findings_for(answer_id: uuid.UUID) -> list[dict]:
    """Cited authorities recorded as superseded, identifier-bearing only."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_CITED_SUPERSEDED, {"a": answer_id})).mappings().all()
    return identified([dict(r) for r in rows])


async def issues_for(answer_id: uuid.UUID) -> list[str]:
    """Feedback strings for the gate's `issues` list.

    Never raises into the caller: an answer that is otherwise publishable must
    not be held up because this failed.
    """
    try:
        found = await findings_for(answer_id)
    except Exception:
        # Said here rather than only in the log. A check that crashed returns
        # nothing, and nothing is what a clean answer returns, so swallowing
        # the failure makes a broken check indistinguishable from a passing
        # one.
        log.exception("supersession check failed for answer %s", answer_id)
        return [
            "The check that a cited authority is not recorded as superseded "
            "did not run on this answer: it failed. No citation was examined. "
            "This is not a finding of none."
        ]
    return [message(f) for f in found[:MAX_FINDINGS]]
