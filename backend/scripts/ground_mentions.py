"""Run the grounding gate over documents with active mentions.

Usage (from the backend directory):
    python -m scripts.ground_mentions [--document <filename>] [--dry-run]
Gates every document with active mentions unless --document is given.
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
    from app.services.grounding_gate import ground_documents

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

    reports = await ground_documents(doc_ids, write=not args.dry_run)
    for r in reports:
        print(json.dumps(r))
    total = {k: sum(r.get(k, 0) for r in reports) for k in
             ("active", "verbatim", "kept", "rejected", "unparsed_kept",
              "no_surface_skipped", "llm_calls")}
    print(json.dumps({"TOTAL": total}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
