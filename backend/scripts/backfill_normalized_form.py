"""Populate entity.normalized_form, and merge the collisions that blocks.

Sits between migrations 0022 (adds the column) and 0023 (makes it unique).
normalize() folds accents; the identity it produces cannot be expressed in
plain Postgres, so the backfill runs here rather than inside the migration.

    python -m scripts.backfill_normalized_form --report   # counts only
    python -m scripts.backfill_normalized_form --apply    # write + merge

Collisions are merged survivor-first by created_at, the same rule the dedup
tool uses, and mentions are repointed — after which 0023 can create the
unique index.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import select, update

from app.db import AsyncSessionLocal
from app.models import Entity
from app.services.entity_resolver import normalize

CHUNK = 5000


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Entity.id, Entity.entity_type_id, Entity.canonical_form,
                   Entity.created_at)
            .where(Entity.merged_into_id.is_(None))
        )).all()

    groups: dict[tuple, list] = defaultdict(list)
    for eid, tid, cf, created in rows:
        groups[(tid, normalize(cf or ""))].append((eid, created))
    collisions = {k: v for k, v in groups.items() if len(v) > 1 and k[1]}
    extra = sum(len(v) - 1 for v in collisions.values())
    print(f"live entities:        {len(rows)}")
    print(f"colliding identities: {len(collisions)}")
    print(f"rows to merge away:   {extra}")

    if args.report or not args.apply:
        return

    from app.entity_dedup import apply_merge

    async with AsyncSessionLocal() as s:
        merged = 0
        for (tid, norm), members in collisions.items():
            members.sort(key=lambda x: x[1])
            survivor = await s.get(Entity, members[0][0])
            for eid, _ in members[1:]:
                loser = await s.get(Entity, eid)
                if loser is None or loser.merged_into_id is not None:
                    continue
                await apply_merge(s, survivor, loser)
                merged += 1
        await s.commit()
        print(f"merged: {merged}")

        done = 0
        for i in range(0, len(rows), CHUNK):
            for eid, tid, cf, _ in rows[i:i + CHUNK]:
                await s.execute(update(Entity).where(Entity.id == eid)
                                .values(normalized_form=normalize(cf or "") or None))
                done += 1
            await s.commit()
            print(f"  backfilled {min(i + CHUNK, len(rows))}/{len(rows)}", flush=True)
        print(f"normalized_form written for {done} entities")


asyncio.run(main())
