"""Document registration — the single write path into the corpus.

Grove is the system of record: every route that accepts content (bulk zip,
single upload, any future connector) registers through here, so identity
and deduplication have exactly one implementation. Identity strongest
first: the connector's natural key (source, external_ref) decides
create-vs-new-version; filename is the external ref of last resort;
byte-identical content under any name is a duplicate, not a document.

There used to be a second write path — a Sinas collection whose
post-upload function registered files keyed on collection_file_id, with no
content hash and no source identity. Two write paths with different
identity semantics is how a corpus rots; that path is gone.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentVersion
from app.services.front_matter import split_front_matter
from app.services.toc import normalize_line_density


@dataclass
class Registration:
    document: Document | None
    outcome: str  # created | new_version | unchanged | duplicate
    version: int | None = None


async def register_document(
    session: AsyncSession,
    *,
    filename: str,
    content: str,
    owner_id: uuid.UUID | None,
    roles: list[str] | None,
    source: str | None = None,
    external_ref: str | None = None,
    staged: bool = False,
    document_class_id: uuid.UUID | None = None,
) -> Registration:
    """Register one document. Does not commit; the caller owns the
    transaction and the decision to spawn processing."""
    content = content.replace("\x00", "").replace("\\u0000", "")
    content = normalize_line_density(content)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    if source and not external_ref:
        external_ref = filename

    existing = None
    if source and external_ref:
        existing = (await session.execute(
            select(Document).where(Document.source == source,
                                   Document.external_ref == external_ref)
        )).scalars().first()
    if existing is None:
        existing = (await session.execute(
            select(Document).where(Document.filename == filename)
        )).scalars().first()

    if existing is not None:
        doc = existing
        if document_class_id is not None and doc.document_class_id is None:
            doc.document_class_id = document_class_id
            doc.classification_confidence = 1.0
        latest = (await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == doc.id)
            .order_by(DocumentVersion.version.desc()).limit(1)
        )).scalars().first()
        if latest is not None and latest.content_hash == content_hash:
            return Registration(doc, "unchanged", latest.version)
        version = (latest.version + 1) if latest else 1
        outcome = "new_version"
    else:
        same = (await session.execute(
            select(Document)
            .join(DocumentVersion,
                  DocumentVersion.id == Document.current_version_id)
            .where(DocumentVersion.content_hash == content_hash)
        )).scalars().first()
        if same is not None:
            return Registration(same, "duplicate", None)
        doc = Document(filename=filename,
                       source=source or None,
                       external_ref=external_ref if source else None,
                       owner_id=owner_id,
                       roles=roles or [], staged=staged,
                       document_class_id=document_class_id,
                       classification_confidence=(
                           1.0 if document_class_id else None))
        session.add(doc)
        await session.flush()
        version = 1
        outcome = "created"

    dv = DocumentVersion(document_id=doc.id, version=version,
                         content_md=content, content_hash=content_hash)
    session.add(dv)
    await session.flush()
    doc.current_version_id = dv.id
    await _seed_front_matter_values(session, doc, dv, content)
    return Registration(doc, outcome, version)


async def _seed_front_matter_values(session, doc, dv, content: str) -> None:
    """What the source already knows is written, not guessed.

    For every front-matter key matching a property of the document's class,
    a locked manual PropertyValue at confidence 1.0 — the exporter's exact
    value. The resolver matches citations on these keys, so a model
    transcription slip here is a missed link; a declared value cannot slip.

    Only when the class is known at registration (properties belong to a
    class), and never over an existing value: re-registration and re-extract
    both leave earlier values standing, the same rule the one-shot applies.
    """
    if doc.document_class_id is None:
        return
    fm, _body = split_front_matter(content)
    if not fm:
        return
    from app.models import DocumentClassProperty, PropertyValue
    from app.services.front_matter import front_matter_property_values

    properties = (await session.execute(
        select(DocumentClassProperty).where(
            DocumentClassProperty.document_class_id == doc.document_class_id)
    )).scalars().all()
    seeded = front_matter_property_values(fm, properties)
    if not seeded:
        return
    existing = {pid for (pid,) in (await session.execute(
        select(PropertyValue.property_id).where(
            PropertyValue.document_id == doc.id)
    )).all()}
    for prop_id, value in seeded:
        if prop_id in existing:
            continue
        session.add(PropertyValue(
            property_id=prop_id, document_id=doc.id,
            document_version_id=dv.id, value=value,
            method="manual", locked=True, confidence=1.0,
            reason="front-matter declared"))
    await session.flush()
