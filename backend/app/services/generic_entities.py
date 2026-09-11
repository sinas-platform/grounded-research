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

#: Types the test cannot read, excluded as a rule rather than one entity at a
#: time. A relevant market is named by a common noun phrase because that is
#: what a market is: "retail market", "upstream market", "resale price
#: maintenance" are lower-case for the same reason "services" is, and the test
#: cannot tell a correctly recorded market from a mistakenly recorded one. On
#: this corpus 229 of the 274 entities the strict test selects are of this
#: type, so applying it here would be 229 individual judgements wearing the
#: clothes of a rule. If Relevant Market carries junk, and it may, it needs a
#: test that knows what a market is. This one does not.
EXCLUDED_TYPES = frozenset({"Relevant Market"})

#: Below this many documents an entity is too rare to be worth judging, and
#: the case evidence too thin to judge on. The junk that matters is common by
#: construction: it was created from a word, so it matches everywhere.
MIN_DOCUMENTS = 350


def case_evidence(name: str, text: str) -> dict:
    """How often this name is written lower-case where it appears.

    The denominator is every casing of the word (case-insensitive whole-word
    count), not just the canonical form and the lower-case form — an
    all-caps variant in a heading would otherwise vanish from the total and
    inflate the share.
    """
    if not name or not text:
        return {"as_written": 0, "lowercase": 0, "lowercase_share": None}
    if name == name.lower() and re.match(r"^[a-z]", name):
        return {"as_written": 0, "lowercase": 0, "lowercase_share": 1.0,
                "note": "the canonical form is itself lower-case"}
    boundary = r"(?<![A-Za-z]){}(?![A-Za-z])"
    pat = boundary.format(re.escape(name))
    any_case = len(re.findall(f"(?i){pat}", text))
    lowercase = len(re.findall(boundary.format(re.escape(name.lower())), text))
    as_written = len(re.findall(pat, text))
    if any_case == 0:
        return {"as_written": 0, "lowercase": 0, "lowercase_share": None}
    return {"as_written": as_written, "lowercase": lowercase,
            "any_case": any_case,
            "lowercase_share": round(lowercase / any_case, 4)}


def carries_an_identifier(name: str, patterns: list[str]) -> bool:
    """Whether the name carries a reference its deployment declares.

    Case alone does not separate a name from a word on a corpus that is partly
    French. In English a proper noun is capitalised wherever it falls, so
    lower case is evidence; in French legal nomenclature lower case is the
    convention, and `ordonnance n° 86-1243 du 1er décembre 1986` is as
    specific as `Regulation No 1/2003` and as correctly written. It appears in
    1,411 documents and is not a word.

    An identifier is the signal that survives the language. The patterns come
    from `document_class.identifier_pattern`, declared by the deployment for
    its own classes, so this asks nothing about French or English and nothing
    about competition law. `paragraph 49` and `décision attaquée` carry no
    identifier in any language and stay marked.
    """
    if not name:
        return False
    for pattern in patterns or ():
        try:
            if re.search(pattern, name):
                return True
        except re.error:
            log.warning("class identifier_pattern does not compile: %r", pattern)
    return False


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
    SELECT e.id, e.canonical_form, e.metadata, et.name AS entity_type,
           (SELECT count(DISTINCT em.document_id)
              FROM entity_mention em WHERE em.entity_id = e.id) AS documents
    FROM entity e
    JOIN entity_type et ON et.id = e.entity_type_id
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
    patterns = [p for (p,) in (await session.execute(text(
        "SELECT identifier_pattern FROM document_class "
        "WHERE identifier_pattern IS NOT NULL AND identifier_pattern <> ''"
    ))).all()]
    rows = (await session.execute(_CANDIDATES)).mappings().all()
    marked = skipped = already = excluded = identified = 0
    examples: list[str] = []
    for row in rows:
        name, docs = row["canonical_form"], int(row["documents"] or 0)
        if row["entity_type"] in EXCLUDED_TYPES:
            excluded += 1
            continue
        if not (written_as_a_word(name) and docs >= MIN_DOCUMENTS):
            skipped += 1
            continue
        if carries_an_identifier(name, patterns):
            identified += 1
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
            "below_threshold": skipped, "excluded_by_type": excluded,
            "spared_carries_identifier": identified,
            "patterns_declared": len(patterns),
            "dry_run": dry_run, "examples": examples}


