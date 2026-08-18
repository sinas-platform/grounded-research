"""Retrieval-first query execution — v2 (16 Aug, schema-aware planner).

Kjeld's spec: the planner is grounded in the corpus schema (entity types,
document classes, properties — with counts and frequency-ranked examples),
uses TWO rounds (propose probes/value-queries -> see real matched values
with counts -> finalize), and emits websearch-syntax text queries plus
class boosts. Retrieval traverses the relationship graph to depth k
(linked to effort), every result document carries WHY it is there
(channel provenance -> result_document.reason), the full ranked list is
stored (rerankable later), and a synthesis briefing (title, class,
properties, summary, TOC — capped by effort) is assembled per result.

CLI:
  python -m app.retrieval_first --regress [--effort medium]
  python -m app.retrieval_first --question "..." [--effort medium] [--store]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

PLAN_AGENT = "grove/retrieval-planner-agent"  # tool-less planning lane, Sonnet via client MLflow (Kjeld 17 Aug)

EFFORT_DEPTH = {"low": 1, "medium": 2, "high": 3}
EFFORT_BRIEFING = {"low": 8, "medium": 15, "high": 25}
STORE_TOP = 100  # full ranked list stored for later reranking

_ROUND1_PROMPT = """You are planning document retrieval for a legal research
question against a corpus with this schema:

{corpus_map}

Propose retrieval probes grounded in the schema. Reply ONLY JSON:
{{"named_entities": ["<entities NAMED in the question>"],
  "value_probes": [{{"type": "<entity type from the schema>", "match": "<substring to find real values, e.g. 'food deliv'>"}}],
  "seed_cases": ["<specific cases/decisions/parties you KNOW bear on this topic even if unnamed>"],
  "websearch_queries": ["<3-10 SHORT queries: one quoted phrase or 2-4 words each; several small queries beat one long one; per concept per language (EN, FR; NL/DE/ES when relevant)>"]}}

QUESTION: {question}"""

_ROUND2_PROMPT = """Your probes were resolved against the real corpus. Matched
values (with document counts):

{matches}

Finalize the retrieval plan. Keep only anchors that serve the question;
drop noise; add websearch queries for gaps the matches revealed.
Reply ONLY JSON:
{{"anchor_entity_ids": ["<ids from the matches to anchor on>"],
  "websearch_queries": ["<final SHORT queries: one quoted phrase or 2-4 words each>"],
  "class_boost": ["<document classes from the schema to rank up, or empty>"]}}

