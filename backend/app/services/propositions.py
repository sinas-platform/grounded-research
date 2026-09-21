"""Propositions: what a document establishes, applies or decides, stored.

A proposition is one sentence in the collection's working language, with
the line span of the document version it rests on. `store` writes the set
for one document, replacing whatever that version already had, so an
extraction run twice leaves one copy. `load_jsonl` is the backfill path:
one line per document, the shape an out-of-band extraction produces —
`{"id", "holdings": [{"holding", "lines": [from, to]}], "language"}` —
resolved to the document's current version.

Which classes carry propositions is the deployment's declaration
(`document_class.propositions`); nothing here decides it.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable

from sqlalchemy import text

log = logging.getLogger("sgr.propositions")

#: A proposition longer than this is a paragraph, not a statement.
MAX_CHARS = 2000

#: Control characters are not text: a NUL inside one extracted sentence
#: refused the whole load, and nothing a reader wants sits in that range.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def normalise(items: Iterable[dict]) -> list[dict]:
    """The rows one document's extraction becomes. Pure.

    Keeps the extractor's order as the ordinal; drops empties and anything
    over MAX_CHARS; whitespace collapsed; a line span that is not two
    integers is stored as none rather than as nonsense.
    """
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        t = " ".join(_CONTROL.sub("", str(item.get("holding") or item.get("text") or "")).split())
        if not t or len(t) > MAX_CHARS:
            continue
        lines = item.get("lines") or []
        try:
            lf, lt = int(lines[0]), int(lines[-1])
            if lf < 1 or lt < lf:
                lf = lt = None
        except (TypeError, ValueError, IndexError):
            lf = lt = None
        out.append({"ordinal": len(out), "text": t, "line_from": lf, "line_to": lt})
    return out


async def store(session, document_id, items: Iterable[dict],
                language: str | None = None) -> int:
    """Write one document's propositions against its current version.

    Replaces the version's existing rows. Returns how many were written;
    zero when the document has no current version, which is not an error
    but a document nothing can be extracted from yet.
    """
    row = (await session.execute(text(
        "SELECT current_version_id FROM document WHERE id = :d"),
        {"d": str(document_id)})).first()
    if row is None or row[0] is None:
        return 0
    version_id = row[0]
    rows = normalise(items)
    await session.execute(text(
        "DELETE FROM proposition WHERE document_version_id = :v"), {"v": version_id})
    for r in rows:
        await session.execute(text("""
            INSERT INTO proposition (id, document_id, document_version_id, ordinal,
                                     text, line_from, line_to, language)
            VALUES (:id, :d, :v, :o, :t, :lf, :lt, :lang)"""),
            {"id": uuid.uuid4(), "d": str(document_id), "v": version_id,
             "o": r["ordinal"], "t": r["text"], "lf": r["line_from"],
             "lt": r["line_to"], "lang": (language or None) and str(language)[:16]})
    return len(rows)


async def load_jsonl(session, path: str, commit_every: int = 500) -> dict:
    """Backfill from a file of extractions. One commit per `commit_every`
    documents; a document missing from the collection is counted, not
    fatal."""
    docs = written = missing = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            n = await store(session, d["id"], d.get("holdings") or [], d.get("language"))
            docs += 1
            if n == 0 and not (d.get("holdings") or []):
                pass
            elif n == 0:
                missing += 1
            written += n
            if docs % commit_every == 0:
                await session.commit()
                log.info("propositions: %d documents, %d rows", docs, written)
    await session.commit()
    return {"documents": docs, "propositions": written, "without_version": missing}


if __name__ == "__main__":
    import asyncio
    import sys

    from app.db import AsyncSessionLocal

    async def _main():
        async with AsyncSessionLocal() as s:
            print(await load_jsonl(s, sys.argv[1]))

    asyncio.run(_main())
