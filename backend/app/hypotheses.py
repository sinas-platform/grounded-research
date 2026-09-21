"""Hypothesis-first retrieval: the plan is what the answer might say.

The planner used to name entities and write search queries, and two runs
of one question at temperature zero named different entities and wrote
different queries, so the same question retrieved a different set every
time — measured at 0% shared queries and half-shared documents. What IS
stable in meaning across runs is what the question asks: the issues it
contains and the rule, principle or finding expected to govern each.
Measured on two independent samples of one question: the same four rules
in different words.

So the plan is hypotheses. Two independent samples of them, merged, one
call each (a second sample raised the shared top-30 from about 21 to 24 of
30 and the shared top-10 from 5 to 8, measured on 14,511 documents). Each
rule then runs the same deterministic searches: its words against the
propositions documents were found to carry (`services/propositions`), its
words against the full text, and a walk of the graph from the source it
names. The question's own words are one more list. The lists are fused by
reciprocal rank, so a document that serves several rules rises above one
that serves one, and no channel needs a hand-tuned weight. Then the graph
is walked once more from the fused top, so the documents that cite or are
cited by what was found come in whatever the planner happened to name.

Measured on the slice, two questions, four runs each: every document a
review of published answers had asked for inside the top 15 for one
question and seven of nine inside the top 30 for the other, in every run.
On the whole collection (144,200 documents), one question, twelve rules:
six of seven inside the top 41, the seventh outside the top 100 — named by
a rule, resolved, first in that rule's graph list, and in no other list
inside 30, so one reciprocal rank of 1/61 against documents mid-list in
many. That is the open measurement.

Nothing here knows a domain: the prompt asks for issues, rules and the
source each is expected to sit in, and the collection's own map and the
deployment's guidance say what those look like here.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections import defaultdict

from sqlalchemy import text

_log = logging.getLogger("sgr.hypotheses")

HYPOTHESES_PROMPT = """You are preparing a {domain}research answer against a collection with this schema:

{corpus_map}
{guidance}
Before anything is retrieved, state your hypotheses. Reply ONLY JSON:
{{"issues": [{{"issue": "<one question the question contains, in a sentence>",
             "rules": [{{"rule": "<the rule, principle or finding you expect governs it, one sentence, in the language this collection works in>",
                        "source": {{"identifier": "<how this collection cites the source you expect carries it, exactly, or empty if unsure>", "title": "<its name>"}}}}]}}]}}

Name every issue the question contains, including the ones it only implies. Hypotheses may be wrong; that is what retrieval is for.