QUESTION: {question}"""


def _parse(reply: str) -> dict:
    cleaned = reply.strip().strip("`").removeprefix("json").strip()
    return json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])


async def build_corpus_map() -> str:
    """Schema snapshot: entity types (frequency-ranked examples), document
    classes with counts, and per-class properties with example values."""
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        et = (await s.execute(text("""
            WITH freq AS (
              SELECT e.entity_type_id, e.canonical_form,
                     count(m.id) AS uses,
                     row_number() OVER (PARTITION BY e.entity_type_id
                                        ORDER BY count(m.id) DESC) AS rn
              FROM entity e LEFT JOIN entity_mention m ON m.entity_id = e.id
              WHERE e.merged_into_id IS NULL
              GROUP BY 1, 2)
            SELECT t.name, count(DISTINCT e.id),
                   (SELECT array_agg(canonical_form)
                    FROM freq WHERE entity_type_id = t.id AND rn <= 5)
            FROM entity_type t
            LEFT JOIN entity e ON e.entity_type_id = t.id
                               AND e.merged_into_id IS NULL
            GROUP BY t.id, t.name ORDER BY 2 DESC"""))).all()
        dc = (await s.execute(text("""
            SELECT c.name, count(d.id) FROM document_class c
            LEFT JOIN document d ON d.document_class_id = c.id
            GROUP BY c.name ORDER BY 2 DESC"""))).all()
        props = (await s.execute(text("""
            SELECT c.name, p.name,
                   (SELECT (array_agg(DISTINCT pv.value->>'_'))[1:3]
                    FROM property_value pv WHERE pv.property_id = p.id)
            FROM document_class_property p
            JOIN document_class c ON c.id = p.document_class_id
            LIMIT 40"""))).all()
    lines = ["ENTITY TYPES (name, count, most-mentioned examples):"]
    for name, cnt, ex in et:
        lines.append(f"- {name} ({cnt}): {', '.join((ex or [])[:5])}")
    lines.append("DOCUMENT CLASSES (name, count):")
    for name, cnt in dc:
        lines.append(f"- {name} ({cnt})")
    lines.append("DOCUMENT PROPERTIES (class.property: example values):")
    for cname, pname, ex in props:
        exs = ", ".join(str(x) for x in (ex or []) if x)[:90]
        lines.append(f"- {cname}.{pname}: {exs}")
    return "\n".join(lines)


async def _resolve_value_probes(probes: list[dict]) -> list[dict]:
    """Probe -> real values with document counts. Returned to the planner."""
    from app.db import AsyncSessionLocal

    out: list[dict] = []
    async with AsyncSessionLocal() as s:
        for pr in probes[:12]:
            m = str(pr.get("match") or "").strip()
            tname = str(pr.get("type") or "").strip()
            if len(m) < 3:
                continue
            rows = (await s.execute(text("""
                SELECT e.id, e.canonical_form, t.name,
                       (SELECT count(DISTINCT document_id)
                        FROM entity_mention WHERE entity_id = e.id) AS docs
                FROM entity e JOIN entity_type t ON t.id = e.entity_type_id
                WHERE e.merged_into_id IS NULL
                  AND (:tname = '' OR t.name ILIKE :tname)
                  AND e.canonical_form ILIKE :pat
                ORDER BY docs DESC LIMIT 12"""),
                {"tname": tname, "pat": f"%{m}%"})).all()
            for eid, cf, tn, docs in rows:
                out.append({"id": str(eid), "value": cf, "type": tn,
                            "docs": int(docs)})
    return out


async def _resolve_names(names: list[str]) -> list[dict]:
    """Named entities / seed cases -> entity matches (alias-tolerant)."""
    from app.db import AsyncSessionLocal

    seen: dict[str, dict] = {}
    parts: list[str] = []
    for name in names:
        parts.append(name)
        for p in name.replace("/", ",").split(","):
            if p.strip() and p.strip() != name:
                parts.append(p.strip())
    async with AsyncSessionLocal() as s:
        for n in dict.fromkeys(x.strip() for x in parts):
            if len(n) < 4:
                continue
            rows = (await s.execute(text("""
                SELECT e.id, e.canonical_form, t.name,
                       (SELECT count(DISTINCT document_id)
                        FROM entity_mention WHERE entity_id = e.id)
                FROM entity e JOIN entity_type t ON t.id = e.entity_type_id
                WHERE e.merged_into_id IS NULL
                  AND (e.canonical_form ILIKE :pat OR e.id IN
                       (SELECT entity_id FROM entity_alias
                        WHERE alias ILIKE :pat))
                LIMIT 6"""), {"pat": f"%{n}%"})).all()
            for eid, cf, tn, docs in rows:
                seen[str(eid)] = {"id": str(eid), "value": cf, "type": tn,
                                  "docs": int(docs)}
    return list(seen.values())


async def plan_question(question: str, effort: str = "medium") -> dict:
    from app.services.query_runner import _Sinas

    sinas = _Sinas()
    corpus_map = await build_corpus_map()
    r1 = _parse(await sinas.invoke(PLAN_AGENT, _ROUND1_PROMPT.format(
        corpus_map=corpus_map, question=question)))
    probe_matches = await _resolve_value_probes(r1.get("value_probes") or [])
    name_matches = await _resolve_names(
        (r1.get("named_entities") or []) + (r1.get("seed_cases") or []))
    all_matches = {m["id"]: m for m in probe_matches + name_matches}
    match_lines = "\n".join(
        f"- id={m['id']} [{m['type']}] {m['value']!r} ({m['docs']} docs)"
        for m in sorted(all_matches.values(), key=lambda x: -x["docs"])[:40]
    ) or "(no matches — rely on websearch queries)"
    r2 = _parse(await sinas.invoke(PLAN_AGENT, _ROUND2_PROMPT.format(
        matches=match_lines, question=question)))
    anchors = [a for a in (r2.get("anchor_entity_ids") or [])
               if a in all_matches]
    # determinism: model's picks unioned with the strongest matches, and
    # both rounds' queries kept — reduces run-to-run swing
    for m in sorted(all_matches.values(), key=lambda x: -x["docs"])[:6]:
        if m["id"] not in anchors:
            anchors.append(m["id"])
    queries = list(dict.fromkeys(
        [str(q) for q in (r1.get("websearch_queries") or [])]
        + [str(q) for q in (r2.get("websearch_queries") or [])]))[:14]
    return {"anchors": anchors,
            "anchor_names": {a: all_matches[a]["value"] for a in anchors},
            "queries": queries,
            "class_boost": [str(c) for c in (r2.get("class_boost") or [])],
            "effort": effort}


async def retrieve_and_rank(plan: dict, top_n: int = STORE_TOP) -> list[dict]:
    """Channels: anchor mentions, graph traversal to depth k (effort),
    websearch text. Every doc accumulates provenance reasons."""
    from app.db import AsyncSessionLocal

    depth = EFFORT_DEPTH.get(plan.get("effort", "medium"), 2)
    scores: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)
    names: dict[str, str] = {}

    async with AsyncSessionLocal() as s:
        frontier = set(plan["anchors"])
        seen_entities = set(frontier)
        for hop in range(depth):
            if not frontier:
                break
            w_mention = 3.0 / (hop + 1) ** 2
            w_graph = 2.0 / (hop + 1) ** 2
            rows = (await s.execute(text("""
                SELECT m.document_id, d.filename, m.entity_id, count(*)
                FROM entity_mention m JOIN document d ON d.id = m.document_id
                WHERE m.entity_id = ANY(CAST(:eids AS uuid[]))
                  AND m.status = 'active'
                GROUP BY 1, 2, 3"""),
                {"eids": list(frontier)})).all()
            for did, fn, eid, hits in rows:
                did = str(did)
                scores[did] += w_mention * min(hits, 10)
                names[did] = fn
                label = plan["anchor_names"].get(str(eid), str(eid)[:8])
                reasons[did].append(
                    f"mentions {label} x{hits}" if hop == 0 else
                    f"mentions {label} ({hop}-hop) x{hits}")
            rows = (await s.execute(text("""
                SELECT r.evidence_document_id, d.filename, count(*)
                FROM relationship r
                JOIN document d ON d.id = r.evidence_document_id
                WHERE r.source_id = ANY(CAST(:eids AS uuid[]))
                   OR r.target_id = ANY(CAST(:eids AS uuid[]))
                GROUP BY 1, 2"""), {"eids": list(frontier)})).all()
            for did, fn, hits in rows:
                did = str(did)
                scores[did] += w_graph * min(hits, 5)
                names[did] = fn
                via = ", ".join(list(plan["anchor_names"].values())[:2])
                reasons[did].append(
                    f"relationship evidence ({hop + 1}-hop via {via})")
            if hop + 1 < depth:
                rows = (await s.execute(text("""
                    SELECT DISTINCT CASE WHEN r.source_id = ANY(CAST(:eids AS uuid[]))
                                         THEN r.target_id ELSE r.source_id END
                    FROM relationship r
                    WHERE r.source_id = ANY(CAST(:eids AS uuid[]))
                       OR r.target_id = ANY(CAST(:eids AS uuid[]))
                    LIMIT 200"""), {"eids": list(frontier)})).scalars().all()
                frontier = {str(r) for r in rows} - seen_entities
                seen_entities |= frontier

        for q in plan["queries"]:
            if len(q.strip()) < 3:
                continue
            rows = (await s.execute(text("""
                SELECT d.id, d.filename,
                       ts_rank(dv.content_tsvector,
                               websearch_to_tsquery('simple', :q)) AS r
                FROM document d
                JOIN document_version dv ON dv.id = d.current_version_id
                WHERE dv.content_tsvector @@ websearch_to_tsquery('simple', :q)
                ORDER BY r DESC LIMIT 60"""), {"q": q})).all()
            for did, fn, r in rows:
                did = str(did)
                scores[did] += 8.0 * float(r) + 0.5
                names[did] = fn
                reasons[did].append(f"matched {q!r}")

        # filename channel: decision docs are named after their cases
        import re as _re
        for a in plan["anchors"]:
            nm = plan["anchor_names"].get(a, "")
            toks = [t for t in _re.findall(r"[a-z0-9]{4,}", nm.lower())
                    if t not in ("merger", "inquiry", "decision", "case")][:4]
            if len(toks) < 2:
                continue
            pat = "%" + "%".join(toks[:3]) + "%"
            rows = (await s.execute(text("""
                SELECT id, filename FROM document
                WHERE filename ILIKE :pat LIMIT 10"""),
                {"pat": pat})).all()
            for did, fn in rows:
                did = str(did)
                scores[did] += 12.0
                names[did] = fn
                reasons[did].append(f"filename matches {nm!r}")

        if plan.get("class_boost") and scores:
            rows = (await s.execute(text("""
                SELECT d.id FROM document d
                JOIN document_class c ON c.id = d.document_class_id
                WHERE c.name = ANY(:classes)
                  AND d.id = ANY(CAST(:dids AS uuid[]))"""),
                {"classes": plan["class_boost"],
                 "dids": list(scores.keys())})).scalars().all()
            for did in rows:
                scores[str(did)] *= 1.3

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_n]
    return [{"document_id": did, "filename": names[did],
             "score": round(sc, 3),
             "reason": "; ".join(dict.fromkeys(reasons[did]))[:480]}
            for did, sc in ranked]


async def build_briefing(ranked: list[dict], effort: str) -> list[dict]:
    """title/class/properties/summary/TOC for the top-N (effort-capped)."""
    from app.db import AsyncSessionLocal

    cap = EFFORT_BRIEFING.get(effort, 15)
    out = []
    async with AsyncSessionLocal() as s:
        for r in ranked[:cap]:
            row = (await s.execute(text("""
                SELECT d.filename, c.name, d.summary, d.toc,
                       (SELECT json_object_agg(p.name, pv.value->>'_')
                        FROM property_value pv
                        JOIN document_class_property p ON p.id = pv.property_id
                        WHERE pv.document_id = d.id)
                FROM document d
                LEFT JOIN document_class c ON c.id = d.document_class_id
                WHERE d.id = CAST(:did AS uuid)"""),
                {"did": r["document_id"]})).first()
            if row is None:
                continue
            fn, cls, summary, toc, props = row
            out.append({"document_id": r["document_id"], "filename": fn,
                        "class": cls, "score": r["score"],
                        "reason": r["reason"], "summary": summary,
                        "toc": toc, "properties": props})
    return out


async def store_result(question: str, ranked: list[dict],
                       briefing: list[dict], plan: dict | None = None) -> str:
    """Persist as a Grove result with per-doc rank + reason; briefing kept on
    the result's filter payload so synthesis/UI can read it."""
    from app.db import AsyncSessionLocal

    rid = str(uuid.uuid4())
    async with AsyncSessionLocal() as s:
        owner = (await s.execute(text(
            "SELECT owner_id FROM result WHERE owner_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"))).scalar()
        await s.execute(text("""
            INSERT INTO result (id, query, status, owner_id, roles, filter,
                                created_at, updated_at, published_at)
            VALUES (CAST(:id AS uuid), :q, 'published', CAST(:o AS uuid),
                    '{}', CAST(:f AS jsonb), now(), now(), now())"""),
            {"id": rid, "q": question, "o": str(owner),
             "f": json.dumps({"retrieval_first": True, "briefing": briefing,
                              "plan": {k: v for k, v in (plan or {}).items()}})})
        for i, r in enumerate(ranked, start=1):
            dv = (await s.execute(text(
                "SELECT current_version_id FROM document "
                "WHERE id = CAST(:d AS uuid)"),
                {"d": r["document_id"]})).scalar()
            await s.execute(text("""
                INSERT INTO result_document (id, result_id, document_id,
                    document_version_id, rank, reason, added_by_agent,
                    created_at, updated_at)
                VALUES (gen_random_uuid(), CAST(:rid AS uuid),
                        CAST(:did AS uuid), CAST(:dv AS uuid), :rank,
                        :reason, 'retrieval-first', now(), now())"""),
                {"rid": rid, "did": r["document_id"], "dv": str(dv),
                 "rank": i, "reason": r["reason"]})
        await s.commit()
    return rid


