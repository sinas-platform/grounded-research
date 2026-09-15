"""Apply a class's declared properties to documents already stored.

Separate from ingestion on purpose. Ingestion reaches a document once, when it
arrives, and the disagreements this exists to correct are in documents that
arrived long ago: on one corpus, 693 stored dates differ from the date printed
in their own header, and not one of them would be revisited by any ingestion
run. A correction that only works on arrival is the shape this codebase keeps
meeting, where the fix exists and reaches nothing.

Dry by default. `--apply` writes.

  python -m scripts.apply_declared_properties --class "Court Decision"
  python -m scripts.apply_declared_properties --class "Court Decision" --apply

Every replaced value keeps the one it replaced in `reason`, in the shape the
7 September joined-case split used, so this is reversible the same way and a
reader who finds a replaced value knows where the old one is.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import Document, DocumentVersion
from app.models.config import DocumentClass, DocumentClassProperty
from app.models.runtime import PropertyValue
from app.services.declared_properties import (
    Existing, plan_declared_values, replacement_reason)
from app.services.front_matter import split_front_matter
from app.services.ingestion_oneshot import wrap_property_value


async def run(class_name: str, apply: bool, limit: int | None) -> dict:
    totals = {"documents": 0, "no_front_matter": 0, "written": 0,
              "replaced": 0, "kept_manual": 0, "kept_locked": 0,
              "kept_existing": 0, "unchanged": 0}
    today = datetime.now(timezone.utc).date().isoformat()

    async with AsyncSessionLocal() as session:
        cls = (await session.execute(
            select(DocumentClass).where(DocumentClass.name == class_name)
        )).scalars().first()
        if cls is None:
            raise SystemExit(f"no document class named {class_name!r}")
        mapping = cls.declared_properties or []
        if not mapping:
            raise SystemExit(
                f"{class_name!r} declares no declared_properties; nothing to "
                "apply. That is a change to the deployment's package file, "
                "not to this script.")

        props = (await session.execute(
            select(DocumentClassProperty).where(
                DocumentClassProperty.document_class_id == cls.id)
        )).scalars().all()
        by_name = {p.name: p for p in props}

        q = select(Document.id, DocumentVersion.content_md,
                   DocumentVersion.id).join(
            DocumentVersion, DocumentVersion.id == Document.current_version_id
        ).where(Document.document_class_id == cls.id)
        if limit:
            q = q.limit(limit)
        rows = (await session.execute(q)).all()

        for doc_id, content, version_id in rows:
            totals["documents"] += 1
            header, _body = split_front_matter(content or "")
            if not header:
                totals["no_front_matter"] += 1
                continue
            held = (await session.execute(
                select(PropertyValue).where(
                    PropertyValue.document_id == doc_id))).scalars().all()
            by_prop_id = {r.property_id: r for r in held}
            existing = {}
            for name, p in by_name.items():
                r = by_prop_id.get(p.id)
                if r is not None:
                    existing[name] = Existing(
                        value=str((r.value or {}).get("_", "")),
                        method=r.method, locked=bool(r.locked))

            plan = plan_declared_values(header, mapping, existing)
            for k, v in plan.as_dict().items():
                if k in totals:
                    totals[k] += v
            if not apply:
                continue
            for w in plan.write:
                p = by_name.get(w.property)
                if p is None:
                    continue
                row = by_prop_id.get(p.id)
                reason = replacement_reason(today, w.replaces)
                if row is not None:
                    row.value = wrap_property_value(w.value)
                    row.method = w.method
                    row.confidence = w.confidence
                    row.document_version_id = version_id
                    row.reason = reason
                else:
                    session.add(PropertyValue(
                        property_id=p.id, document_id=doc_id,
                        document_version_id=version_id,
                        value=wrap_property_value(w.value),
                        method=w.method, confidence=w.confidence,
                        reason=reason))
        if apply:
            await session.commit()
    return totals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="class_name", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="write; without it nothing is changed")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    totals = asyncio.run(run(a.class_name, a.apply, a.limit))
    verb = "wrote" if a.apply else "would write"
    print(f"{a.class_name}: {totals['documents']} documents, "
          f"{totals['no_front_matter']} with no front matter")
    print(f"  {verb} {totals['written']} values, "
          f"{totals['replaced']} of them replacing an extracted one")
    print(f"  left alone: {totals['kept_manual']} manual, "
          f"{totals['kept_locked']} locked, "
          f"{totals['kept_existing']} fill-only with a value, "
          f"{totals['unchanged']} already agreeing")


if __name__ == "__main__":
    main()
