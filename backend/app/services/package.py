"""GrovePackage import / export service.

Idempotent apply of a GrovePackage YAML against the Grove DB. Rows installed
by a package are tagged `managed_by = pkg:<name>` so a re-import can prune
resources that were removed from the manifest.

Order of operations on apply (single transaction):
  1. Parse + structural validate the YAML.
  2. Cross-ref validate (relationships reference existing names, etc.).
  3. entity_types → document_classes (+ properties + entity-type links)
     → dossier_classes (+ properties + document-class links)
     → relationship_definitions (+ states)
     → playbooks (+ scope rows).
  4. If prune=True, delete managed_by-tagged rows not in the manifest.

Playbook content is Grove-owned (see migration 0014). No Sinas writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnnotationDefinition,
    DocumentClass,
    DocumentClassEntityType,
    DocumentClassProperty,
    DossierClass,
    DossierClassDocumentClass,
    DossierClassProperty,
    EntityType,
    Playbook,
    PlaybookScope,
    RelationshipDefinition,
    RelationshipState,
)
from app.schemas.config import slugify
from app.services.annotations import (
    AnnotationConfigError,
    normalize_reduce,
    parse_path,
)
from app.schemas.package import (
    GrovePackage,
    PackageDiff,
    PackageImportResult,
    PackagePlaybookEntry,
    PackageValidateResult,
)


# ─────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────
def parse_package(yaml_text: str) -> tuple[GrovePackage | None, list[str]]:
    """Parse YAML into a GrovePackage; return (package, errors)."""
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return None, [f"YAML parse error: {exc}"]
    if not isinstance(raw, dict):
        return None, ["YAML root must be a mapping"]
    try:
        return GrovePackage.model_validate(raw), []
    except ValidationError as exc:
        return None, [str(e) for e in exc.errors()]


def _tag(pkg: GrovePackage) -> str:
    return f"pkg:{pkg.package.name}"


# ─────────────────────────────────────────────────────────────
# Cross-reference validation
# ─────────────────────────────────────────────────────────────
def validate_crossrefs(pkg: GrovePackage) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    dc_names = {dc.name for dc in pkg.spec.document_classes}
    et_names = {et.name for et in pkg.spec.entity_types}
    doss_names = {d.name for d in pkg.spec.dossier_classes}

    for dc in pkg.spec.document_classes:
        for et_name in dc.entity_types:
            if et_name not in et_names:
                errors.append(
                    f"document class '{dc.name}' references entity type '{et_name}' which is not defined in the package"
                )

    for dossier in pkg.spec.dossier_classes:
        for link in dossier.document_classes:
            if link.document_class not in dc_names:
                errors.append(
                    f"dossier class '{dossier.name}' links to document class '{link.document_class}' which is not defined in the package"
                )

    for rdef in pkg.spec.relationship_definitions:
        for side, ref in (("source", rdef.source), ("target", rdef.target)):
            valid = {
                "document_class": dc_names,
                "entity_type": et_names,
                "dossier_class": doss_names,
            }[ref.type]
            if ref.name not in valid:
                errors.append(
                    f"relationship '{rdef.name}' {side} references {ref.type} '{ref.name}' which is not defined in the package"
                )

    rdef_names = {r.name for r in pkg.spec.relationship_definitions}
    seen_annotations: set[str] = set()
    for ann in pkg.spec.annotations:
        if ann.name in seen_annotations:
            errors.append(f"annotation '{ann.name}' is defined more than once")
        seen_annotations.add(ann.name)
        try:
            path = parse_path(ann.path)
            normalize_reduce(ann.reduce)
        except AnnotationConfigError as exc:
            errors.append(f"annotation '{ann.name}': {exc}")
            continue
        for missing in sorted(path.names - rdef_names):
            warnings.append(
                f"annotation '{ann.name}' references relationship '{missing}' not defined "
                "in this package; it must already exist in the target Grove at import"
            )

    for pb in pkg.spec.playbooks:
        for s in pb.scope:
            if s.document_class and s.document_class not in dc_names:
                errors.append(
                    f"playbook '{pb.name}' scope references document class '{s.document_class}' which is not defined in the package"
                )
            if s.dossier_class and s.dossier_class not in doss_names:
                errors.append(
                    f"playbook '{pb.name}' scope references dossier class '{s.dossier_class}' which is not defined in the package"
                )

    return errors, warnings


def validate(yaml_text: str) -> PackageValidateResult:
    pkg, parse_errors = parse_package(yaml_text)
    if parse_errors:
        return PackageValidateResult(valid=False, errors=parse_errors)
    assert pkg is not None
    errors, warnings = validate_crossrefs(pkg)
    return PackageValidateResult(valid=not errors, errors=errors, warnings=warnings)


# ─────────────────────────────────────────────────────────────
# Apply
# ─────────────────────────────────────────────────────────────
@dataclass
class _ApplyCtx:
    pkg: GrovePackage
    tag: str
    session: AsyncSession
    prune: bool
    diff: PackageDiff = field(default_factory=PackageDiff)
    warnings: list[str] = field(default_factory=list)


async def apply(
    yaml_text: str,
    session: AsyncSession,
    prune: bool = True,
) -> PackageImportResult:
    pkg, parse_errors = parse_package(yaml_text)
    if parse_errors:
        raise ValueError("Package parse failed: " + "; ".join(parse_errors))
    assert pkg is not None
    errors, warnings = validate_crossrefs(pkg)
    if errors:
        raise ValueError("Package validation failed: " + "; ".join(errors))

    ctx = _ApplyCtx(
        pkg=pkg,
        tag=_tag(pkg),
        session=session,
        prune=prune,
        warnings=list(warnings),
    )

    # All writes happen in this transaction.
    await _apply_entity_types(ctx)
    await _apply_document_classes(ctx)
    await _apply_dossier_classes(ctx)
    await _apply_relationship_definitions(ctx)
    await _apply_playbooks(ctx)
    await _apply_annotations(ctx)

    if prune:
        await _prune(ctx)

    await session.commit()

    return PackageImportResult(
        package=pkg.package.name,
        version=pkg.package.version,
        diff=ctx.diff,
        warnings=ctx.warnings,
    )


# ────────── helpers: upsert by (managed_by, name) ──────────
async def _existing_managed(
    ctx: _ApplyCtx, model: Any, name_col: str = "name"
) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            select(model).where(model.managed_by == ctx.tag)
        )
    ).scalars().all()
    return {getattr(r, name_col): r for r in rows}


def _track(diff: PackageDiff, kind: str, name: str, created: bool, changed: bool) -> None:
    label = f"{kind}/{name}"
    if created:
        diff.created.append(label)
    elif changed:
        diff.updated.append(label)
    else:
        diff.unchanged.append(label)


def _assign_if_changed(row: Any, fields: dict[str, Any]) -> bool:
    changed = False
    for k, v in fields.items():
        if getattr(row, k) != v:
            setattr(row, k, v)
            changed = True
    return changed


# ────────── entity types ──────────
async def _apply_entity_types(ctx: _ApplyCtx) -> None:
    existing = await _existing_managed(ctx, EntityType)
    seen: set[str] = set()
    for et in ctx.pkg.spec.entity_types:
        seen.add(et.name)
        row = existing.get(et.name)
        if row is None:
            # An unmanaged row with the same name? Take it over.
            row = (
                await ctx.session.execute(
                    select(EntityType).where(EntityType.name == et.name)
                )
            ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = EntityType(name=et.name)
            ctx.session.add(row)
        changed = _assign_if_changed(
            row,
            {
                "description": et.description,
                "guidance": et.guidance,
                "creation_mode": et.creation_mode,
                "managed_by": ctx.tag,
            },
        )
        await ctx.session.flush()
        _track(ctx.diff, "entity_type", et.name, created, changed)


# ────────── document classes ──────────
async def _resolve_entity_types_by_name(
    ctx: _ApplyCtx, names: Iterable[str]
) -> dict[str, EntityType]:
    name_list = list(names)
    if not name_list:
        return {}
    rows = (
        await ctx.session.execute(
            select(EntityType).where(EntityType.name.in_(name_list))
        )
    ).scalars().all()
    return {r.name: r for r in rows}


async def _apply_document_classes(ctx: _ApplyCtx) -> None:
    existing = await _existing_managed(ctx, DocumentClass)
    for dc in ctx.pkg.spec.document_classes:
        row = existing.get(dc.name) or (
            await ctx.session.execute(
                select(DocumentClass).where(DocumentClass.name == dc.name)
            )
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = DocumentClass(name=dc.name, slug=dc.slug or slugify(dc.name))
            ctx.session.add(row)
            await ctx.session.flush()
        changed = _assign_if_changed(
            row,
            {
                "description": dc.description,
                "summarization_guidance": dc.summarization_guidance,
                "classification_hints": dc.classification_hints,
                "managed_by": ctx.tag,
            },
        )

        # Reconcile properties (delete-then-add by name).
        prop_rows = (
            await ctx.session.execute(
                select(DocumentClassProperty).where(
                    DocumentClassProperty.document_class_id == row.id
                )
            )
        ).scalars().all()
        by_name = {p.name: p for p in prop_rows}
        manifest_names = {p.name for p in dc.properties}
        for old in prop_rows:
            if old.name not in manifest_names:
                await ctx.session.delete(old)
                changed = True
        for prop in dc.properties:
            p_row = by_name.get(prop.name)
            if p_row is None:
                p_row = DocumentClassProperty(document_class_id=row.id, name=prop.name)
                ctx.session.add(p_row)
                changed = True
            p_changed = _assign_if_changed(
                p_row,
                {
                    "description": prop.description,
                    "schema": prop.schema,
                    "guidance": prop.guidance,
                    "manual": prop.manual,
                    "required": prop.required,
                    "cardinality": prop.cardinality,
                    "schema_version": prop.schema_version,
                },
            )
            changed = changed or p_changed

        # Reconcile entity_type attachments.
        et_map = await _resolve_entity_types_by_name(ctx, dc.entity_types)
        existing_links = (
            await ctx.session.execute(
                select(DocumentClassEntityType).where(
                    DocumentClassEntityType.document_class_id == row.id
                )
            )
        ).scalars().all()
        wanted_ids = {et.id for et in et_map.values()}
        for link in existing_links:
            if link.entity_type_id not in wanted_ids:
                await ctx.session.delete(link)
                changed = True
        present_ids = {link.entity_type_id for link in existing_links}
        for et in et_map.values():
            if et.id not in present_ids:
                ctx.session.add(
                    DocumentClassEntityType(
                        document_class_id=row.id, entity_type_id=et.id
                    )
                )
                changed = True

        await ctx.session.flush()
        _track(ctx.diff, "document_class", dc.name, created, changed)


# ────────── dossier classes ──────────
async def _apply_dossier_classes(ctx: _ApplyCtx) -> None:
    existing = await _existing_managed(ctx, DossierClass)
    # Build name→DocumentClass map for link resolution.
    all_dc = (
        await ctx.session.execute(select(DocumentClass))
    ).scalars().all()
    dc_by_name = {dc.name: dc for dc in all_dc}

    for dossier in ctx.pkg.spec.dossier_classes:
        row = existing.get(dossier.name) or (
            await ctx.session.execute(
                select(DossierClass).where(DossierClass.name == dossier.name)
            )
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = DossierClass(
                name=dossier.name, slug=dossier.slug or slugify(dossier.name)
            )
            ctx.session.add(row)
            await ctx.session.flush()
        changed = _assign_if_changed(
            row,
            {
                "description": dossier.description,
                "guidance": dossier.guidance,
                "summarization_guidance": dossier.summarization_guidance,
                "classification_hints": dossier.classification_hints,
                "managed_by": ctx.tag,
            },
        )

        # Properties.
        prop_rows = (
            await ctx.session.execute(
                select(DossierClassProperty).where(
                    DossierClassProperty.dossier_class_id == row.id
                )
            )
        ).scalars().all()
        by_name = {p.name: p for p in prop_rows}
        manifest_names = {p.name for p in dossier.properties}
        for old in prop_rows:
            if old.name not in manifest_names:
                await ctx.session.delete(old)
                changed = True
        for prop in dossier.properties:
            p_row = by_name.get(prop.name)
            if p_row is None:
                p_row = DossierClassProperty(dossier_class_id=row.id, name=prop.name)
                ctx.session.add(p_row)
                changed = True
            p_changed = _assign_if_changed(
                p_row,
                {
                    "description": prop.description,
                    "schema": prop.schema,
                    "guidance": prop.guidance,
                    "manual": prop.manual,
                    "required": prop.required,
                    "cardinality": prop.cardinality,
                    "schema_version": prop.schema_version,
                },
            )
            changed = changed or p_changed

        # Document-class links.
        existing_links = (
            await ctx.session.execute(
                select(DossierClassDocumentClass).where(
                    DossierClassDocumentClass.dossier_class_id == row.id
                )
            )
        ).scalars().all()
        wanted = {
            dc_by_name[link.document_class].id: link
            for link in dossier.document_classes
            if link.document_class in dc_by_name
        }
        by_dc_id = {link.document_class_id: link for link in existing_links}
        for dc_id, existing_link in by_dc_id.items():
            if dc_id not in wanted:
                await ctx.session.delete(existing_link)
                changed = True
        for dc_id, manifest_link in wanted.items():
            link_row = by_dc_id.get(dc_id)
            if link_row is None:
                ctx.session.add(
                    DossierClassDocumentClass(
                        dossier_class_id=row.id,
                        document_class_id=dc_id,
                        required=manifest_link.required,
                        cardinality=manifest_link.cardinality,
                    )
                )
                changed = True
            else:
                l_changed = _assign_if_changed(
                    link_row,
                    {
                        "required": manifest_link.required,
                        "cardinality": manifest_link.cardinality,
                    },
                )
                changed = changed or l_changed

        await ctx.session.flush()
        _track(ctx.diff, "dossier_class", dossier.name, created, changed)


# ────────── relationship definitions ──────────
async def _apply_relationship_definitions(ctx: _ApplyCtx) -> None:
    existing = await _existing_managed(ctx, RelationshipDefinition)

    dc_by_name = {
        dc.name: dc
        for dc in (await ctx.session.execute(select(DocumentClass))).scalars().all()
    }
    et_by_name = {
        et.name: et
        for et in (await ctx.session.execute(select(EntityType))).scalars().all()
    }
    doss_by_name = {
        d.name: d
        for d in (await ctx.session.execute(select(DossierClass))).scalars().all()
    }

    def _resolve(ref_type: str, name: str):
        if ref_type == "document_class":
            return dc_by_name[name]
        if ref_type == "entity_type":
            return et_by_name[name]
        return doss_by_name[name]

    for rdef in ctx.pkg.spec.relationship_definitions:
        row = existing.get(rdef.name) or (
            await ctx.session.execute(
                select(RelationshipDefinition).where(
                    RelationshipDefinition.name == rdef.name
                )
            )
        ).scalar_one_or_none()
        created = row is None
        source = _resolve(rdef.source.type, rdef.source.name)
        target = _resolve(rdef.target.type, rdef.target.name)
        if row is None:
            row = RelationshipDefinition(
                name=rdef.name,
                source_ref_type=rdef.source.type,
                source_ref_id=source.id,
                target_ref_type=rdef.target.type,
                target_ref_id=target.id,
            )
            ctx.session.add(row)
            await ctx.session.flush()
        changed = _assign_if_changed(
            row,
            {
                "description": rdef.description,
                "source_ref_type": rdef.source.type,
                "source_ref_id": source.id,
                "target_ref_type": rdef.target.type,
                "target_ref_id": target.id,
                "cardinality": rdef.cardinality,
                "extraction_guidance": rdef.extraction_guidance,
                "discovery_guidance": rdef.discovery_guidance,
                "creation_mode": rdef.creation_mode,
                "managed_by": ctx.tag,
            },
        )

        state_rows = (
            await ctx.session.execute(
                select(RelationshipState).where(
                    RelationshipState.relationship_definition_id == row.id
                )
            )
        ).scalars().all()
        by_name = {s.name: s for s in state_rows}
        manifest_names = {s.name for s in rdef.states}
        for old in state_rows:
            if old.name not in manifest_names:
                await ctx.session.delete(old)
                changed = True
        for state in rdef.states:
            s_row = by_name.get(state.name)
            if s_row is None:
                s_row = RelationshipState(
                    relationship_definition_id=row.id, name=state.name
                )
                ctx.session.add(s_row)
                changed = True
            s_changed = _assign_if_changed(
                s_row,
                {
                    "description": state.description,
                    "counts_as_active": state.counts_as_active,
                },
            )
            changed = changed or s_changed

        await ctx.session.flush()
        _track(ctx.diff, "relationship_definition", rdef.name, created, changed)


# ────────── playbooks ──────────
async def _apply_playbooks(ctx: _ApplyCtx) -> None:
    """Upsert Playbook content + reconcile PlaybookScope rows.

    Per playbook: find-or-create the `playbook` row, update description/content,
    then wipe-and-rewrite its scope rows from the manifest. A playbook with an
    empty `scope` list still gets a sentinel row (both class refs NULL) tagged
    `managed_by` so prune can identify it as belonging to this package.
    """
    dc_by_name = {
        dc.name: dc
        for dc in (await ctx.session.execute(select(DocumentClass))).scalars().all()
    }
    doss_by_name = {
        d.name: d
        for d in (await ctx.session.execute(select(DossierClass))).scalars().all()
    }
    existing_by_key = {
        (p.kind, p.name): p
        for p in (
            await ctx.session.execute(
                select(Playbook).where(Playbook.managed_by == ctx.tag)
            )
        ).scalars().all()
    }

    for pb in ctx.pkg.spec.playbooks:
        row = existing_by_key.get((pb.kind, pb.name)) or (
            await ctx.session.execute(
                select(Playbook).where(Playbook.kind == pb.kind, Playbook.name == pb.name)
            )
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = Playbook(kind=pb.kind, name=pb.name)
            ctx.session.add(row)
            await ctx.session.flush()
        changed = _assign_if_changed(
            row,
            {
                "description": pb.description,
                "content": pb.content,
                "managed_by": ctx.tag,
            },
        )

        # Wipe-then-rewrite the scope rows for this playbook.
        old_scopes = (
            await ctx.session.execute(
                select(PlaybookScope).where(PlaybookScope.playbook_id == row.id)
            )
        ).scalars().all()
        for s in old_scopes:
            await ctx.session.delete(s)
        await ctx.session.flush()

        scope_entries = pb.scope or [None]  # sentinel for "applies everywhere"
        for s in scope_entries:
            dc_id = None
            doss_id = None
            if s is not None:
                if s.document_class:
                    dc_id = dc_by_name[s.document_class].id
                if s.dossier_class:
                    doss_id = doss_by_name[s.dossier_class].id
            ctx.session.add(
                PlaybookScope(
                    playbook_id=row.id,
                    document_class_id=dc_id,
                    dossier_class_id=doss_id,
                    managed_by=ctx.tag,
                )
            )
        await ctx.session.flush()
        _track(ctx.diff, f"playbook/{pb.kind}", pb.name, created, changed)


# ────────── annotations ──────────
async def _apply_annotations(ctx: _ApplyCtx) -> None:
    """Upsert annotation definitions. Loud validation against the REAL
    relationship definitions now present (package ones were just applied):
    unknown names, bad syntax, or unknown reducers abort the import."""
    all_rdefs = {
        r.name: r
        for r in (
            await ctx.session.execute(select(RelationshipDefinition))
        ).scalars().all()
    }

    def _subject_ref_type(ann_name: str, path) -> str:
        """The subject side of the path's first hop; every alternation member
        must agree, otherwise the annotation is ambiguous."""
        sides = set()
        for step in path.terms[0].steps:
            rdef = all_rdefs[step.name]
            sides.add(rdef.target_ref_type if step.inverse else rdef.source_ref_type)
        if len(sides) != 1:
            raise ValueError(
                f"annotation '{ann_name}': first-hop alternation members disagree on the "
                f"subject type ({', '.join(sorted(sides))})"
            )
        return sides.pop()

    existing = await _existing_managed(ctx, AnnotationDefinition)
    for ann in ctx.pkg.spec.annotations:
        try:
            path = parse_path(ann.path)
            normalize_reduce(ann.reduce)
        except AnnotationConfigError as exc:
            raise ValueError(f"annotation '{ann.name}': {exc}") from exc
        unknown = sorted(path.names - set(all_rdefs))
        if unknown:
            raise ValueError(
                f"annotation '{ann.name}' references unknown relationship definition(s): "
                + ", ".join(unknown)
            )
        subject_ref_type = _subject_ref_type(ann.name, path)

        row = existing.get(ann.name) or (
            await ctx.session.execute(
                select(AnnotationDefinition).where(AnnotationDefinition.name == ann.name)
            )
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = AnnotationDefinition(name=ann.name)
            ctx.session.add(row)
        changed = _assign_if_changed(
            row,
            {
                "description": ann.description,
                "path": ann.path,
                "reduce": ann.reduce,
                "materialize": ann.materialize,
                "subject_ref_type": subject_ref_type,
                "managed_by": ctx.tag,
            },
        )
        await ctx.session.flush()
        _track(ctx.diff, "annotation", ann.name, created, changed)


# ────────── prune ──────────
async def _prune(ctx: _ApplyCtx) -> None:
    manifest_names = {
        "entity_type": {et.name for et in ctx.pkg.spec.entity_types},
        "document_class": {dc.name for dc in ctx.pkg.spec.document_classes},
        "dossier_class": {d.name for d in ctx.pkg.spec.dossier_classes},
        "relationship_definition": {r.name for r in ctx.pkg.spec.relationship_definitions},
        "annotation": {a.name for a in ctx.pkg.spec.annotations},
    }
    model_for = {
        "entity_type": EntityType,
        "document_class": DocumentClass,
        "dossier_class": DossierClass,
        "relationship_definition": RelationshipDefinition,
        "annotation": AnnotationDefinition,
    }

    for kind, model in model_for.items():
        rows = (
            await ctx.session.execute(
                select(model).where(model.managed_by == ctx.tag)
            )
        ).scalars().all()
        wanted = manifest_names[kind]
        for r in rows:
            if r.name not in wanted:
                await ctx.session.delete(r)
                ctx.diff.deleted.append(f"{kind}/{r.name}")

    # Playbooks: any (kind, name) tagged for this package and NOT in the
    # manifest is deleted (cascades to its scope rows via FK).
    manifest_pb = {(pb.kind, pb.name) for pb in ctx.pkg.spec.playbooks}
    pb_rows = (
        await ctx.session.execute(
            select(Playbook).where(Playbook.managed_by == ctx.tag)
        )
    ).scalars().all()
    for pb in pb_rows:
        if (pb.kind, pb.name) not in manifest_pb:
            await ctx.session.delete(pb)
            ctx.diff.deleted.append(f"playbook/{pb.kind}/{pb.name}")

    await ctx.session.flush()


# ─────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────
async def export_package(
    name: str,
    session: AsyncSession,
    version: str = "0.0.0",
) -> str:
    """Export all rows tagged `pkg:<name>` (plus their nested resources) as YAML."""
    tag = f"pkg:{name}"

    et_rows = (
        await session.execute(
            select(EntityType).where(EntityType.managed_by == tag)
        )
    ).scalars().all()
    dc_rows = (
        await session.execute(
            select(DocumentClass).where(DocumentClass.managed_by == tag)
        )
    ).scalars().all()
    doss_rows = (
        await session.execute(
            select(DossierClass).where(DossierClass.managed_by == tag)
        )
    ).scalars().all()
    rdef_rows = (
        await session.execute(
            select(RelationshipDefinition).where(RelationshipDefinition.managed_by == tag)
        )
    ).scalars().all()

    # Lookup tables for ref resolution on export.
    et_by_id = {et.id: et for et in (await session.execute(select(EntityType))).scalars().all()}
    dc_by_id = {dc.id: dc for dc in (await session.execute(select(DocumentClass))).scalars().all()}
    doss_by_id = {d.id: d for d in (await session.execute(select(DossierClass))).scalars().all()}

    def _ref_name(ref_type: str, ref_id) -> str:
        if ref_type == "document_class":
            return dc_by_id[ref_id].name
        if ref_type == "entity_type":
            return et_by_id[ref_id].name
        return doss_by_id[ref_id].name

    spec: dict[str, Any] = {}

    if et_rows:
        spec["entity_types"] = [
            {
                "name": et.name,
                "description": et.description,
                "guidance": et.guidance,
                "creation_mode": et.creation_mode,
            }
            for et in et_rows
        ]

    if dc_rows:
        out_dc = []
        for dc in dc_rows:
            props = (
                await session.execute(
                    select(DocumentClassProperty).where(
                        DocumentClassProperty.document_class_id == dc.id
                    )
                )
            ).scalars().all()
            links = (
                await session.execute(
                    select(EntityType)
                    .join(
                        DocumentClassEntityType,
                        DocumentClassEntityType.entity_type_id == EntityType.id,
                    )
                    .where(DocumentClassEntityType.document_class_id == dc.id)
                )
            ).scalars().all()
            out_dc.append(
                {
                    "name": dc.name,
                    "slug": dc.slug,
                    "description": dc.description,
                    "summarization_guidance": dc.summarization_guidance,
                    "classification_hints": dc.classification_hints,
                    "properties": [_export_property(p) for p in props],
                    "entity_types": [et.name for et in links],
                }
            )
        spec["document_classes"] = out_dc

    if doss_rows:
        out_doss = []
        for d in doss_rows:
            props = (
                await session.execute(
                    select(DossierClassProperty).where(
                        DossierClassProperty.dossier_class_id == d.id
                    )
                )
            ).scalars().all()
            links = (
                await session.execute(
                    select(DossierClassDocumentClass).where(
                        DossierClassDocumentClass.dossier_class_id == d.id
                    )
                )
            ).scalars().all()
            out_doss.append(
                {
                    "name": d.name,
                    "slug": d.slug,
                    "description": d.description,
                    "guidance": d.guidance,
                    "summarization_guidance": d.summarization_guidance,
                    "classification_hints": d.classification_hints,
                    "properties": [_export_property(p) for p in props],
                    "document_classes": [
                        {
                            "document_class": dc_by_id[link.document_class_id].name,
                            "required": link.required,
                            "cardinality": link.cardinality,
                        }
                        for link in links
                    ],
                }
            )
        spec["dossier_classes"] = out_doss

    if rdef_rows:
        out_rdef = []
        for r in rdef_rows:
            states = (
                await session.execute(
                    select(RelationshipState).where(
                        RelationshipState.relationship_definition_id == r.id
                    )
                )
            ).scalars().all()
            out_rdef.append(
                {
                    "name": r.name,
                    "description": r.description,
                    "source": {
                        "type": r.source_ref_type,
                        "name": _ref_name(r.source_ref_type, r.source_ref_id),
                    },
                    "target": {
                        "type": r.target_ref_type,
                        "name": _ref_name(r.target_ref_type, r.target_ref_id),
                    },
                    "cardinality": r.cardinality,
                    "extraction_guidance": r.extraction_guidance,
                    "discovery_guidance": r.discovery_guidance,
                    "creation_mode": r.creation_mode,
                    "states": [
                        {
                            "name": s.name,
                            "description": s.description,
                            "counts_as_active": s.counts_as_active,
                        }
                        for s in states
                    ],
                }
            )
        spec["relationship_definitions"] = out_rdef

    # Playbooks: managed Playbook rows directly. Scope is fetched per-playbook.
    pb_rows = (
        await session.execute(
            select(Playbook).where(Playbook.managed_by == tag).order_by(Playbook.kind, Playbook.name)
        )
    ).scalars().all()
    if pb_rows:
        out_pb = []
        for pb in pb_rows:
            scope_rows = (
                await session.execute(
                    select(PlaybookScope).where(PlaybookScope.playbook_id == pb.id)
                )
            ).scalars().all()
            scope_out: list[dict[str, str]] = []
            for s in scope_rows:
                if s.document_class_id is None and s.dossier_class_id is None:
                    continue  # sentinel = applies everywhere
                entry: dict[str, str] = {}
                if s.document_class_id:
                    entry["document_class"] = dc_by_id[s.document_class_id].name
                if s.dossier_class_id:
                    entry["dossier_class"] = doss_by_id[s.dossier_class_id].name
                scope_out.append(entry)
            out_pb.append(
                {
                    "kind": pb.kind,
                    "name": pb.name,
                    "description": pb.description,
                    "content": pb.content,
                    "scope": scope_out,
                }
            )
        spec["playbooks"] = out_pb

    ann_rows = (
        await session.execute(
            select(AnnotationDefinition)
            .where(AnnotationDefinition.managed_by == tag)
            .order_by(AnnotationDefinition.name)
        )
    ).scalars().all()
    if ann_rows:
        spec["annotations"] = [
            {
                "name": a.name,
                "description": a.description,
                "path": a.path,
                "reduce": a.reduce,
                "materialize": a.materialize,
            }
            for a in ann_rows
        ]

    doc = {
        "apiVersion": "grove.sinas.co/v1",
        "kind": "GrovePackage",
        "metadata": {"name": name},
        "package": {"name": name, "version": version},
        "spec": spec,
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _export_property(p: Any) -> dict[str, Any]:
    return {
        "name": p.name,
        "description": p.description,
        "schema": p.schema,
        "guidance": p.guidance,
        "manual": p.manual,
        "required": p.required,
        "cardinality": p.cardinality,
        "schema_version": p.schema_version,
    }
