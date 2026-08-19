"""Entity dedup — deterministic merges + LLM-judged fuzzy pairs.

Sources of duplicates: multi-phase ingestion, and same-batch creations in
the batched middle (candidate lists frozen at collect time, 16 Aug).

Semantics of a merge (survivor <- loser):
  - loser.merged_into_id = survivor.id (tombstone; matchers already skip
    merged entities)
  - loser's mentions repointed to survivor (retrieval unifies)
  - loser's canonical_form added as an EntityAlias of survivor (name keeps
    matching in the gazetteer)
Survivor choice: the older row (more history pointing at it).

Modes:
  --report        list candidate pairs, change nothing (default)
  --apply-exact   apply deterministic merges (identical normalized form)
  --apply-llm     judge fuzzy pairs via a provider batch, apply merges

Run standalone:
    cd backend && ../.venv/bin/python -m app.entity_dedup --report
    ... --apply-exact --apply-llm --job-dir ~/sgr-bulk-jobs/dedup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select, update

log = logging.getLogger("dedup")

_JUDGE_PROMPT = """Are these two entries the SAME real-world entity? Consider
abbreviations, translations, and short forms of the same organisation, court,
instrument or company as the SAME entity. Distinct subsidiaries, chambers or
departments are DIFFERENT entities. Reply ONLY JSON:
{{"pairs": [{{"pair": <n>, "same": true|false, "confidence": <0..1>}}]}}

