"""The human identity of a document: its extracted title, and what its class
says about it.

Filenames are frequently bare numeric ids, so any surface a person reads
should join the title extracted at ingestion instead of echoing the storage
name. One correlated subquery, shared by every endpoint that serves document
identity, so the coalesce over the two stored value shapes lives in exactly
one place.

Beside the title sit the class's own declared properties — case number,
CELEX, ECLI, decision date, status, whatever the deployment declared. A
reader citing a source needs them all at once and must not have to ask for
them one document at a time, so they are loaded in a single query and
unwrapped to the scalars they store.
"""

import uuid

from sqlalchemy import Select, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentClassProperty, PropertyValue
from app.models.config import DocumentClass
from app.services.answer_structure import unwrap


def document_title_subquery() -> Select:
    """Correlated scalar subquery: the document's name property, or NULL.

    WHICH property holds the name is the class's to say. This matched the
    literal property name ``"title"``, so a deployment whose classes call it
    anything else — ``heading``, ``subject``, ``titre`` — served the storage
    filename on every reader surface and had nothing to say why. The class
    already declares ``name_property`` for exactly this ("the property
    holding the document's name, for claims that name a source in prose
    rather than by its identifier"); it is read here instead. A class that
    declares none has no name property, and NULL is the honest answer.

    Property values are stored either wrapped (``{"_": "The title"}``) or as
    a bare JSON scalar, so both shapes are coalesced — same treatment the
    entity key replay uses. Correlates on ``Document.id``, so it composes
    into any statement that selects from ``Document``.
    """
    return (
        select(
            func.coalesce(
                PropertyValue.value["_"].astext,
                PropertyValue.value.op("#>>")(literal_column("'{}'::text[]")),
            )
        )
        .join(
            DocumentClassProperty,
            DocumentClassProperty.id == PropertyValue.property_id,
        )
        .join(
            DocumentClass,
            DocumentClass.id == DocumentClassProperty.document_class_id,
        )
        .where(PropertyValue.document_id == Document.id)
        # The property this document's OWN class names, not a property that
        # happens to share a name with another class's.
        .where(DocumentClass.id == Document.document_class_id)
        .where(DocumentClassProperty.name == DocumentClass.name_property)
        # A document holds one title value today, but LIMIT 1 without an
        # order would hand back an arbitrary row the day a re-ingestion
        # leaves two. Newest wins, id as the tiebreak, so the identity an
        # API serves cannot flap between requests.
        .order_by(PropertyValue.created_at.desc(), PropertyValue.id)
        .limit(1)
        .scalar_subquery()
    )


async def properties_for_documents(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict]:
    """The declared property values of each document, keyed by document id.

    `{case_number: "C-1/20", ecli: "ECLI:EU:C:2021:1", decision_date:
    "2021-05-04", status: "in_force", ...}` — the property NAMES the
    deployment declared on the class, with the stored wrapper taken off so a
    reader sees the scalar rather than `{"_": ...}`. Nothing here knows which
    properties exist: a class that declares none yields an empty dict, which
    is what a document with nothing extracted also yields.

    A property holding several values keeps the last one written; a document
    is expected to carry one value per declared property, and picking a list
    to serve would change the field's type per row.
    """
    if not document_ids:
        return {}
    rows = (
        await session.execute(
            select(PropertyValue.document_id, DocumentClassProperty.name,
                   PropertyValue.value)
            .join(DocumentClassProperty,
                  DocumentClassProperty.id == PropertyValue.property_id)
            .where(PropertyValue.document_id.in_(document_ids))
            .order_by(PropertyValue.created_at, PropertyValue.id)
        )
    ).all()
    out: dict[uuid.UUID, dict] = {}
    for doc_id, name, value in rows:
        if value is None:
            continue
        out.setdefault(doc_id, {})[str(name)] = unwrap(value)
    return out
