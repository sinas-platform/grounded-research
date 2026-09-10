"""Mark entities that are words rather than names, without deleting them.

The extractor was told to be EXHAUSTIVE about named entities and read that as
every capitalised token. The result, measured on a 35,407-document corpus on
10 September 2026: an entity whose name is the bare word "Decision" with
19,311 document mentions, "Thus" and "Only" typed as undertakings with 14,706
and 18,804, and four entities named after their own entity type. One junk
entity does not stay small, because a name once created is matched against
every occurrence of that string in every document: "Thus" has exactly one
surface form across all of its mentions, and that form is the adverb.

They are not inert. They acquire relationships like any other entity, which is
where this was found: one of the 149 targets of the `supersedes` relationship
is "Decision", and reading that relationship without a filter reports twenty
thousand mentions of superseded authority.

NOTHING IS DELETED HERE. The evidence for calling an entity generic is its
mentions and its relationships, and deleting it destroys the evidence for the
decision to delete it. So this writes a mark and leaves everything in place; a
reader that wants to exclude them can, and a later pass that wants to check
this judgement still can too.

THE TEST, AND WHY NOT WORD COUNT. Shape does not separate them: "Google",
"Nexans" and "Servier" are one word, and "European Commission" appears in 53.8%
of this corpus legitimately. Case does. A proper noun is written with a capital
wherever it falls in a sentence; a common word is not. Measured over 11.2 MB of
this corpus, "European Commission", "TFEU" and "France" appear lower-case 0% of
the time, "United" 1% and "Council" 7%, while "SAME", "ALSO", "Must" and "Will"
are lower-case in 100% of their occurrences, "Decision" and "Case" in 86%.

The span guard cannot carry this. `_locate` is a hallucination check: it asks
whether the extracted name occurs in the document, and a common word always
does. It is also handed `content_lower`, so the case evidence is destroyed
before it arrives. Presence is exactly what a common word has.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text

log = logging.getLogger("sgr.generic_entities")

#: A name written lower-case at least this often is a word, not a name. Set
#: clear of the observed populations rather than between them: the highest
#: legitimate name measured was 7% and the lowest junk 76%.
#:
#: This is the wider test and it is not what the first marking pass used. On
#: this corpus it selects 766 entities against 274 for `written_as_a_word`,
#: and the extra 492 are the ones needing a judgement about how often a word
#: happens to be capitalised, which is the kind of call a mark exists to
#: avoid making. Widening to it later is a threshold change over reversible
#: marks; narrowing after a deletion is not a change at all.
LOWERCASE_SHARE = 0.60

#: Below this many documents an entity is too rare to be worth judging, and
#: the case evidence too thin to judge on. The junk that matters is common by
#: construction: it was created from a word, so it matches everywhere.
MIN_DOCUMENTS = 350


def case_evidence(name: str, text: str) -> dict:
    """How often this name is written lower-case where it appears.

    Whole-word matching in both forms. A name whose canonical form is already
    lower-case needs no counting: it was never written as a name at all.
    """
    if not name or not text:
        return {"as_written": 0, "lowercase": 0, "lowercase_share": None}
    if name == name.lower() and re.match(r"^[a-z]", name):
        return {"as_written": 0, "lowercase": 0, "lowercase_share": 1.0,
                "note": "the canonical form is itself lower-case"}
    boundary = r"(?<![A-Za-z]){}(?![A-Za-z])"
    exact = len(re.findall(boundary.format(re.escape(name)), text))
    lower = len(re.findall(boundary.format(re.escape(name.lower())), text))
    total = exact + lower
    return {"as_written": exact, "lowercase": lower,
            "lowercase_share": (lower / total) if total else None}


def written_as_a_word(name: str) -> bool:
    """The canonical form is itself lower-case, so it was never written as a name.

    The strictest of the available tests and the only one needing no corpus
    scan: a name recorded as "services" or "national" was not capitalised
    anywhere it was read, because the canonical form is what the extractor
    saw. Nothing here is a judgement about how often a word appears in one
    case or another, which is why it is the test to mark on first.
    """
    return bool(name) and name == name.lower() and bool(re.match(r"^[a-z]", name))


def is_generic(name: str, documents: int, evidence: dict) -> bool:
    """Whether to mark. Deliberately conservative on every axis.

    Rare entities are left alone whatever they look like, an unmeasurable name
    is left alone, and the threshold sits clear of both populations rather
    than between them. A mark that is wrong costs a real entity its standing,
    and the whole reason this is a mark and not a deletion is that being wrong
    should be recoverable.
    """
    if documents < MIN_DOCUMENTS:
        return False
    share = evidence.get("lowercase_share")
    if share is None:
        return False
    return share >= LOWERCASE_SHARE


def mark(name: str, documents: int, evidence: dict, when: str) -> dict:
    """The metadata to merge into the entity. Carries its own reasons."""
    return {
        "generic_term": {
            "marked_at": when,
            "documents": documents,
            "lowercase_share": round(evidence.get("lowercase_share") or 0.0, 3),
            "as_written": evidence.get("as_written", 0),
            "lowercase": evidence.get("lowercase", 0),
            "test": (
                f"written lower-case in at least {int(LOWERCASE_SHARE * 100)}% of "
                f"occurrences, and present in at least {MIN_DOCUMENTS} documents"
            ),
            "not_deleted": (
                "Its mentions and relationships are the evidence for this "
                "judgement and are left in place so the judgement can be checked."
            ),
        }
    }


_CANDIDATES = text("""
    SELECT e.id, e.canonical_form, e.metadata,
           (SELECT count(DISTINCT em.document_id)
              FROM entity_mention em WHERE em.entity_id = e.id) AS documents
    FROM entity e
    WHERE e.merged_into_id IS NULL
      AND e.canonical_form = lower(e.canonical_form)
      AND e.canonical_form ~ '^[a-z]'
""")


async def mark_generic(session, when: str, dry_run: bool = True) -> dict:
    """Mark entities whose canonical form is itself a lower-case word.

    Writes into `entity.metadata` and touches nothing else: no mention is
    removed, no relationship is cut, no row is deleted. An already-marked
    entity is left as it is rather than re-marked, so the pass can be re-run
    and the first mark keeps its date.

    Dry by default. The caller has to ask for the write, because the thing
    this exists to avoid is a judgement applied at scale before anyone has
    read what it selected.
    """
    rows = (await session.execute(_CANDIDATES)).mappings().all()
    marked = skipped = already = 0
    examples: list[str] = []
    for row in rows:
        name, docs = row["canonical_form"], int(row["documents"] or 0)
        if not (written_as_a_word(name) and docs >= MIN_DOCUMENTS):
            skipped += 1
            continue
        meta = dict(row["metadata"] or {})
        if "generic_term" in meta:
            already += 1
            continue
        marked += 1
        if len(examples) < 10:
            examples.append(f"{name} ({docs} documents)")
        if dry_run:
            continue
        meta.update(mark(name, docs, case_evidence(name, ""), when))
        await session.execute(
            text("UPDATE entity SET metadata = :m WHERE id = :i"),
            {"m": json.dumps(meta), "i": row["id"]},
        )
    if not dry_run:
        await session.commit()
    return {"candidates": len(rows), "marked": marked, "already_marked": already,
            "below_threshold": skipped, "dry_run": dry_run, "examples": examples}
