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
from dataclasses import dataclass, field

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


# ── every declaration at once ────────────────────────────────────────────────
#
# What a reader of an answer needs is not one role but all of them, and it
# needs them for the whole answer rather than per row: the currency rules read
# a status, what replaced it and a date; the jurisdiction note reads a
# jurisdiction; a citation reads a second identifier; a claim carries a tier
# and, where anything derives it, an issuing body. Resolved one role at a time
# at each use, that is a query per document per rule, and — worse — a session
# in the middle of code that has no business holding one.
#
# So the session-holding caller resolves ONCE, at the top of the answer, and
# carries this object into the pure code. What travels is data: five mappings
# from a class's name to the property that class declared for a role, and the
# annotation names that were resolved. Nothing here can reach the database,
# which is the point — `answer_render` and the readers in `answer_structure`
# stay functions of their arguments.


@dataclass(frozen=True)
class DeclaredRoles:
    """Which field means what, for every class of one deployment.

    Each mapping is class NAME → the name of the property that class declared
    for that role. A class absent from a mapping declared none, and the only
    correct reading of that is "this class has no such field" — never a reason
    to look at other names. `DeclaredRoles()` (see `NONE`) is the honest
    answer for a caller that has resolved nothing: every role absent.
    """

    #: When the source speaks.
    date: dict[str, str] = field(default_factory=dict)
    #: Whose law or authority it belongs to.
    jurisdiction: dict[str, str] = field(default_factory=dict)
    #: In force, repealed, superseded.
    status: dict[str, str] = field(default_factory=dict)
    #: What replaced it.
    superseded_by: dict[str, str] = field(default_factory=dict)
    #: A second citable identifier, beside the class's `identifier_property`.
    alternate_identifier: dict[str, str] = field(default_factory=dict)
    #: The annotations that carry a source's standing, in the order a reader
    #: should try them. Empty when nothing derives one.
    tier_annotations: tuple[str, ...] = ()
    #: The annotation that carries who issued the source, or None.
    issuing_body_annotation: str | None = None

    def value(self, by_class: dict[str, str], props: dict | None,
              class_name: str) -> object | None:
        """The value this row holds for one of the mappings above.

        `roles.value(roles.status, props, class_name)` reads as what it is:
        the status property THIS class declared, and nothing when it declared
        none.
        """
        return value_for(props or {}, class_name, by_class)


#: Nothing is declared. The default for pure code called without a resolved
#: set — a test, an older row, a caller not yet threaded — and it disables
#: every rule that needs a declaration rather than guessing one.
NONE = DeclaredRoles()


async def resolve(session: AsyncSession) -> DeclaredRoles:
    """Every declaration this deployment made, read once.

    Seven small queries at the top of an answer, against columns that are
    indexed and rows that number in the tens. The alternative is the same
    lookups inside loops over documents, which is how the engine came to read
    a name in the first place: the cheap thing to write there was a literal.

    Each absence is announced once per process, naming what it switches off,
    because a role nothing declares and a corpus holding nothing look
    identical in the output and want opposite fixes.
    """
    from app.services.relationship_roles import standing_annotation_names

    by_role: dict[str, dict[str, str]] = {}
    for role, disabled in (
        (DATE, "no source carries a date: no currency comparison, no ordering, "
               "and an answer states the law as at the run date"),
        (JURISDICTION, "no claim says the source it cites is from another "
                       "jurisdiction than the rest of the set"),
        (STATUS, "no claim says the source it cites is no longer in force"),
        (SUPERSEDED_BY, "a note that a source is superseded cannot name what "
                        "replaced it"),
        (ALTERNATE_IDENTIFIER, "a citation carries one identifier rather than "
                               "two"),
    ):
        by_role[role] = await property_names_by_class_name(session, role)
        if not by_role[role]:
            _announce(role, disabled)

    # The tier has two declaration paths and they mean the same thing, so
    # either will do and the annotation's own declaration wins where both are
    # written. `standing_annotation_names` finds the annotation by the
    # relation its path WALKS — a deployment that declared the `standing`
    # relationship role has already said which that is — and announces its own
    # absence. What is deliberately NOT here is a fallback to a name: that
    # lives, documented, in `answer_structure.tier_of`, for a deployment that
    # has declared neither.
    tiers = await annotation_names(session, STANDING_TIER)
    if not tiers:
        tiers = await standing_annotation_names(session)

    return DeclaredRoles(
        date=by_role[DATE],
        jurisdiction=by_role[JURISDICTION],
        status=by_role[STATUS],
        superseded_by=by_role[SUPERSEDED_BY],
        alternate_identifier=by_role[ALTERNATE_IDENTIFIER],
        tier_annotations=tuple(tiers),
        issuing_body_annotation=await annotation_name(
            session, ISSUING_BODY,
            "no claim carries the issuing body of the source it cites"),
    )
