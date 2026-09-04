"""The human identity of a document: its extracted title.

Filenames are frequently bare numeric ids, so any surface a person reads
should join the title extracted at ingestion instead of echoing the storage
name. One correlated subquery, shared by every endpoint that serves document
identity, so the coalesce over the two stored value shapes lives in exactly
one place.
"""

from sqlalchemy import Select, func, literal_column, select

from app.models import Document, DocumentClassProperty, PropertyValue


def document_title_subquery() -> Select:
    """Correlated scalar subquery: the document's extracted title, or NULL.

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
        .where(PropertyValue.document_id == Document.id)
        .where(DocumentClassProperty.name == "title")
        .limit(1)
        .scalar_subquery()
    )
