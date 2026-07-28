"""Resolve unlinked entity mentions.

Usage (from the backend directory):
    python -m scripts.resolve_mentions [--document <filename>] [--dry-run]
Resolves every document with unlinked mentions unless --document is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--document")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.models import Document
    from app.services.entity_resolver import resolve_unlinked

    doc_ids = None
    if args.document:
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(Document.id).where(Document.filename == args.document)
                )
            ).scalar_one_or_none()
            if row is None:
                print(f"no document {args.document!r}")
                return 1
            doc_ids = [row]

    reports = await resolve_unlinked(doc_ids, write=not args.dry_run)
    for r in reports:
        print(json.dumps(r))
    total = {k: sum(r.get(k, 0) for r in reports) for k in
             ("unlinked", "natural_key", "exact", "adjudicated", "created",
              "proposed", "left_unlinked", "llm_calls", "prompt_chars", "reply_chars")}
    print(json.dumps({"TOTAL": total}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
