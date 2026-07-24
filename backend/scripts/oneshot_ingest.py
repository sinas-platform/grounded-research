#!/usr/bin/env python3
"""Drive the one-shot ingestion path from the CLI.

  oneshot_ingest.py --unclassified --limit 10          # classify + extract
  oneshot_ingest.py --unclassified --limit 5 --dry-run # no writes
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import AsyncSessionLocal  # noqa: E402
from app.models import Document  # noqa: E402
from app.services.ingestion_oneshot import oneshot_ingest  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unclassified", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    if args.ids:
        ids = [uuid.UUID(i) for i in args.ids]
    else:
        async with AsyncSessionLocal() as session:
            q = select(Document.id)
            if args.unclassified:
                q = q.where(Document.document_class_id.is_(None))
            q = q.limit(args.limit)
            ids = list((await session.execute(q)).scalars())
    print(f"{len(ids)} documents; write={not args.dry_run}", flush=True)
    reports = await oneshot_ingest(ids, write=not args.dry_run, concurrency=args.concurrency)
    for r in reports:
        print(json.dumps(r, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