{pairs}"""


def _norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if len(t) >= 3}


async def find_pairs():
    from app.db import AsyncSessionLocal
    from app.models import Entity, EntityType

    async with AsyncSessionLocal() as session:
        ents = (await session.execute(
            select(Entity).where(Entity.merged_into_id.is_(None))
        )).scalars().all()
        types = {t.id: t.name for t in
                 (await session.execute(select(EntityType))).scalars()}
    exact: list[tuple] = []
    fuzzy: list[tuple] = []
    by_type: dict = defaultdict(list)
    for e in ents:
        by_type[e.entity_type_id].append(e)
    for tid, rows in by_type.items():
        by_norm: dict[str, list] = defaultdict(list)
        for e in rows:
            by_norm[_norm(e.canonical_form)].append(e)
        for n, group in by_norm.items():
            if n and len(group) > 1:
                group.sort(key=lambda e: e.created_at)
                for loser in group[1:]:
                    exact.append((group[0], loser))
        # fuzzy: token-overlap blocking within type (Jaccard >= 0.6)
        norms = [(e, _tokens(e.canonical_form)) for e in rows]
        token_index: dict[str, list[int]] = defaultdict(list)
        for i, (_, toks) in enumerate(norms):
            for t in toks:
                token_index[t].append(i)
        seen = set()
        for i, (e1, t1) in enumerate(norms):
            if not t1:
                continue
            cand_idx = {j for t in t1 for j in token_index[t] if j > i}
            for j in cand_idx:
                e2, t2 = norms[j]
                if (i, j) in seen or not t2:
                    continue
                seen.add((i, j))
                if _norm(e1.canonical_form) == _norm(e2.canonical_form):
                    continue  # handled by exact
                jac = len(t1 & t2) / len(t1 | t2)
                if jac >= 0.6:
                    a, b = sorted((e1, e2), key=lambda e: e.created_at)
                    fuzzy.append((a, b, round(jac, 2), types.get(tid, "?")))
    return exact, fuzzy


async def apply_merge(session, survivor, loser) -> None:
    from app.models import EntityAlias, EntityMention

    await session.execute(
        update(EntityMention)
        .where(EntityMention.entity_id == loser.id)
        .values(entity_id=survivor.id)
    )
    if _norm(loser.canonical_form) != _norm(survivor.canonical_form):
        session.add(EntityAlias(alias=loser.canonical_form[:500],
                                entity_id=survivor.id))
    loser.merged_into_id = survivor.id




async def _judge_and_merge(fuzzy) -> int:
    """Judge tightened fuzzy pairs via a provider batch and merge confirmed
    ones. Extracted from the CLI path so the API can reuse it."""
    from pathlib import Path

    from app.bulk_pipeline import BatchClient
    from app.db import AsyncSessionLocal
    from app.services.entity_resolver import RESOLVER_AGENT

    if not fuzzy:
        return 0
    job_dir = Path.home() / "sgr-bulk-jobs" / "dedup-api"
    job_dir.mkdir(parents=True, exist_ok=True)
    client = BatchClient(job_dir)
    _PAIRS_PER_PROMPT = 40
    prompts = []
    for start in range(0, len(fuzzy), _PAIRS_PER_PROMPT):
        block = fuzzy[start:start + _PAIRS_PER_PROMPT]
        lines = []
        for i, (a, b, jac, tname) in enumerate(block, start=1):
            lines.append(f'PAIR {i} (type {tname}):\n'
                         f'  A: "{a.canonical_form[:200]}"\n'
                         f'  B: "{b.canonical_form[:200]}"')
        prompts.append(_JUDGE_PROMPT.format(pairs="\n\n".join(lines)))
    replies = await client.run_round("dedup-judge", RESOLVER_AGENT, prompts)
    llm_merged = 0
    async with AsyncSessionLocal() as session:
        for pi, reply in enumerate(replies):
            if not reply:
                continue
            try:
                cleaned = reply.strip().strip("`").removeprefix("json").strip()
                data = json.loads(cleaned[cleaned.find("{"):
                                          cleaned.rfind("}") + 1])
                verdicts = {int(p.get("pair") or 0): p
                            for p in data.get("pairs") or []}
            except Exception:  # noqa: BLE001
                continue
            block = fuzzy[pi * _PAIRS_PER_PROMPT:
                          (pi + 1) * _PAIRS_PER_PROMPT]
            for i, (a, b, jac, tname) in enumerate(block, start=1):
                v = verdicts.get(i) or {}
                if v.get("same") and float(v.get("confidence") or 0) >= 0.8:
                    s = await session.get(type(a), a.id)
                    l = await session.get(type(b), b.id)
                    if (s is None or l is None
                            or l.merged_into_id is not None
                            or s.merged_into_id is not None):
                        continue
                    await apply_merge(session, s, l)
                    llm_merged += 1
        await session.commit()
    return llm_merged


def _tighten_pairs(fuzzy, types=None):
    from collections import defaultdict
    if types:
        wanted = set(types)
        fuzzy = [row for row in fuzzy if row[3] in wanted]
    per_entity = defaultdict(int)
    kept = []
    for a, b, jac, tname in fuzzy:
        ta, tb = _tokens(a.canonical_form), _tokens(b.canonical_form)
        if not ta or not tb:
            continue
        contained = (ta <= tb or tb <= ta) and min(len(ta), len(tb)) >= 2
        if jac < 0.8 and not contained:
            continue
        if per_entity[a.id] >= 3 or per_entity[b.id] >= 3:
            continue
        per_entity[a.id] += 1
        per_entity[b.id] += 1
        kept.append((a, b, jac, tname))
    return kept


async def run_apply(mode: str, tighten: bool = True, types=None) -> dict:
    """API entry point: run one merge pass and repoint relationship edges.
    Returns a summary dict. mode: 'exact' | 'llm'."""
    from app.db import AsyncSessionLocal

    exact, fuzzy = await find_pairs()
    merged = 0
    if mode == "exact":
        async with AsyncSessionLocal() as session:
            for survivor, loser in exact:
                s = await session.get(type(survivor), survivor.id)
                l = await session.get(type(loser), loser.id)
                if l is None or l.merged_into_id is not None:
                    continue
                await apply_merge(session, s, l)
                merged += 1
            await session.commit()
    else:
        if tighten:
            fuzzy = _tighten_pairs(fuzzy, types)
        elif types:
            fuzzy = _tighten_pairs(fuzzy, types)  # types filter implies pass
        merged = await _judge_and_merge(fuzzy)
    from app.api.v1.maintenance import repoint_merged_relationships

    repointed = await repoint_merged_relationships()
    return {"pairs_considered": len(exact) if mode == "exact" else len(fuzzy),
            "merged": merged, "edges_repointed": repointed}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply-exact", action="store_true")
    ap.add_argument("--apply-llm", action="store_true")
    ap.add_argument("--job-dir", default="~/sgr-bulk-jobs/entity-dedup")
    ap.add_argument("--tighten", action="store_true",
                    help="restrict fuzzy pairs to jaccard>=0.8 or full "
                         "name-containment (>=2 tokens), max 3 partners "
                         "per entity")
    ap.add_argument("--types",
                    help="comma-separated entity type names; only judge "
                         "fuzzy pairs of these types")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")

    from app.db import AsyncSessionLocal

    exact, fuzzy = await find_pairs()
    print(f"exact-duplicate pairs: {len(exact)}")
    print(f"fuzzy candidate pairs: {len(fuzzy)}")

    if args.types:
        wanted = {t.strip() for t in args.types.split(",")}
        fuzzy = [row for row in fuzzy if row[3] in wanted]
        print(f"after --types {sorted(wanted)}: {len(fuzzy)}")
    if args.tighten:
        per_entity: dict = defaultdict(int)
        kept = []
        for a, b, jac, tname in fuzzy:
            ta, tb = _tokens(a.canonical_form), _tokens(b.canonical_form)
            if not ta or not tb:
                continue
            contained = (ta <= tb or tb <= ta) and min(len(ta), len(tb)) >= 2
            if jac < 0.8 and not contained:
                continue
            if per_entity[a.id] >= 3 or per_entity[b.id] >= 3:
                continue
            per_entity[a.id] += 1
            per_entity[b.id] += 1
            kept.append((a, b, jac, tname))
        fuzzy = kept
        print(f"after --tighten: {len(fuzzy)}")
    if args.report or not (args.apply_exact or args.apply_llm):
        for a, b, jac, tname in fuzzy[:40]:
            print(f"  [{tname}] {jac}: {a.canonical_form[:60]!r} ~ "
                  f"{b.canonical_form[:60]!r}")
        return

    merged = 0
    if args.apply_exact and exact:
        async with AsyncSessionLocal() as session:
            for survivor, loser in exact:
                s = await session.get(type(survivor), survivor.id)
                l = await session.get(type(loser), loser.id)
                if l is None or l.merged_into_id is not None:
                    continue
                await apply_merge(session, s, l)
                merged += 1
            await session.commit()
        print(f"exact merges applied: {merged}")

    if args.apply_llm and fuzzy:
        from app.bulk_pipeline import BatchClient, SUBMIT_MAX
        from app.services.entity_resolver import RESOLVER_AGENT

        job_dir = Path(args.job_dir).expanduser()
        job_dir.mkdir(parents=True, exist_ok=True)
        client = BatchClient(job_dir)
        _PAIRS_PER_PROMPT = 40
        prompts = []
        for start in range(0, len(fuzzy), _PAIRS_PER_PROMPT):
            block = fuzzy[start:start + _PAIRS_PER_PROMPT]
            lines = []
            for i, (a, b, jac, tname) in enumerate(block, start=1):
                lines.append(f'PAIR {i} (type {tname}):\n'
                             f'  A: "{a.canonical_form[:200]}"\n'
                             f'  B: "{b.canonical_form[:200]}"')
            prompts.append(_JUDGE_PROMPT.format(pairs="\n\n".join(lines)))
        replies = await client.run_round("dedup-judge", RESOLVER_AGENT, prompts)
        llm_merged = 0
        async with AsyncSessionLocal() as session:
            for pi, reply in enumerate(replies):
                if not reply:
                    continue
                try:
                    cleaned = reply.strip().strip("`").removeprefix("json").strip()
                    data = json.loads(cleaned[cleaned.find("{"):
                                              cleaned.rfind("}") + 1])
                    verdicts = {int(p.get("pair") or 0): p
                                for p in data.get("pairs") or []}
                except Exception:  # noqa: BLE001
                    continue
                block = fuzzy[pi * _PAIRS_PER_PROMPT:
                              (pi + 1) * _PAIRS_PER_PROMPT]
                for i, (a, b, jac, tname) in enumerate(block, start=1):
                    v = verdicts.get(i) or {}
                    if v.get("same") and float(v.get("confidence") or 0) >= 0.8:
                        s = await session.get(type(a), a.id)
                        l = await session.get(type(b), b.id)
                        if (s is None or l is None
                                or l.merged_into_id is not None
                                or s.merged_into_id is not None):
                            continue
                        await apply_merge(session, s, l)
                        llm_merged += 1
            await session.commit()
        print(f"llm merges applied: {llm_merged}")


if __name__ == "__main__":
    asyncio.run(main())