# ── Gate-1 regression (same harness, v2 planner/retriever) ──────────────────
# Optional regression benchmark directory (gold_standard yaml + document
# map). Domain-specific content lives OUTSIDE the repo; point this at your
# deployment's benchmark folder to enable --regress.
BENCH = Path(__import__("os").environ.get("GROVE_BENCH_DIR", str(Path.home() / "grove-benchmark")))


def _load_gold():
    import yaml

    gold = yaml.safe_load((BENCH / "gold_standard_v2.yaml").read_text())
    dmap = yaml.safe_load((BENCH / "document_map.yaml").read_text())
    entries = dmap.get("documents") or dmap
    canon, a2c = {}, {}
    for key, ent in entries.items():
        if not isinstance(ent, dict):
            continue
        canon[key] = ent
        a2c[key.lower()] = key
        for a in ent.get("aliases") or []:
            a2c[str(a).lower()] = key
    return gold, canon, a2c


def _files_for(docname, canon, a2c):
    key = a2c.get(docname.lower())
    if key is None:
        for a, k in a2c.items():
            if docname.lower() in a or a in docname.lower():
                key = k
                break
    if key is None:
        return [], True
    ent = canon[key]
    files = [str(f) for f in (ent.get("files") or [])]
    # July consolidation renamed part-files (x--pNN.md -> x.md); the map
    # predates it. Accept the consolidated base name too.
    import re as _re
    for f in list(files):
        base = _re.sub(r"--p\d+\.md$", ".md", f)
        if base != f and base not in files:
            files.append(base)
    return files, bool(ent.get("loaded", True))


