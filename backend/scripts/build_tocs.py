"""Backfill deterministic TOCs over every document with stored content.

Replaces whatever the old pipeline wrote (audited: 52% invented entries,
inconsistent shapes) with the verbatim-headings-plus-line-ranges schema
from services/toc. Free: no model calls.

Usage (from the backend directory):
    python -m scripts.build_tocs [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.models import Document
    from app.models.runtime import DocumentVersion
    from app.services.toc import derive_toc

    stats = {"docs": 0, "with_entries": 0, "empty": 0, "no_content": 0, "entries": 0}
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Document.id).order_by(Document.created_at)
            )
        ).scalars().all()
    for doc_id in rows:
        async with AsyncSessionLocal() as session:
            doc = await session.get(Document, doc_id)
            if doc is None:
                continue
            stats["docs"] += 1
            content = None
            if doc.current_version_id:
                content = (
                    await session.execute(
                        select(DocumentVersion.content_md).where(
                            DocumentVersion.id == doc.current_version_id
                        )
                    )
                ).scalar_one_or_none()
            if not (content or "").strip():
                stats["no_content"] += 1
                continue
            entries = derive_toc(content)
            stats["with_entries" if entries else "empty"] += 1
            stats["entries"] += len(entries)
            if not args.dry_run:
                doc.toc = {"entries": entries}
                await session.commit()
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