QUESTION: {question}"""

#: The reply must carry issues; an empty list is an answer (a question
#: with no issue in it), an object without the key is not.
HYPOTHESES_GROUPS = (("issues",),)

#: Independent hypothesis samples merged before retrieval. Two, because the
#: second one bought the measured gain above and a third is another call.
SAMPLES = 2

#: Reciprocal-rank fusion constant: the standard 60, which makes rank 1
#: worth 1/61 and rank 61 half that. Not tuned here.
RRF_K = 60
#: How deep each channel's list goes before fusion.
CHANNEL_TOP = 300
#: The summary channel is a fallback for a document whose propositions
#: were never extracted, so it enters below the others: measured, a
#: summary index lost to every other channel on the documents that matter.
SUMMARY_WEIGHT = 0.3
#: How many fused documents the graph walks out from.
CORE = 10
#: An entity in more documents than this links everything to everything;
#: it is left out of the walk for cost, not for meaning.
UBIQUITY_CAP = 5000
#: How many documents the full-text channel ranks per rule. Candidates are
#: chosen through the index alone, by how many of the rule's rare words a
#: document carries; only these have their text read and ranked on all of
#: the rule's words. Measured on 144,200 documents under load: ranking every
#: document that carried any word of one rule read a gigabyte a second for
#: eleven minutes; this takes a minute, and the documents a review of
#: published answers had asked for stayed inside the candidates.
TEXT_CANDIDATES = 2000
#: How many PROPOSITIONS the proposition channel ranks per rule, chosen the
#: same way. A proposition is a sentence, so ranking one costs next to
#: nothing and the cut can be wide; the cost is in finding them, which the
#: width does not change. Measured with the cut at TEXT_CANDIDATES: two of
#: seven of the review's documents fell out of one rule's candidates.
PROPOSITION_CANDIDATES = 20000

# Postgres keeps, for an analysed tsvector column, the lexemes the most rows
# contain (`pg_stats.most_common_elems`; the freqs array carries two extra
# trailing entries, the slice leaves them out). A word in that list carries
# no information about which document is meant, and it is a fact about the
# collection, not a word list of any language: `the` in an English one,
# `het` in a Dutch one, `decision` in a collection of decisions.
_COMMON_TERMS_SQL = """
    SELECT t.term
    FROM pg_stats s,
         LATERAL unnest(
             s.most_common_elems::text::text[],
             s.most_common_elem_freqs[1:array_length(s.most_common_elems::text::text[], 1)]
         ) AS t(term, freq)
    WHERE s.tablename = 'document_version' AND s.attname = 'content_tsvector'"""

_WORD = re.compile(r"\w+")


# ── pure ────────────────────────────────────────────────────────────────────


def rules_of(reply: dict) -> list[dict]:
    """The rules in one hypotheses reply, flat, each with its issue and the
    source it is expected to sit in. Anything that is not a rule with text
    is dropped."""
    out = []
    for issue in reply.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        for r in issue.get("rules") or []:
            if not isinstance(r, dict):
                continue
            rule = " ".join(str(r.get("rule") or "").split())
            if not rule:
                continue
            src = r.get("source") if isinstance(r.get("source"), dict) else {}
            out.append({"issue": " ".join(str(issue.get("issue") or "").split()),
                        "rule": rule,
                        "source": {"identifier": " ".join(str(src.get("identifier") or "").split()),
                                   "title": " ".join(str(src.get("title") or "").split())}})
    return out


def _fold(s: str) -> str:
    return " ".join(_WORD.findall(s.lower()))


def union_rules(samples: list[list[dict]]) -> list[dict]:
    """The rules of every sample, in sample order, one copy of each. Two
    samples writing the same rule in the same words is one rule; the same
    rule in different words is two, and both run — that is the point of
    a second sample."""
    seen: dict[str, dict] = {}
    for rules in samples:
        for r in rules:
            key = _fold(r["rule"])
            if key and key not in seen:
                seen[key] = r
    return list(seen.values())


def rare_words(terms: str, common: set[str]) -> str:
    """The words of a tsquery expression that are not common in the
    collection, as one expression of their own. Pure. Empty when every word
    is common, which is a rule that selects nothing on its own."""
    words = [w.strip() for w in terms.split("|") if w.strip()]
    return " | ".join(w for w in words if w not in common)


def rrf(lists: list[tuple[float, list[str]]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion of ranked lists, each with a weight. A document
    at rank r in a list of weight w adds w / (k + r). Ties break on the
    document id, so equal scores rank the same way every time."""
    scores: dict[str, float] = defaultdict(float)
    for w, lst in lists:
        for i, d in enumerate(lst):
            scores[d] += w / (k + i + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# ── the plan ────────────────────────────────────────────────────────────────


async def plan(question: str, effort: str = "medium",
               run_id: uuid.UUID | None = None) -> dict:
    """Hypotheses, sampled SAMPLES times and merged, each rule with the
    entities its named source resolves to. Raises when no sample produced
    a rule: a plan with nothing in it is not a plan."""
    from app import retrieval_first as rf
    from app.services.query_runner import _Sinas

    sinas = _Sinas(run_id=run_id)
    corpus_map, map_problem = await rf.build_corpus_map()
    guidance, guidance_skipped = await rf._retrieval_guidance()
    prompt = HYPOTHESES_PROMPT.format(
        corpus_map=corpus_map, question=question, domain=rf._domain_prefix(),
        guidance=guidance)
    samples = []
    for i in range(SAMPLES):
        reply = await rf._invoke_json(sinas, rf.PLAN_AGENT, prompt,
                                      HYPOTHESES_GROUPS, run_id, f"hypotheses {i + 1}")
        samples.append(rules_of(reply))
    rules = union_rules(samples)
    if not rules:
        raise ValueError("planning produced no hypotheses: nothing to retrieve with")
    anchor_names: dict[str, str] = {}
    for r in rules:
        matches = await rf._resolve_names([r["source"]]) if (
            r["source"].get("identifier") or r["source"].get("title")) else []
        marked = await rf._annotate_matches({m["id"]: m for m in matches})
        # A generic term names the word, not the thing: a rule whose source
        # resolves to one walks from nowhere rather than from the shelf that
        # contains the word.
        r["anchors"] = [eid for eid, m in marked.items() if not m.get("generic")]
        for eid in r["anchors"]:
            anchor_names[eid] = marked[eid]["value"]
    return {"hypotheses": rules,
            "samples": SAMPLES,
            "anchors": list(anchor_names),
            "anchor_names": anchor_names,
            # Kept for readers of a stored plan; this plan writes no free-form
            # queries and boosts no class — the rules are the queries.
            "queries": [],
            "class_boost": [],
            "guidance_skipped": guidance_skipped,
            "warnings": [map_problem] if map_problem else [],
            "question": question,
            "effort": effort}


# ── the channels ────────────────────────────────────────────────────────────


async def _proposition_words(s, terms: str, common: set[str], n: int) -> list[str]:
    """Two stages, as `_text_words`, but the candidates are PROPOSITIONS:
    the ones carrying the most of the rule's rare words. A document's
    propositions are each a sentence, and the one that matches a rule is
    the one carrying several of its words at once; counted per document
    instead, a judgment whose twenty propositions each carry one word of
    the rule outranks the one whose single proposition states the rule, and
    the cut lost the review's documents (measured: two of seven). The
    candidate propositions are then ranked on all the words and the best
    one speaks for its document."""
    if not terms:
        return []
    rare = rare_words(terms, common)
    if not rare:
        return [str(d) for d in (await s.execute(text("""
            WITH r AS (
                SELECT p.document_id AS did,
                       max(ts_rank(p.text_tsvector, to_tsquery('simple', :t), 1)) AS sc
                FROM proposition p JOIN document d ON d.id = p.document_id
                WHERE p.text_tsvector @@ to_tsquery('simple', :t)
                  AND d.staged IS NOT TRUE
                GROUP BY p.document_id)
            SELECT did FROM r ORDER BY sc DESC, did LIMIT :n"""),
            {"t": terms, "n": n})).scalars().all()]
    return [str(d) for d in (await s.execute(text("""
        WITH rare AS (SELECT unnest(string_to_array(:rare, ' | ')) AS w),
        hits AS (
            SELECT m.id, count(*) AS n
            FROM rare r
            JOIN LATERAL (
                SELECT p.id FROM proposition p
                WHERE p.text_tsvector @@ to_tsquery('simple', r.w)) m ON true
            GROUP BY m.id),
        cand AS (SELECT id FROM hits ORDER BY n DESC, id LIMIT :c),
        r AS (
            SELECT p.document_id AS did,
                   max(ts_rank(p.text_tsvector, to_tsquery('simple', :t), 1)) AS sc
            FROM cand c
            JOIN proposition p ON p.id = c.id
            JOIN document d ON d.id = p.document_id
            WHERE d.staged IS NOT TRUE
            GROUP BY p.document_id)
        SELECT did FROM r ORDER BY sc DESC, did LIMIT :n"""),
        {"rare": rare, "t": terms, "c": PROPOSITION_CANDIDATES, "n": n})).scalars().all()]


async def _common_terms(s) -> set[str]:
    return {t for (t,) in await s.execute(text(_COMMON_TERMS_SQL))}


async def _text_words(s, terms: str, common: set[str], n: int) -> list[str]:
    """Two stages. Candidates: the documents carrying the most of the rule's
    rare words, found through the index without reading a text. Rank: those
    candidates on all of the rule's words, length-normalised. A rule with
    no rare word selects nothing on its own and is ranked on every document
    it touches, which is the slow path and is rare."""
    if not terms:
        return []
    rare = rare_words(terms, common)
    if not rare:
        return [str(d) for d in (await s.execute(text("""
            SELECT d.id FROM document d
            JOIN document_version dv ON dv.id = d.current_version_id
            WHERE dv.content_tsvector @@ to_tsquery('simple', :t)
              AND d.staged IS NOT TRUE
            ORDER BY ts_rank(dv.content_tsvector, to_tsquery('simple', :t), 1) DESC, d.id
            LIMIT :n"""), {"t": terms, "n": n})).scalars().all()]
    return [str(d) for d in (await s.execute(text("""
        WITH rare AS (SELECT unnest(string_to_array(:rare, ' | ')) AS w),
        hits AS (
            SELECT m.id, count(*) AS n
            FROM rare r
            JOIN LATERAL (
                SELECT d.id FROM document d
                JOIN document_version dv ON dv.id = d.current_version_id
                WHERE dv.content_tsvector @@ to_tsquery('simple', r.w)
                  AND d.staged IS NOT TRUE) m ON true
            GROUP BY m.id),
        cand AS (SELECT id FROM hits ORDER BY n DESC, id LIMIT :c)
        SELECT d.id FROM cand c
        JOIN document d ON d.id = c.id
        JOIN document_version dv ON dv.id = d.current_version_id
        ORDER BY ts_rank(dv.content_tsvector, to_tsquery('simple', :t), 1) DESC, d.id
        LIMIT :n"""), {"rare": rare, "t": terms, "c": TEXT_CANDIDATES, "n": n})).scalars().all()]


async def _summary_words(s, terms: str, common: set[str], n: int) -> list[str]:
    """Two stages, as `_text_words`, over the summary: the expression here
    is the one the index is built on (migration 0053), word for word."""
    if not terms:
        return []
    rare = rare_words(terms, common)
    if not rare:
        return [str(d) for d in (await s.execute(text("""
            SELECT d.id FROM document d
            WHERE to_tsvector('simple', coalesce(d.summary, '')) @@ to_tsquery('simple', :t)
              AND d.staged IS NOT TRUE
            ORDER BY ts_rank(to_tsvector('simple', coalesce(d.summary, '')),
                             to_tsquery('simple', :t), 1) DESC, d.id
            LIMIT :n"""), {"t": terms, "n": n})).scalars().all()]
    return [str(d) for d in (await s.execute(text("""
        WITH rare AS (SELECT unnest(string_to_array(:rare, ' | ')) AS w),
        hits AS (
            SELECT m.id, count(*) AS n
            FROM rare r
            JOIN LATERAL (
                SELECT d.id FROM document d
                WHERE to_tsvector('simple', coalesce(d.summary, '')) @@ to_tsquery('simple', r.w)
                  AND d.staged IS NOT TRUE) m ON true
            GROUP BY m.id),
        cand AS (SELECT id FROM hits ORDER BY n DESC, id LIMIT :c)
        SELECT d.id FROM cand c JOIN document d ON d.id = c.id
        ORDER BY ts_rank(to_tsvector('simple', coalesce(d.summary, '')),
                         to_tsquery('simple', :t), 1) DESC, d.id
        LIMIT :n"""), {"rare": rare, "t": terms, "c": TEXT_CANDIDATES, "n": n})).scalars().all()]


async def _graph_from(s, anchors: list[str], depth: int, n: int) -> list[str]:
    """Documents about the anchors and what they are linked to, `depth` hops
    out: mentions weighted by how rare the entity is, relationship evidence
    by count. The walk that used to be the whole of retrieval, per rule."""
    from app.retrieval_first import BLIND_LINK_METHODS

    if not anchors:
        return []
    total_docs = (await s.execute(text("SELECT count(*) FROM document"))).scalar() or 1
    ln_n = math.log(total_docs + 1)
    scores: dict[str, float] = defaultdict(float)
    frontier, seen = set(anchors), set(anchors)
    for hop in range(depth):
        if not frontier:
            break
        w_mention, w_graph = 3.0 / (hop + 1) ** 2, 2.0 / (hop + 1) ** 2
        rows = (await s.execute(text("""
            SELECT m.document_id, m.entity_id, count(*), coalesce(st.documents, 0)
            FROM entity_mention m
            JOIN document d ON d.id = m.document_id
            LEFT JOIN entity_stats st ON st.entity_id = m.entity_id
            WHERE m.entity_id = ANY(CAST(:eids AS uuid[]))
              AND m.status = 'active' AND d.staged IS NOT TRUE
              AND (m.link_method IS NULL OR NOT (m.link_method = ANY(:blind)))
            GROUP BY 1, 2, 4"""),
            {"eids": list(frontier), "blind": list(BLIND_LINK_METHODS)})).all()
        for did, _eid, hits, df in rows:
            idf = max(0.05, math.log((total_docs + 1) / (df + 1)) / ln_n)
            scores[str(did)] += w_mention * min(hits, 10) * idf
        rows = (await s.execute(text("""
            SELECT r.evidence_document_id, count(*)
            FROM relationship r JOIN document d ON d.id = r.evidence_document_id
            WHERE (r.source_id = ANY(CAST(:eids AS uuid[]))
                   OR r.target_id = ANY(CAST(:eids AS uuid[])))
              AND r.source_id <> r.target_id AND d.staged IS NOT TRUE
            GROUP BY 1"""), {"eids": list(frontier)})).all()
        for did, hits in rows:
            scores[str(did)] += w_graph * min(hits, 5)
        if hop + 1 < depth:
            # The next hop leaves out the ubiquitous, as the walk does: an
            # entity in thousands of documents weighs the idf floor on each
            # of them and costs a scan of them all. Measured, one rule whose
            # source resolved to six entities spent 322 seconds here.
            nxt = (await s.execute(text("""
                WITH nxt AS (
                    SELECT DISTINCT CASE WHEN r.source_id = ANY(CAST(:eids AS uuid[]))
                                         THEN r.target_id ELSE r.source_id END AS eid
                    FROM relationship r
                    WHERE (r.source_id = ANY(CAST(:eids AS uuid[]))
                           OR r.target_id = ANY(CAST(:eids AS uuid[])))
                      AND r.source_id <> r.target_id)
                SELECT n.eid FROM nxt n
                LEFT JOIN entity_stats st ON st.entity_id = n.eid
                WHERE coalesce(st.documents, 0) <= :cap
                ORDER BY n.eid LIMIT 200"""),
                {"eids": list(frontier), "cap": UBIQUITY_CAP})).scalars().all()
            frontier = {str(e) for e in nxt} - seen
            seen |= frontier
    return [d for d, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


async def _walk(s, core: list[str], n: int) -> list[str]:
    """Documents that share the fused core's entities, weighted by how rare
    each shared entity is: the graph walked from what was retrieved rather
    than from what the planner named. The core itself is left out."""
    from app.retrieval_first import BLIND_LINK_METHODS

    if not core:
        return []
    return [str(d) for d in (await s.execute(text("""
        WITH core AS (SELECT unnest(CAST(:core AS uuid[])) AS did),
        ents AS (
          SELECT DISTINCT m.entity_id AS eid FROM entity_mention m JOIN core c ON m.document_id = c.did
          WHERE m.status = 'active' AND (m.link_method IS NULL OR NOT (m.link_method = ANY(:blind)))
          UNION
          SELECT r.source_id FROM relationship r JOIN core c ON r.evidence_document_id = c.did WHERE r.source_id <> r.target_id
          UNION
          SELECT r.target_id FROM relationship r JOIN core c ON r.evidence_document_id = c.did WHERE r.source_id <> r.target_id),
        weighted AS (
          SELECT e.eid, 1.0 / ln(coalesce(st.documents, 1) + 2) AS w
          FROM ents e JOIN entity ent ON ent.id = e.eid LEFT JOIN entity_stats st ON st.entity_id = e.eid
          WHERE coalesce(st.documents, 0) <= :cap AND NOT (ent.metadata ? 'generic_term')),
        neigh AS (
          SELECT m.document_id AS did, sum(w.w) AS score
          FROM entity_mention m JOIN weighted w ON w.eid = m.entity_id
          JOIN document d ON d.id = m.document_id
          WHERE m.status = 'active' AND (m.link_method IS NULL OR NOT (m.link_method = ANY(:blind)))
            AND d.staged IS NOT TRUE AND m.document_id NOT IN (SELECT did FROM core)
          GROUP BY m.document_id
          UNION ALL
          SELECT r.evidence_document_id, sum(w.w) * 2
          FROM relationship r JOIN weighted w ON w.eid IN (r.source_id, r.target_id)
          JOIN document d ON d.id = r.evidence_document_id
          WHERE d.staged IS NOT TRUE AND r.evidence_document_id NOT IN (SELECT did FROM core)
            AND r.source_id <> r.target_id
          GROUP BY r.evidence_document_id)
        SELECT did FROM neigh GROUP BY did ORDER BY sum(score) DESC, did LIMIT :n"""),
        {"core": list(core), "blind": list(BLIND_LINK_METHODS), "cap": UBIQUITY_CAP, "n": n})).scalars().all()]


# ── the rank ────────────────────────────────────────────────────────────────


async def rank(plan: dict, top_n: int = 100) -> list[dict]:
    """Every rule's channels, the question's words, fused; the walk from
    the fused top, fused again. Each document carries why it is there."""
    from app.db import AsyncSessionLocal
    from app.retrieval_first import EFFORT_DEPTH, question_terms

    depth = EFFORT_DEPTH.get(plan.get("effort", "medium"), 2)
    lists: list[tuple[float, list[str]]] = []
    reasons: dict[str, list[str]] = defaultdict(list)

    def take(label: str, weight: float, docs: list[str]) -> None:
        lists.append((weight, docs))
        for i, d in enumerate(docs[:top_n]):
            reasons[d].append(f"{label} #{i + 1}")

    # Rules that name the same source walk the same graph: two samples of
    # the same question name the same handful of sources over and over,
    # and the walk is the dearest channel (its second hop fans out to two
    # hundred entities). Walked once per distinct anchor set, then reused;
    # the fusion still counts it once per rule, which is what a source
    # named by several rules deserves.
    walked: dict[frozenset[str], list[str]] = {}

    async def graph_of(s, anchors: list[str]) -> list[str]:
        key = frozenset(anchors)
        if key not in walked:
            walked[key] = await _graph_from(s, sorted(key), depth, CHANNEL_TOP)
        return walked[key]

    async with AsyncSessionLocal() as s:
        common = await _common_terms(s)
        for k, r in enumerate(plan.get("hypotheses") or [], 1):
            terms = question_terms(r["rule"])
            take(f"rule {k}: propositions", 1.0, await _proposition_words(s, terms, common, CHANNEL_TOP))
            take(f"rule {k}: full text", 1.0, await _text_words(s, terms, common, CHANNEL_TOP))
            take(f"rule {k}: graph", 1.0, await graph_of(s, r.get("anchors") or []))
            take(f"rule {k}: summary", SUMMARY_WEIGHT, await _summary_words(s, terms, common, CHANNEL_TOP))
        take("the question's words", 1.0,
             await _text_words(s, question_terms(str(plan.get("question") or "")), common, CHANNEL_TOP))
        core = [d for d, _ in rrf(lists)[:CORE]]
        take("walk from the retrieved core", 1.0, await _walk(s, core, CHANNEL_TOP))
        fused = rrf(lists)[:top_n]
        ids = [d for d, _ in fused]
        names = {str(i): fn for i, fn in (await s.execute(text(
            "SELECT id, filename FROM document WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids})).all()} if ids else {}
    # Tie-break on the document id: equal scores rank the same way every
    # time, which is the whole point. The fusion already sorts so; the cut
    # here keeps that order and the shape the briefing reads.
    ranked = sorted(fused, key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [{"document_id": did, "filename": names.get(did, ""),
             "score": round(sc, 5),
             "reason": "; ".join(dict.fromkeys(reasons[did]))[:480]}
            for did, sc in ranked]
