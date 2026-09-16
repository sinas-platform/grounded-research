"""Which of a deployment's fields mean what to the engine.

The companion of `relationship_roles`, for the two things that are not edges:
a document class's properties, and the annotations derived over the graph.

WHAT THIS REPLACES. The engine used to read meaning off a name. A property
whose name matched `date` was the source's date; one matching
`jurisdiction|country|member_?state` was its jurisdiction; a citation's number
was whichever of `case_number`, `celex`, `reference`, `number` happened to
exist; currency came from properties literally called `status` and
`superseded_by`; the tier was the annotation called `authority_tier` and the
issuer the one called `issuing_body`. Every one of those is a guess about
another party's vocabulary, and a guess that matches nothing looks exactly
like a corpus that holds nothing: no date, no jurisdiction note, no identifier
in the citation, no tier — and no error to say so.

HOW IT WORKS INSTEAD. A class declares, on the property itself, what that
property means (`engine_role: date`). Because the declaration sits on the
property and properties belong to classes, two classes may name the same
meaning differently — a judgment's `decision_date` and an article's
`published_on` are both the date — and neither collides with the other. A
class may not declare the same role twice; that is refused at import, where it
is written, rather than resolved arbitrarily here.

ABSENCE IS ANNOUNCED, NOT ASSUMED, exactly as for relationship roles: a role
no class declares disables what needed it and says so once, naming what
stopped. A class that declares no date has no date. The engine does not fall
back to looking at names, because that fallback is the defect.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AnnotationDefinition, DocumentClass, DocumentClassProperty
from app.schemas.package import ANNOTATION_ROLES, PROPERTY_ROLES

log = logging.getLogger("sgr.declared_roles")

#: When the source speaks: currency, ordering, and the answer's stated-as-at.
DATE = "date"
#: Whose law or authority the source belongs to.
JURISDICTION = "jurisdiction"
#: In force, repealed, superseded.
STATUS = "status"
#: What replaced it, where the status says it was replaced.
SUPERSEDED_BY = "superseded_by"
#: A second citable identifier, beside the class's own `identifier_property`.
ALTERNATE_IDENTIFIER = "alternate_identifier"

#: How high a source stands, 1 highest.
STANDING_TIER = "standing_tier"
#: Who issued it.
ISSUING_BODY = "issuing_body"

assert set(PROPERTY_ROLES) == {
    DATE, JURISDICTION, STATUS, SUPERSEDED_BY, ALTERNATE_IDENTIFIER
}
assert set(ANNOTATION_ROLES) == {STANDING_TIER, ISSUING_BODY}

_announced: set[str] = set()


def _announce(role: str, disabled: str) -> None:
    """Say once that a role is undeclared, and what that switches off."""
    if role in _announced:
        return
    _announced.add(role)
    log.warning(
        "no field declares the %r role; %s is off until a class declares it",
        role, disabled,
    )


async def property_names_by_class(
    session: AsyncSession, role: str
) -> dict[uuid.UUID, str]:
    """Class id → the name of the property that class declares for `role`.

    A class missing from the mapping has not declared one, and the caller must
    treat that as "this class has no such field", never as a reason to guess.
    """
    rows = (
        await session.execute(
            select(DocumentClassProperty.document_class_id,
                   DocumentClassProperty.name)
            .where(DocumentClassProperty.engine_role == role)
        )
    ).all()
    return {class_id: name for class_id, name in rows}


async def property_names_by_class_name(
    session: AsyncSession, role: str
) -> dict[str, str]:
    """Class NAME → the property that class declares for `role`.

    The read paths carry a class name on each row rather than its id, so this
    is the shape they need; `property_names_by_class` stays for callers that
    hold ids.
    """
    rows = (
        await session.execute(
            select(DocumentClass.name, DocumentClassProperty.name)
            .join(DocumentClassProperty,
                  DocumentClassProperty.document_class_id == DocumentClass.id)
            .where(DocumentClassProperty.engine_role == role)
        )
    ).all()
    return {class_name: prop for class_name, prop in rows}


def value_for(
    props: dict, class_name: str, by_class: dict[str, str]
) -> object | None:
    """The value a row holds for a role, or None when its class declares none.

    `props` is the document's stored properties and `by_class` the mapping
    from `property_names_by_class_name`. Deliberately dumb: the only way to
    reach a value is through a name the deployment declared.
    """
    name = by_class.get(class_name)
    if not name:
        return None
    return props.get(name)


async def annotation_names(session: AsyncSession, role: str) -> list[str]:
    """The names of the annotations declared for `role`, most useful first.

    Empty when nothing declares it, which the caller must treat as the feature
    being off rather than as an empty result.
    """
    rows = (
        await session.execute(
            select(AnnotationDefinition.name)
            .where(AnnotationDefinition.engine_role == role)
        )
    ).scalars().all()
    return list(rows)


async def annotation_name(
    session: AsyncSession, role: str, disabled: str
) -> str | None:
    """The single annotation declared for `role`, announcing an absence."""
    names = await annotation_names(session, role)
    if not names:
        _announce(role, disabled)
        return None
    return names[0]
