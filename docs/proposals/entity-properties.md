# Proposal — Properties on entity types

Parked from a design conversation. Build when a concrete schema requires it; not before.

## Context

SgrPackage currently supports `properties` on document classes and dossier classes but not on entity types. The first ask came from a legal schema wanting `court_level` on `Court` and `country` on `Jurisdiction` — but on inspection both were better modelled as relationships (`ranks_higher_than`/`appeals_to` for hierarchy; `Document ↔ Jurisdiction` edges for multi-valued country), so we declined the build at the time.

The feature is still genuinely useful for **scalar, intrinsic, typed, queryable** attributes that don't relate to another entity:

- Identifiers: ISIN/ticker on Company, VAT number on Organization, ECLI on CaseLaw.
- Typed scalars with range queries: date_of_birth on Person, founded_at on Company, population on City.
- Codified single-valued attributes not worth their own entity type: ISO country code on Country itself (the country *is* the entity; the code is its typed handle), language on Document, currency on PriceQuote.
- Booleans / flags: is_listed on Company, deceased on Person.

**Litmus test for the schema author:** *"Would I want to filter, sort, index, or constrain on this field across all entities of this type?"* Yes → property. Connects to another entity → relationship. Source-specific metadata only → `Entity.extra_metadata` (the existing JSONB column on `entity` already handles this).

## Why we're deferring

1. Properties without a filter/search story are half a feature. Storage that nothing reads intelligently is worse than the absence of a column.
2. The first request didn't pass the litmus test. Building speculatively risks normalizing misuse — once the column exists, agents and authors will fill it with things that should have been relationships.
3. The reingestion cost when a property is later added is unavoidable in either direction (it's about backfilling existing entities, not about when the table got created). Building earlier only shrinks the eventual backfill by the size of the corpus added in the meantime.

## Trigger to build

A concrete schema that:
- defines at least one attribute passing the litmus test (scalar, intrinsic, typed, queryable, not an entity-to-entity link), AND
- has a downstream query / synthesis use that needs to filter or sort entities by that attribute.

Both conditions must hold. The second is what justifies the read path.

## Sketch

Two coherent slices. Ship them together.

### Slice 1 — Capture

Mirrors `DocumentClassProperty` / `PropertyValue` exactly. Separate tables, not a polymorphic generic — same reasoning as the existing split: real FKs, no `WHERE owner_type = …` traps, consistency with the prior pattern.

1. **Migration** — `entity_type_property` (one-to-many off `entity_type`) and `entity_property_value` (one-to-many off `entity`). Same column set as the document-class versions.
2. **Models + Pydantic** — `EntityTypeProperty`, `EntityPropertyValue`; `EntityTypePropertyIn/Out`.
3. **Config API** — `/config/entity-types/{id}/properties` CRUD (mirrors document-class properties).
4. **SgrPackage schema** — `PackageEntityTypeEntry.properties: list[PackagePropertyEntry]` (reuse the existing entry type). Apply + export wire it through.
5. **Ingestion API** — `POST /ingest/entity-property-values` (mirrors `set_property_value`).
6. **Review-mode handling** — `EntityProposal.pending_property_values` JSONB. Agent extracts values eagerly even when the entity is pending review; on approval, the proposal's pending values are promoted to real `EntityPropertyValue` rows. Without this, the typed values are lost between extraction and approval.
7. **Closed-mode handling** — N/A; no new entities created, so no new property values to capture at extraction time. Manual or package-import only.

### Slice 2 — Read

1. **Filter API for entities** — parallel to the document filter: `GET /entities?type=…&filters=[…]` with the same operator vocabulary.
2. **Connector op + agent guidance** — expose the filter to the retrieval agent. A small skill explaining *when* to filter entities by property vs. follow relationships, so the new tool isn't over- or under-used.
3. **Admin UI** — properties tab on the entity-type editor (copy the document-class pattern). An entities browse page with a filter strip — or, minimally, surface property values on existing entity views (proposals, unresolved mentions, dossier detail).

### Sinas package updates

- `propose_new_entity` connector op already accepts `extra_metadata`; values themselves go via a new `set_entity_property_value` op (mirror of `set_property_value`).
- `entity-extractor-agent` prompt: after the entity is materialised (`kind="entity"`), call `get_entity_type_properties` and `set_entity_property_value` for each property the document supports — same pattern the property-extractor uses for documents. For `kind="proposal"`, bundle pending values into the proposal payload instead.
- Bump package version.

## Migration impact

Backward compatible. EntityTypes without properties continue to work — empty list, no rows, no behaviour change. Existing entities silently lack property values until a backfill ingestion pass runs (which is the unavoidable reingestion cost noted above).

## Open questions for whoever picks this up

- Should `extra_metadata` migrate into typed properties when a schema author adds one with the same name, or stay parallel? Probably stay parallel — `extra_metadata` is the escape hatch, properties are the structured contract.
- Filter operator vocabulary: copy the document filter's exactly, or take the chance to clean it up? Copy unless the existing one is known-broken — keeping one vocabulary is worth more than perfect operators.
- Do we need entity-property *discovery* (the `ConfigProposal` pipeline for documents)? Yes for completeness, no for v1 — the document discovery pipeline is a heavy build, and the litmus-test discipline says properties should be deliberate, not discovered.