_TIER2_CANDIDATES = text("""
    SELECT e.id, e.canonical_form, e.metadata, et.name AS entity_type,
           (SELECT count(DISTINCT em.document_id)
              FROM entity_mention em WHERE em.entity_id = e.id) AS documents
    FROM entity e
    JOIN entity_type et ON et.id = e.entity_type_id
    WHERE e.merged_into_id IS NULL
      AND e.canonical_form ~ '^[A-Za-z]+$'
      AND e.canonical_form <> lower(e.canonical_form)
""")

_SAMPLE_TEXT = text("""
    SELECT dv.content_md FROM document d
    JOIN document_version dv ON dv.id = d.current_version_id
    WHERE d.duplicate_of_id IS NULL AND d.staged IS NOT TRUE
      AND dv.content_md IS NOT NULL
    ORDER BY md5(d.id::text)
    LIMIT :n
""")


async def mark_generic_by_case(
    session, when: str, dry_run: bool = True,
    sample_documents: int = 400, min_occurrences: int = 25,
) -> dict:
    """The second tier: a capitalised name the corpus writes as a word.

    The first pass marks canonical forms that are already lower-case — the
    strictest test, needing no corpus scan. It structurally cannot reach the
    worst offenders, whose canonical form is capitalised while the corpus
    writes the word lower-case everywhere: an undertaking named `Thus` with
    14,704 blind mentions is invisible to it.

    So this tier brings the corpus: one deterministic sample of in-service
    documents (md5-ordered, so re-runs read the same sample), one case
    census over it, and the same `is_generic` judgement the first tier uses
    — a candidate is marked when the corpus writes it lower-case at least
    ``LOWERCASE_SHARE`` of the time. Single-word candidates only: a
    multi-word name is capitalised or not per word, and no share describes
    it. A word the sample carries fewer than ``min_occurrences`` times is
    left unmeasured rather than judged on noise — absence over arbitrary,
    as everywhere else in this module.

    Same contract as ``mark_generic``: dry by default, a mark merges into
    ``entity.metadata``, an already-marked entity keeps its first mark, and
    nothing is deleted.
    """
    patterns = [p for (p,) in (await session.execute(text(
        "SELECT identifier_pattern FROM document_class "
        "WHERE identifier_pattern IS NOT NULL AND identifier_pattern <> ''"
    ))).all()]
    sample = "\n".join(t for (t,) in (await session.execute(
        _SAMPLE_TEXT, {"n": sample_documents})).all())
    rows = (await session.execute(_TIER2_CANDIDATES)).mappings().all()
    marked = skipped = already = excluded = identified = unmeasured = 0
    examples: list[str] = []
    for row in rows:
        name, docs = row["canonical_form"], int(row["documents"] or 0)
        if row["entity_type"] in EXCLUDED_TYPES:
            excluded += 1
            continue
        if docs < MIN_DOCUMENTS:
            skipped += 1
            continue
        if carries_an_identifier(name, patterns):
            identified += 1
            continue
        meta = dict(row["metadata"] or {})
        if "generic_term" in meta:
            already += 1
            continue
        evidence = case_evidence(name, sample)
        # Every casing counts toward the floor, matching the share's own
        # denominator: 20 lower-case plus 10 all-caps is 30 observations.
        seen = int(evidence.get("any_case") or 0)
        if seen < min_occurrences:
            unmeasured += 1
            continue
        evidence["sampled_documents"] = sample_documents
        if not is_generic(name, docs, evidence):
            skipped += 1
            continue
        marked += 1
        if len(examples) < 10:
            examples.append(
                f"{name} ({docs} documents, "
                f"{evidence['lowercase_share']:.0%} lower-case)")
        if dry_run:
            continue
        meta.update(mark(name, docs, evidence, when))
        await session.execute(
            text("UPDATE entity SET metadata = :m WHERE id = :i"),
            {"m": json.dumps(meta), "i": row["id"]},
        )
    if not dry_run:
        await session.commit()
    return {"candidates": len(rows), "marked": marked, "already_marked": already,
            "below_threshold": skipped, "excluded_by_type": excluded,
            "spared_carries_identifier": identified, "unmeasured": unmeasured,
            "patterns_declared": len(patterns), "dry_run": dry_run,
            "examples": examples}


# The tiers that find a string without anything having recognised the
# entity: the gazetteer's corpus scan and the pre-tiering legacy rows. A
# NULL link_method is treated as blind too — unknown provenance is not
# recognition. Everything else (created, adjudicated, natural_key, exact,
# alias, and any manual tier added later) counts as recognition, so an
# entity matched by its case number is never misread as unrecognised.
BLIND_LINK_METHODS = ("gazetteer", "legacy")

