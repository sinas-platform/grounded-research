"""Say so when an answer cites a source the collection records as superseded.

A reader must not be left resting on a source the collection itself records as
replaced. This check is that and only that: nothing here decides which source
is the leading one, and nothing here reads dates.

WHAT IT DOES NOT DO, AND WHY. The obvious wider version is to flag a cited
source older than another cited source on the same point, which would fire
constantly and be wrong most of the time: a foundational source is old and not
superseded, and a reader told a source is superseded will not cite it. On this
question a flag that is usually wrong is worse than no flag, so this fires only
where the collection actually records the supersession.

WHICH EDGES ARE READ IS DECLARED, NOT GUESSED. Both relationships this needs —
the one saying a document is the full text of what it records, and the one
saying a source replaced another — are found through the roles the deployment
declares in its package (`spec.relationship_roles`), never by name. The names
matched here before, and a deployment naming its relations anything else got no
findings, which is byte-identical to a collection that records no supersession.
See `app.services.relationship_roles`.

THE FILTER IS THE FEATURE. Measured on 10 September 2026, the supersession
relationship of one collection held 188 edges over 149 targets, and those
targets reached 21,327 mentions. One target was an entity whose name is a bare
common noun, carrying 19,311 of them, and two more were not sources of the kind
the edge is for at all. Reading the relationship without a filter therefore
reports nearly twenty thousand mentions of superseded authority on the strength
of a generic string that became an entity. So a target counts only where its
name carries an identifier the deployment declares for its class, which is the
same machinery the naming checks use and lives in the same place: the
deployment's package, not here.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.services.relationship_roles import (
    full_text_definition_ids,
    supersession_definition_ids,
)

log = logging.getLogger("sgr.supersession")

#: At most this many in one answer's feedback. The point is to name the
#: problem, not to inventory it.
MAX_FINDINGS = 5

# Both joins read only edges that count as active. `relationship_state`
# carries `counts_as_active`, and a rejected or withdrawn assertion keeps its
# row: reading the edge without the state tells a reader an authority is
# superseded on the strength of an assertion the graph has already refused. A
# NULL `current_state_id` is active, which is the convention the annotation
# walker uses in three places and is not re-decided here.
_CITED_SUPERSEDED = text("""
    WITH ft AS (
        SELECT r.source_id AS doc_id, r.target_id AS ent_id
        FROM relationship r
        JOIN relationship_definition rd ON rd.id = r.relationship_definition_id
        LEFT JOIN relationship_state rs ON rs.id = r.current_state_id
        WHERE rd.id = ANY(:full_text_defs)
          AND (r.current_state_id IS NULL OR rs.counts_as_active IS TRUE)
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
                                   AND rd.id = ANY(:supersession_defs)
    JOIN entity te ON te.id = r.target_id
    JOIN entity se ON se.id = r.source_id
    LEFT JOIN relationship_state srs ON srs.id = r.current_state_id
    WHERE ac.answer_id = :a
      AND (r.current_state_id IS NULL OR srs.counts_as_active IS TRUE)
""")


def identified(rows: list[dict]) -> list[dict]:
    """Keep the rows whose superseded authority is named by an identifier.

    A supersession edge is only worth telling a reader about if the thing
    superseded can be recognised. "Case T-451/20 Kestrel Holdings v Northmoor Authority" can;
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
        f"it, or cite the later source instead. This is what the collection "
        f"records, not a judgement that the point no longer holds."
    )


async def findings_for(answer_id: uuid.UUID) -> list[dict]:
    """Cited sources recorded as superseded, identifier-bearing only.

    Returns nothing, without asking the database anything, when the deployment
    declares no supersession role — the reader of that role says so in the log
    rather than letting an empty result pass for a clean answer.
    """
    async with AsyncSessionLocal() as session:
        supersession_defs = await supersession_definition_ids(session)
        if not supersession_defs:
            return []
        full_text_defs = await full_text_definition_ids(session)
        if not full_text_defs:
            return []
        rows = (await session.execute(_CITED_SUPERSEDED, {
            "a": answer_id,
            "supersession_defs": supersession_defs,
            "full_text_defs": full_text_defs,
        })).mappings().all()
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
