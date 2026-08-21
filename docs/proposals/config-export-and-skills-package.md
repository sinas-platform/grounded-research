# Proposal — SGR config export + Sinas Skills Package install

Quick notes parked from a design conversation. Not an ADR yet. Pick up when batch refactor is done.

## Context

Domain config in SGR today (DocumentClass, properties, EntityType, RelationshipDefinition, DossierClass, playbooks/skills) is created via REST endpoints + admin UI. There's no declarative apply path.

Initially considered a Sinas-style YAML `sgr apply` CLI. **Rejected as the primary surface** because SGR operators deploy on a VPS and don't access the codebase. Editing a YAML file isn't their workflow — the admin UI is. YAML stays useful as an **interchange format**, not an editing surface.

## Sketch

### 1. Export from the UI

A "Export config" button in the SGR admin UI that produces a YAML manifest of the current domain config:
- Document classes + properties + entity-type bindings
- Entity types
- Relationship definitions + states
- Dossier classes + properties
- Skills (retrieval / synthesis playbooks)

Use cases:
- Reproduce a deployment elsewhere (staging → prod, dev → demo)
- Share a tested config with a sister deployment
- Snapshot before risky bulk changes (rollback path)
- Hand over to a customer / partner

### 2. Import via API endpoint (no CLI needed)

`POST /api/v1/config/apply` accepts the exported manifest and reconciles idempotently. Same shape as Sinas's `apply` flow. Dry-run support for preview.

The UI gets an "Import config" button that uploads a YAML file to this endpoint. No filesystem access required.

### 3. Skills Package — round-trip to Sinas

Playbooks (skills) live in Sinas, not SGR. Today they're in `package/sinas-grounded-research.yaml` and installed via `sinas package install`. With this proposal, the admin UI can also:

- **Export skills as a standalone Sinas package** (`sgr-skills-{deployment}.yaml`) containing just the `skills:` section of a Sinas package manifest. Bundle the deployment's authored playbooks.
- **Install button**: if the SGR operator has Sinas admin / package-install permissions, a button in SGR's UI POSTs the generated package to Sinas's `/api/v1/packages/install` endpoint. One click to push the playbooks live.

**Permissions matter here.** The user installing must have `sinas.packages.install` (or equivalent). The UI surfaces the requirement clearly — if the operator's Sinas token lacks the permission, the button is disabled with an explanation.

## Manifest shape (proposed)

```yaml
apiVersion: sgr.sinas.dev/v1
kind: SgrConfig
spec:
  entityTypes:
    - name: institution
      description: ...
      guidance: ...
  documentClasses:
    - slug: case_brief
      name: ...
      classificationHints: ...
      summarizationGuidance: ...
      properties:
        - name: legal_theme
          schema: { type: string, enum: [...] }
          cardinality: one
          guidance: ...
      entityTypes: [institution, company, ...]
  relationshipDefinitions:
    - name: cites
      sourceRefType: document_class
      sourceRefName: case_brief
      targetRefType: document_class
      targetRefName: case_brief
      extractionGuidance: ...
  dossierClasses:
    - slug: investigation
      name: ...
      properties: [...]
  skills:
    - namespace: sgr_retrieval_playbooks
      name: antitrust_default
      content: |
        ...
```

Skills Package export (separate file) shape:

```yaml
apiVersion: sinas.sinas.dev/v1
kind: Package
metadata:
  name: sgr-skills-{deployment_slug}
spec:
  skills:
    - namespace: sgr_retrieval_playbooks
      name: antitrust_default
      content: ...
```

## Implementation outline

1. **Apply service** (`backend/app/services/config_apply.py`):
   - Walks the manifest in dependency order:
     1. Entity types
     2. Document classes
     3. Document-class properties
     4. Document-class ↔ entity-type bindings
     5. Relationship definitions + states
     6. Dossier classes + properties
     7. Skills (playbooks)
   - Each kind has an idempotent upsert function: find by `slug`/`name`, create-or-update.
   - Returns `{created: N, updated: N, unchanged: N, errors: [...]}`.
   - Dry-run skips the commits, returns the same shape.

2. **Endpoints**:
   - `POST /api/v1/config/apply` (body: YAML or JSON manifest, `?dry_run=true|false`)
   - `GET /api/v1/config/export` (returns the current config as YAML)
   - `GET /api/v1/config/export/skills-package` (returns the Sinas Package YAML for the deployment's playbooks)
   - `POST /api/v1/config/install-skills-package` (POSTs the generated package to Sinas using the caller's token; requires the caller's Sinas permission)

3. **UI buttons**:
   - "Export config" → downloads YAML
   - "Import config" → uploads YAML, shows dry-run diff, asks to confirm before applying
   - "Export skills package" → downloads YAML
   - "Install skills to Sinas" → calls the install endpoint, surfaces success/error inline

## Open design questions

- **Drift detection**: should the apply service warn if the live config diverges from a previously-imported manifest? Maybe just show a diff at import time and let the user decide.
- **Deletes**: should apply *remove* resources not present in the manifest, or only add/update? Probably opt-in via `prune: true` flag. Default behavior: never delete.
- **Conflict resolution**: if two operators import overlapping configs back-to-back, last-write-wins. Probably fine for v1.
- **Skills round-trip integrity**: if a skill is edited in Sinas after install, re-installing from SGR overwrites. That's expected, but worth surfacing in the UI.
- **Manifest versioning**: `apiVersion` should be `sgr.sinas.dev/v1`. Future breaking changes get `v2`; the apply service supports multiple versions or errors clearly on unknown versions.

## Effort estimate

~Half a week of focused work for v1 (apply service + endpoints + basic UI buttons). The hardest part is dependency ordering at apply time and idempotency; Sinas's `services/config_apply/` is the reference implementation to copy.

## Why this isn't an ADR yet

An ADR needs a concrete decision: "we are doing X, not Y." This proposal is still at "we think this shape is right, but the details (drift, prune, conflict semantics) need to be agreed on first." Promote to ADR when we commit to building it.