async def _files_exist(files: list[str]) -> bool:
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM document WHERE filename = ANY(:f)"),
            {"f": files})).scalar()
    return bool(n)


async def regress(top_n: int = 30, effort: str = "medium") -> None:
    gold, canon, a2c = _load_gold()
    base = json.loads((BENCH / "combined_v2_scores.json").read_text())
    base_by_q = {r["qid"]: r for r in base.get("per_question", [])}
    print(f"{'qid':4} {'core_recall':>11} {'gate1':>6} "
          f"{'trap_excl':>9} {'gate1':>6}")
    tot_cr, tot_te, n = 0.0, 0.0, 0
    for q in gold["questions"]:
        plan = await plan_question(q["question"], effort=effort)
        ranked = await retrieve_and_rank(plan, top_n=top_n)
        got = {r["filename"] for r in ranked}
        req = q.get("required_sources") or []
        core = [r for r in req if r.get("core")] or req
        hits = denom = 0
        for r in core:
            files, loaded = _files_for(r["document"], canon, a2c)
            if not loaded or not files:
                continue
            exists = (await _files_exist(files))
            if not exists:
                continue  # stale map entry: files absent from corpus
            denom += 1
            if any(f in got for f in files):
                hits += 1
        cr = hits / denom if denom else 1.0
        tdenom = thits = 0
        for t in q.get("traps") or []:
            files, _ = _files_for(t["lookalike"], canon, a2c)
            if not files:
                continue
            tdenom += 1
            if not any(f in got for f in files):
                thits += 1
        te = thits / tdenom if tdenom else 1.0
        b = base_by_q.get(q["id"], {})
        print(f"{q['id']:4} {cr:11.2f} {b.get('core_recall', 0):6.2f} "
              f"{te:9.2f} {b.get('trap_exclusion', 0):6.2f}", flush=True)
        tot_cr += cr
        tot_te += te
        n += 1
    print(f"MEAN {tot_cr/n:11.2f} {'':6} {tot_te/n:9.2f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regress", action="store_true")
    ap.add_argument("--question")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--store", action="store_true")
    args = ap.parse_args()
    if args.regress:
        await regress(top_n=args.top, effort=args.effort)
    elif args.question:
        plan = await plan_question(args.question, effort=args.effort)
        print(json.dumps({k: v for k, v in plan.items()
                          if k != "anchor_names"}, indent=1)[:1200])
        ranked = await retrieve_and_rank(plan)
        for r in ranked[:args.top]:
            print(f"  {r['score']:8.2f}  {r['filename']:44}  "
                  f"{r['reason'][:70]}")
        if args.store:
            briefing = await build_briefing(ranked, args.effort)
            rid = await store_result(args.question, ranked, briefing, plan)
            print(f"stored result {rid}")
    else:
        print("need --regress or --question", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