# Below this share of context-validated mentions, a widely-matched name is
# functioning as a word: the extractor that reads documents almost never
# recognises the thing the string matcher keeps finding. Measured poles on
# the working corpus: "Thus" 2/14,704 (0.01%), real undertakings near 100%.
# The floor sits far from both.
LINK_PROBABILITY_FLOOR = 0.02

# A ratio alone can wrong a famous real entity: the gazetteer pre-links
# heavily for everything, junk and giants alike, so a widely-cited real
# party's share is diluted too. What dilution cannot fake is the absolute
# count — a real entity the corpus keeps recognising accumulates hundreds
# of recognised mentions whatever its ratio, while a word accumulates a
# handful ("Thus": 2). Both conditions must hold to mark.
#
# The gray zone is irreducible: an entity with a starved ratio AND a
# starved count could still, rarely, be real. No threshold can decide
# that — which is why every marking pass in this module is dry by
# default, prints what it selected, and writes reversible metadata. The
# dry-run review is the judgement; these numbers only draw the shortlist.
RECOGNISED_CEILING = 20

_MENTION_TIERS = text("""
    SELECT e.id, e.canonical_form, e.metadata, et.name AS entity_type,
           count(DISTINCT em.document_id) AS documents,
           count(DISTINCT em.document_id)
               FILTER (WHERE em.link_method IS NOT NULL
                       AND NOT (em.link_method = ANY(:blind))) AS validated
    FROM entity e
    JOIN entity_type et ON et.id = e.entity_type_id
    JOIN entity_mention em ON em.entity_id = e.id AND em.status = 'active'
    WHERE e.merged_into_id IS NULL
    GROUP BY e.id, e.canonical_form, e.metadata, et.name
    HAVING count(DISTINCT em.document_id) >= :min_documents
""")


def improbable_link(documents: int, validated: int) -> bool:
    """Whether the mention population says word, not name.

    Language-free and orthography-free, which the capitalisation tiers are
    not: German capitalises every noun, French legal nomenclature is
    lower-case, and neither convention moves this ratio. It needs a large
    mention population to mean anything, which MIN_DOCUMENTS provides.
    """
    if documents < MIN_DOCUMENTS:
        return False
    return (validated <= RECOGNISED_CEILING
            and (validated / documents) < LINK_PROBABILITY_FLOOR)


async def mark_generic_by_link_probability(
    session, when: str, dry_run: bool = True,
    blind_methods: tuple[str, ...] = BLIND_LINK_METHODS,
) -> dict:
    """Mark entities the corpus matches everywhere and recognises nowhere.

    The primary signal, of which the capitalisation tiers are corroborators:
    of all the documents a name was string-matched in, in what share did an
    extractor that actually read the document recognise the entity? A name
    carries its own evidence trail here — no corpus sample, no orthography.

    Same contract as the other passes: dry by default, marks merge into
    ``entity.metadata``, the first mark keeps its date, nothing is deleted.
    """
    rows = (await session.execute(_MENTION_TIERS, {
        "blind": list(blind_methods),
        "min_documents": MIN_DOCUMENTS})).mappings().all()
    marked = skipped = already = excluded = 0
    examples: list[str] = []
    for row in rows:
        docs, validated = int(row["documents"]), int(row["validated"] or 0)
        if row["entity_type"] in EXCLUDED_TYPES:
            excluded += 1
            continue
        meta = dict(row["metadata"] or {})
        if "generic_term" in meta:
            already += 1
            continue
        if not improbable_link(docs, validated):
            skipped += 1
            continue
        evidence = {"documents": docs, "validated_documents": validated,
                    "link_probability": round(validated / docs, 5)}
        marked += 1
        if len(examples) < 10:
            examples.append(f"{row['canonical_form']} "
                            f"({validated}/{docs} recognised)")
        if dry_run:
            continue
        meta.update(mark(row["canonical_form"], docs, evidence, when))
        await session.execute(
            text("UPDATE entity SET metadata = :m WHERE id = :i"),
            {"m": json.dumps(meta), "i": row["id"]},
        )
    if not dry_run:
        await session.commit()
    return {"candidates": len(rows), "marked": marked, "already_marked": already,
            "below_floor_or_spared": skipped, "excluded_by_type": excluded,
            "dry_run": dry_run, "examples": examples}
