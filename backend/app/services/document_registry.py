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
    return Registration(doc, outcome, version)
