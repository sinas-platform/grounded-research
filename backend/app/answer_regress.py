"""Answer-side regression: score published answers against the gold standard.

The existing harness scores RETRIEVAL — did the right documents come back.
Nothing scored the ANSWER, so every judgement about whether a change helped
came from reading answers by hand, one at a time. That does not survive 56
questions or a week of changes.

What it measures, per question, from data already stored:

  core sources cited   the gold standard names the authorities an answer must
                       rest on. Citing a document that was retrieved but never
                       opened is the single most common expert complaint, so
                       this is the headline number.
  unsupported claims   claims carrying no evidence at all.
  overreach            claims the coverage judge marked as asserting more than
                       their passages establish.
  uncovered parts      parts of the question the gate said were not answered.
  outcome              published / partial, and why.

    python -m app.answer_regress                 # score the latest run per question
    python -m app.answer_regress --json out.json # machine-readable, for diffing
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.retrieval_first import BENCH, _files_for, _load_gold


def _similarity(a: str, b: str) -> float:
    """Token overlap. The gold standard phrases a question in its own words
    ("We are advising on a food-delivery platform acquisition...") while the
    approved question that was actually run is worded differently, so exact
    matching finds nothing."""
    ta = {w for w in a.lower().split() if len(w) > 4}
    tb = {w for w in b.lower().split() if len(w) > 4}
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


async def _latest_run(session, question: str) -> dict | None:
    rows = (await session.execute(text("""
        SELECT q.id, q.question, q.status, q.answer_id, q.telemetry, q.created_at,
               (SELECT count(*) FROM answer_claim c WHERE c.answer_id = q.answer_id) AS claims,
               (SELECT count(*) FROM answer_claim c
                WHERE c.answer_id = q.answer_id
                  AND NOT EXISTS (SELECT 1 FROM claim_evidence e
                                  WHERE e.claim_id = c.id)) AS unsupported
        FROM query_run q
        WHERE q.status IN ('published', 'partial')
        ORDER BY q.created_at DESC"""))).mappings().all()
    best, score = None, 0.0
    for r in rows:
        sc = _similarity(question, r["question"] or "")
        if sc > score:   # rows are newest-first, so ties keep the newest
            best, score = r, sc
    return dict(best) | {"match": round(score, 2)} if best and score >= 0.45 else None


async def _cited_files(session, answer_id) -> set[str]:
    if not answer_id:
        return set()
    return {fn for (fn,) in (await session.execute(text("""
        SELECT DISTINCT d.filename FROM claim_evidence e
        JOIN answer_claim c ON c.id = e.claim_id
        JOIN document d ON d.id = e.document_id
        WHERE c.answer_id = :a"""), {"a": answer_id})).all() if fn}


async def _run_gold(effort: str) -> None:
    """Fire every gold question and wait for it to settle."""
    import httpx

    from app.config import get_settings

    gold, _c, _a = _load_gold()
    qs = [q.get("question") for q in (gold.get("questions") or []) if q.get("question")]
    st = get_settings()
    hdr = {"Authorization": f"Bearer {st.sinas_api_key}"}
    ids = []
    async with httpx.AsyncClient(timeout=60.0) as c:
        for q in qs:
            r = await c.post("http://localhost:8080/api/v1/query-runs", headers=hdr,
                             json={"question": q, "mode": "full", "effort": effort})
            r.raise_for_status()
            ids.append(r.json()["id"])
            print(f"  launched {r.json()['id'][:8]}  {q[:56]}", flush=True)
            await asyncio.sleep(20)   # the extractor quota is the limiter
    print(f"\n{len(ids)} runs launched; waiting…", flush=True)
    while True:
        async with AsyncSessionLocal() as s2:
            done = (await s2.execute(text("""
                SELECT count(*) FROM query_run WHERE id = ANY(CAST(:i AS uuid[]))
                  AND status IN ('published','partial','failed','cancelled')"""),
                {"i": ids})).scalar()
        print(f"  {done}/{len(ids)} settled", flush=True)
        if done >= len(ids):
            break
        await asyncio.sleep(30)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="out")
    ap.add_argument("--run", action="store_true",
                    help="run the gold questions first, then score them. "
                         "Scoring alone reads whatever was run last, which "
                         "only matches when the approved wording and the "
                         "benchmark wording happen to agree.")
    ap.add_argument("--effort", default="medium")
    args = ap.parse_args()

    if args.run:
        await _run_gold(args.effort)

    gold, canon, a2c = _load_gold()
    results = []
    async with AsyncSessionLocal() as session:
        for q in gold.get("questions") or []:
            run = await _latest_run(session, q.get("question", ""))
            if not run:
                results.append({"id": q.get("id"), "title": q.get("title"),
                                "status": "NOT RUN"})
                continue
            cited = await _cited_files(session, run["answer_id"])
            core, hit, missed = 0, 0, []
            for src in q.get("required_sources") or []:
                if not src.get("core"):
                    continue
                core += 1
                # (files, loaded) — `loaded` says the document is in the
                # corpus at all. A source we never ingested cannot be cited,
                # and scoring the answer for it would measure the corpus.
                files_list, loaded = _files_for(src.get("document", ""), canon, a2c)
                files = set(files_list or [])
                if not files or not loaded:
                    core -= 1
                    continue
                if files & cited:
                    hit += 1
                else:
                    missed.append(src.get("document"))
            tel = run["telemetry"] or {}
            val = tel.get("validate") or {}
            results.append({
                "id": q.get("id"), "title": q.get("title"),
                "status": run["status"],
                "claims": run["claims"],
                "unsupported_claims": run["unsupported"],
                "core_sources": f"{hit}/{core}" if core else "-",
                "core_pct": round(100 * hit / core) if core else None,
                "missed_core": missed,
                "overreach": (val.get("revision") or {}).get("feedback_items"),
                "uncovered_parts": val.get("gate_redraft"),
                "partial_cause": (tel.get("partial") or {}).get("cause"),
            })

    scored = [r for r in results if r.get("core_pct") is not None]
    print(f"{'question':34} {'status':10} {'claims':>6} {'no-ev':>6} {'core sources':>13}")
    print("-" * 76)
    for r in results:
        print(f"{str(r.get('title'))[:33]:34} {str(r.get('status')):10} "
              f"{str(r.get('claims','-')):>6} {str(r.get('unsupported_claims','-')):>6} "
              f"{str(r.get('core_sources','-')):>13}")
        if r.get("missed_core"):
            print(f"{'':34} missed: {', '.join(str(m) for m in r['missed_core'])[:90]}")
    if scored:
        print("-" * 76)
        print(f"core sources cited: {round(sum(r['core_pct'] for r in scored)/len(scored))}%"
              f"   answers scored: {len(scored)}"
              f"   unsupported claims: {sum(r.get('unsupported_claims') or 0 for r in scored)}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1, default=str))
        print(f"\nwritten to {args.out}")


asyncio.run(main())
