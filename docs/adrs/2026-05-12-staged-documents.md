# Staged documents opt out of the auto-pipeline at upload time

- **Status**: accepted

## Context

By default, a file dropped in the `sgr/documents` Sinas collection triggers `sgr/post_upload`, which registers the document and immediately invokes `ingestion-coordinator`. The coordinator fans out to classifier, summarizer, extractors, and dossier-assigner — six LLM calls per document.

This is wrong for two real workflows:

1. **Greenfield schema design.** Upload N docs to design the schema against, run the suggest pipeline, approve classes, *then* extract. Auto-firing the pipeline burns LLM budget classifying against zero classes and extracting properties that don't exist yet.
2. **Adding documents from a class not yet configured.** If 300 new docs arrive of a kind SGR hasn't been told about, the classifier-agent can't classify them, but the extractors still run and produce noise.

The discovery and front-matter scans (see [2026-05-12 suggest pipeline architecture](2026-05-12-suggest-pipeline-architecture.md)) genuinely *need* unprocessed documents — they scan the body to design the schema.

## Decision

Add a `staged: bool` flag (`Document.staged`, default `false`) that means "skipped the auto-pipeline." The flag flows from upload to the doc record via three hops:

1. UI / API caller passes `staged=true` to `POST /api/v1/uploads` (form field).
2. `uploads.py` merges `staged: true` into the metadata sent to Sinas.
3. The `sgr/post_upload` Sinas function reads `metadata.staged`. If true, it registers the document with `staged=true` and **does not invoke `ingestion-coordinator`**. Otherwise behavior is unchanged.

**Visibility carve-out:** retrieval (`api/v1/retrieval.py:_apply_filter`) excludes staged docs unconditionally — they're not indexed yet, so they shouldn't appear in search results. The admin Documents listing shows them with a "Staged" badge and a toggle for staged-only view.

**Promotion:** a normal `IngestionRun` with `filter.staged_only=true` runs the full pipeline against staged docs. As soon as **any** stage succeeds for a doc, the runner flips its `staged` flag to false. Partial failures still leave the doc unstaged but with whatever did succeed.

**Default behaviors per code path:**

- `ingestion_runner._select_documents`: **excludes** staged unless `include_staged=true` or `staged_only=true`. Preserves the existing "rerun classifier on unclassified" semantics — staged docs don't get pulled into routine reprocess runs.
- `discovery_runner._select_documents`: **includes** staged unconditionally — that's the whole point of staging. The `include_staged` knob is ignored here; users scope to processed docs via `document_class_ids` (which excludes staged implicitly, since they have no class) or use `staged_only` for the opposite scope.
- `documents.list_documents`: excludes staged by default; `include_staged` or `staged_only` query params opt in.
- `documents.get_document_counts`: reports `staged` as its own bucket alongside `unclassified` and `by_class`.

## Alternatives considered

**Separate Sinas collection.** A `sgr-staging` collection without the `post_upload` function configured. Architecturally pure — Sinas collections are the natural boundary. Rejected because (a) it requires a new collection definition in the package, (b) promotion would need re-upload or a "move" operation with file rewiring, and (c) admins would have to know which collection to use for what.

**Always skip the auto-pipeline; trigger explicitly.** Cleanest mental model, no special state. Rejected because the happy path (config exists, user just wants their doc indexed) becomes an extra click every time. The default has to serve the common case.

**`min_confidence`-style filter without a flag.** Use a magic value like `staged_only` via overloading `document_class_ids: list | None | "staged"`. Rejected for the same reasons noted in [2026-05-12 RunFilter boolean knobs](2026-05-12-runfilter-boolean-knobs.md) — sentinels in unions are footguns; explicit booleans compose.

## Consequences

**Easier:**
- Cold-start: drop docs in staged, design schema with discovery/suggest pipeline, promote when ready. Single mental model.
- Adding docs of unknown shape: stage them, run discovery against the staged bucket, approve a new class, then promote.
- Reprocess workflows are unaffected by default — `staged` is opt-in.

**Harder:**
- One more state per document. UI surfaces (list, detail, counts) need to handle it.
- The unstage-on-success semantics is implicit. A user who runs `IngestionRun({stages: ["summarizer"], staged_only: true})` might be surprised when summarized-but-unclassified docs are now "live" (unstaged) and surfaced in search. Mitigation: the common promote path runs all stages.
- The defaults differ between ingestion and discovery runners. A reader expects `RunFilter` to mean the same thing everywhere; it doesn't quite. Documented at the field level.

**To revisit if reversed:** the visibility carve-out (`Document.staged.is_(False)` in retrieval) is the most invasive piece. If we ever decide staged docs should be retrievable, that one line is the entry point.
