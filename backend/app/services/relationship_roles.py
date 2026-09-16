"""Which of a deployment's relationships mean what to the engine.

An engine feature that needs an edge of a particular MEANING — one source
replaces another, one issuing body outranks another, a document is the full
text of what it records — cannot know what a deployment calls it. Two features
used to guess: the superseded-authority check matched a literal list of names,
and the authority tier was found by the name of the annotation that walks the
hierarchy. Both are silent when the guess is wrong. A deployment naming its
relations anything else got no findings, which is byte-identical to a corpus
that records no supersession, and no tier, which is byte-identical to a source
with no issuing body.

So the deployment declares it, once, in `spec.relationship_roles` of its
package (see `app.schemas.package.PackageRelationshipRoles`), the import
writes it onto `relationship_definition.engine_role`, and this module is what
every reader goes through. The role names here are the engine's own: `STANDING`
says what the engine does with such an edge, not what any collection calls it.

ABSENCE IS ANNOUNCED, NOT ASSUMED. A role nothing declares disables the
feature that needed it and says so in the log, once per process per role, with
the name of what stopped working. That is the whole point: the failure this
module exists to remove was a check that returned nothing and looked like a
check that found nothing.

The one role with a fallback is `FULL_TEXT`, and the fallback is structural
rather than a guessed name — see `full_text_definition_ids`.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AnnotationDefinition, RelationshipDefinition
from app.schemas.package import ENGINE_ROLES

log = logging.getLogger("sgr.relationship_roles")

#: One issuing body outranks another. The engine reads it to find which
#: annotation derives a source's tier.
STANDING = "standing"
#: One source replaces another.
SUPERSESSION = "supersession"
#: A document IS the full text of the subject it records.
FULL_TEXT = "full_text"

assert set(ENGINE_ROLES) == {STANDING, SUPERSESSION, FULL_TEXT}

#: What has already been announced, so a run does not repeat itself once per
#: answer. Process-wide on purpose: the declaration is config, and config does
#: not change under a running process.
_announced: set[str] = set()


def _announce(key: str, why: str, disabled: str) -> None:
    """Say once why a feature is off, and what is off."""
    if key in _announced:
        return
    _announced.add(key)
    log.warning("%s, so %s.", why, disabled)


async def definition_ids(session: AsyncSession, role: str) -> list[uuid.UUID]:
    """Ids of the definitions the deployment gave this role. May be empty."""
    if role not in ENGINE_ROLES:
        raise ValueError(f"unknown engine role {role!r}")
    return list(
        (
            await session.execute(
                select(RelationshipDefinition.id).where(
                    RelationshipDefinition.engine_role == role
                )
            )
        ).scalars()
    )


async def full_text_definition_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Definitions linking a document to the subject it is the full text of.

    Declared first. Undeclared, the fallback is STRUCTURAL and needs no name:
    a definition whose source is a document class, whose target is an entity
    type, and whose cardinality is "one" says exactly this — each document of
    that class is the full text of one entity. That is not an invention here;
    it is the identity contract `annotations_for_documents` and
    `backfill_full_text_entities` already read, in those words, and it is what
    the two literal names this replaced were declared as.

    So a package that declares no `full_text` role keeps today's behaviour,
    and it keeps it without the engine knowing one name a deployment chose.
    The role is still worth declaring: it says WHICH identity definitions the
    engine may follow, where the structural rule takes all of them.
    """
    declared = await definition_ids(session, FULL_TEXT)
    if declared:
        return declared
    log.info(
        "no relationship definition declares the %r role; falling back to "
        "identity definitions (document class → entity type, cardinality "
        "one). Declare spec.relationship_roles.%s to name them explicitly.",
        FULL_TEXT, FULL_TEXT,
    )
    return list(
        (
            await session.execute(
                select(RelationshipDefinition.id).where(
                    RelationshipDefinition.source_ref_type == "document_class",
                    RelationshipDefinition.target_ref_type == "entity_type",
                    RelationshipDefinition.cardinality == "one",
                )
            )
        ).scalars()
    )


async def supersession_definition_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Definitions saying one source replaces another, or none.

    No fallback exists and none is invented. An entity→entity edge between two
    members of the same type is the shape of supersession, but it is equally
    the shape of citation and of appeal, and a check that told a reader an
    authority was superseded because it was cited would be worse than no
    check. Undeclared, the feature is off and says so.
    """
    ids = await definition_ids(session, SUPERSESSION)
    if not ids:
        _announce(
            SUPERSESSION,
            f"no relationship definition declares the {SUPERSESSION!r} role "
            f"(package: spec.relationship_roles.{SUPERSESSION})",
            "no answer is checked for citing a source the collection records "
            "as superseded",
        )
    return ids


async def annotation_names(session: AsyncSession, role: str) -> list[str]:
    """Annotations whose path walks a relation carrying this role.

    An annotation is engine-meaningful through the edges it walks, not through
    what it is called: the annotation that derives a source's standing is the
    one whose path crosses a `standing` relation, whatever name the deployment
    gave it. Sorted, so a caller reading several gets the same order twice.

    Empty when the role is undeclared, when no annotation walks it, or when a
    path no longer parses — an annotation the engine cannot read is one it
    must not silently treat as absent-by-name either, so the parse failure is
    logged where the caller can see it.
    """
    from app.services.annotations import AnnotationConfigError, parse_path

    ids = await definition_ids(session, role)
    if not ids:
        return []
    names_with_role = set(
        (
            await session.execute(
                select(RelationshipDefinition.name).where(
                    RelationshipDefinition.id.in_(ids)
                )
            )
        ).scalars()
    )
    out: list[str] = []
    for d in (await session.execute(select(AnnotationDefinition))).scalars():
        try:
            path = parse_path(d.path)
        except AnnotationConfigError as exc:
            log.warning("annotation %r has an unreadable path: %s", d.name, exc)
            continue
        if path.names & names_with_role:
            out.append(d.name)
    return sorted(out)


async def standing_annotation_names(session: AsyncSession) -> list[str]:
    """The annotations that derive a source's standing, by what they walk.

    Empty is announced with its actual cause, because the two causes want
    different fixes: a deployment that declared no `standing` relation has to
    write the role, and one that declared the role but no annotation walking
    it has to write the annotation.
    """
    names = await annotation_names(session, STANDING)
    if names:
        return names
    declared = await definition_ids(session, STANDING)
    _announce(
        f"{STANDING}:annotation" if declared else STANDING,
        (f"{len(declared)} relationship definition(s) carry the {STANDING!r} "
         "role but no annotation's path walks one")
        if declared else
        (f"no relationship definition declares the {STANDING!r} role "
         f"(package: spec.relationship_roles.{STANDING})"),
        "no claim carries the tier of the source it cites",
    )
    return names
