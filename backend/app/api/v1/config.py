"""Configuration introspection and CRUD.

Read endpoints (`get_*`) are how SGR agents discover the domain model at runtime.
Admin endpoints require `sgr.admin:all`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_permission
from app.db import get_session
from app.models import (
    DocumentClass,
    DocumentClassEntityType,
    DocumentClassProperty,
    DossierClass,
    DossierClassProperty,
    EntityType,
    RelationshipDefinition,
    RelationshipState,
)
from app.schemas.config import (
    DocumentClassCreate,
    DocumentClassOut,
    DocumentClassPropertyIn,
    DocumentClassPropertyOut,
    DocumentClassUpdate,
    DossierClassCreate,
    DossierClassOut,
    DossierClassPropertyIn,
    DossierClassPropertyOut,
    DossierClassUpdate,
    EntityTypeIn,
    EntityTypeOut,
    RelationshipDefinitionIn,
    RelationshipDefinitionOut,
    RelationshipStateIn,
    RelationshipStateOut,
    slugify,
)

router = APIRouter(prefix="/config", tags=["config"])


# ─────────────────────────── document classes ───────────────────────────
@router.get("/document-classes", response_model=list[DocumentClassOut])
async def list_document_classes(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(DocumentClass))).scalars().all()
    return rows


@router.post(
    "/document-classes",
    response_model=DocumentClassOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_document_class(
    payload: DocumentClassCreate, session: AsyncSession = Depends(get_session)
):
    data = payload.model_dump()
    if not data.get("slug"):
        data["slug"] = slugify(data["name"])
    row = DocumentClass(**data)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.get("/document-classes/{class_id}", response_model=DocumentClassOut)
async def get_document_class(class_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    row = await session.get(DocumentClass, class_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document class not found")
    return row


@router.put(
    "/document-classes/{class_id}",
    response_model=DocumentClassOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def update_document_class(
    class_id: uuid.UUID,
    payload: DocumentClassUpdate,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(DocumentClass, class_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document class not found")
    # slug is intentionally not in DocumentClassUpdate — it's immutable.
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/document-classes/{class_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_document_class(class_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    row = await session.get(DocumentClass, class_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document class not found")
    await session.delete(row)
    await session.commit()


# Properties under a class
@router.get(
    "/document-classes/{class_id}/properties",
    response_model=list[DocumentClassPropertyOut],
)
async def list_class_properties(
    class_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    rows = (
        await session.execute(
            select(DocumentClassProperty).where(DocumentClassProperty.document_class_id == class_id)
        )
    ).scalars().all()
    return rows


@router.post(
    "/document-classes/{class_id}/properties",
    response_model=DocumentClassPropertyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_class_property(
    class_id: uuid.UUID,
    payload: DocumentClassPropertyIn,
    session: AsyncSession = Depends(get_session),
):
    if (await session.get(DocumentClass, class_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document class not found")
    row = DocumentClassProperty(document_class_id=class_id, **payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.put(
    "/document-classes/properties/{property_id}",
    response_model=DocumentClassPropertyOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def update_class_property(
    property_id: uuid.UUID,
    payload: DocumentClassPropertyIn,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(DocumentClassProperty, property_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "property not found")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/document-classes/properties/{property_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_class_property(
    property_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = await session.get(DocumentClassProperty, property_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "property not found")
    await session.delete(row)
    await session.commit()


# Entity types attached to a document class
@router.get("/document-classes/{class_id}/entity-types", response_model=list[EntityTypeOut])
async def list_class_entity_types(
    class_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    rows = (
        await session.execute(
            select(EntityType)
            .join(DocumentClassEntityType, DocumentClassEntityType.entity_type_id == EntityType.id)
            .where(DocumentClassEntityType.document_class_id == class_id)
        )
    ).scalars().all()
    return rows


@router.post(
    "/document-classes/{class_id}/entity-types/{entity_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def attach_entity_type(
    class_id: uuid.UUID,
    entity_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    session.add(
        DocumentClassEntityType(document_class_id=class_id, entity_type_id=entity_type_id)
    )
    await session.commit()


# ─────────────────────────── entity types ───────────────────────────
@router.get("/entity-types", response_model=list[EntityTypeOut])
async def list_entity_types(session: AsyncSession = Depends(get_session)):
    return (await session.execute(select(EntityType))).scalars().all()


@router.post(
    "/entity-types",
    response_model=EntityTypeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_entity_type(
    payload: EntityTypeIn, session: AsyncSession = Depends(get_session)
):
    row = EntityType(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.put(
    "/entity-types/{entity_type_id}",
    response_model=EntityTypeOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def update_entity_type(
    entity_type_id: uuid.UUID,
    payload: EntityTypeIn,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(EntityType, entity_type_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity type not found")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/entity-types/{entity_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_entity_type(
    entity_type_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = await session.get(EntityType, entity_type_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entity type not found")
    await session.delete(row)
    await session.commit()


# ─────────────────────────── relationship definitions ───────────────────────────
@router.get("/relationship-definitions", response_model=list[RelationshipDefinitionOut])
async def list_relationship_definitions(session: AsyncSession = Depends(get_session)):
    return (await session.execute(select(RelationshipDefinition))).scalars().all()


@router.post(
    "/relationship-definitions",
    response_model=RelationshipDefinitionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_relationship_definition(
    payload: RelationshipDefinitionIn, session: AsyncSession = Depends(get_session)
):
    row = RelationshipDefinition(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.put(
    "/relationship-definitions/{def_id}",
    response_model=RelationshipDefinitionOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def update_relationship_definition(
    def_id: uuid.UUID,
    payload: RelationshipDefinitionIn,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(RelationshipDefinition, def_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "relationship definition not found")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/relationship-definitions/{def_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_relationship_definition(
    def_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = await session.get(RelationshipDefinition, def_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "relationship definition not found")
    await session.delete(row)
    await session.commit()


@router.get(
    "/relationship-definitions/{def_id}/states",
    response_model=list[RelationshipStateOut],
)
async def list_relationship_states(
    def_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    rows = (
        await session.execute(
            select(RelationshipState).where(RelationshipState.relationship_definition_id == def_id)
        )
    ).scalars().all()
    return rows


@router.post(
    "/relationship-definitions/{def_id}/states",
    response_model=RelationshipStateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_relationship_state(
    def_id: uuid.UUID,
    payload: RelationshipStateIn,
    session: AsyncSession = Depends(get_session),
):
    if (await session.get(RelationshipDefinition, def_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "relationship definition not found")
    row = RelationshipState(relationship_definition_id=def_id, **payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.put(
    "/relationship-states/{state_id}",
    response_model=RelationshipStateOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def update_relationship_state(
    state_id: uuid.UUID,
    payload: RelationshipStateIn,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(RelationshipState, state_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "relationship state not found")
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/relationship-states/{state_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_relationship_state(
    state_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = await session.get(RelationshipState, state_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "relationship state not found")
    await session.delete(row)
    await session.commit()


# ─────────────────────────── dossier classes ───────────────────────────
@router.get("/dossier-classes", response_model=list[DossierClassOut])
async def list_dossier_classes(session: AsyncSession = Depends(get_session)):
    return (await session.execute(select(DossierClass))).scalars().all()


@router.post(
    "/dossier-classes",
    response_model=DossierClassOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_dossier_class(
    payload: DossierClassCreate, session: AsyncSession = Depends(get_session)
):
    data = payload.model_dump()
    if not data.get("slug"):
        data["slug"] = slugify(data["name"])
    row = DossierClass(**data)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.put(
    "/dossier-classes/{class_id}",
    response_model=DossierClassOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def update_dossier_class(
    class_id: uuid.UUID,
    payload: DossierClassUpdate,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(DossierClass, class_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dossier class not found")
    # slug is intentionally not in DossierClassUpdate — it's immutable.
    for k, v in payload.model_dump().items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete(
    "/dossier-classes/{class_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def delete_dossier_class(class_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    row = await session.get(DossierClass, class_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dossier class not found")
    await session.delete(row)
    await session.commit()


@router.get(
    "/dossier-classes/{class_id}/properties",
    response_model=list[DossierClassPropertyOut],
)
async def list_dossier_class_properties(
    class_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    rows = (
        await session.execute(
            select(DossierClassProperty).where(DossierClassProperty.dossier_class_id == class_id)
        )
    ).scalars().all()
    return rows


@router.post(
    "/dossier-classes/{class_id}/properties",
    response_model=DossierClassPropertyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_dossier_class_property(
    class_id: uuid.UUID,
    payload: DossierClassPropertyIn,
    session: AsyncSession = Depends(get_session),
):
    if (await session.get(DossierClass, class_id)) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dossier class not found")
    row = DossierClassProperty(dossier_class_id=class_id, **payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
