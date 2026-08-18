# Operations: bulk ingestion & question runs

Everything an operator needs to run ingestion and question answering
without prior context. Domain content (schemas, playbooks, benchmarks)
lives in deployment-specific repos, never here.

## Bulk ingestion pipeline

Batch-ETL over provider batch APIs. Runs as a standalone process (imports
the app as a library, writes the DB directly); the API route
`POST /bulk/upload` spawns the same thing.

    cd backend && ../.venv/bin/python -m app.bulk_pipeline \
      --ids-file /tmp/ids.txt \
      --stages extract,resolve,relationships \
      --job-dir ~/grove-bulk-jobs/<job-name>

- `--ids-file`: one document id per line. Build it with SQL, e.g. all
  registered docs of a source that lack entities (see gate query below).
- `--stages`: `extract` (front matter, props, chunks), `resolve`
  (grounding + entity resolution), `relationships`. Run extract jobs in
  parallel freely; **run only ONE resolve stage at a time** across all
  jobs — concurrent resolution races entity creation and produces
  duplicates.
- Checkpoints in `<job-dir>/batches.json`; jobs resume. Caveat: resuming
  an extract stage with a SHRUNKEN worklist misaligns cached chat ids —
  use a fresh job dir + fresh ids-file of the remaining docs instead.
- Known repair pattern: if registered documents have empty
  `document_version.content_md`, restore content from source files and
  re-run extract; every stage treats empty content as failure.

## Completeness gate (run before ANY question batch)

Registered vs extracted vs related, per source bucket:

    SELECT <source-bucket-expr>, count(*),
      count(*) FILTER (WHERE EXISTS (SELECT 1 FROM entity_mention m
                                     WHERE m.document_id=d.id)),
      count(*) FILTER (WHERE EXISTS (SELECT 1 FROM relationship r
                                     WHERE r.evidence_document_id=d.id))
    FROM document d GROUP BY 1;

Unit statuses on ingestion runs are bookkeeping, not truth — derive
completeness from data (this query), never from run/unit status.

## Question runs

- Retrieval: `python -m app.retrieval_first --question "…" --effort medium
  --store` prints `stored result <id>`. Optional regression harness needs
  `GROVE_BENCH_DIR` pointing at a benchmark folder.
- Synthesis: `POST /api/v1/query-runs {question, mode: "synthesis",
  effort, parent_result_id}`. Resume a failed run:
  `POST /api/v1/query-runs/{id}/resume`.
- Terminal states: `published`, `partial` (semantic dead-end — cause +
  client-facing note in `telemetry.partial`; verified claims retained),
  `failed` (infrastructure, retryable), `cancelled`.

### Env flags (read at process start)
- `GROVE_DRAFT_MODE=extract` — split drafting: plan (strong model) →
  verbatim passage extraction (cheap model, quotes string-verified
  against document lines) → one-shot draft (strong model), all stateless;
  thin drafts become honest partials, never silent fallbacks. Unset =
  chat drafting.
- `GROVE_RUN_COST_CAP_USD` (default 10) — per-run spend ceiling summed
  over the synthesis chat; tripping it produces a `partial`.
- `GROVE_BENCH_DIR` — regression benchmark folder for retrieval_first.

### Required agent registry (Sinas)
- `grove/retrieval-planner-agent` — tool-less, temp 0, strong model:
  retrieval planning, argument planning, extract-mode drafting.
- `grove/passage-extractor-agent` — tool-less, temp 0, cheap model:
  verbatim passage extraction.
- The synthesis/validator/gate agents as installed by the package.
Model/provider assignments are deployment config; agents read them per
call, so provider swaps take effect without restarts.

## Entity dedup

    python -m app.entity_dedup --report            # counts + samples only
    python -m app.entity_dedup --apply-exact       # identical-form merges
    python -m app.entity_dedup --apply-llm --tighten [--types "A,B"]

`--tighten`: jaccard ≥ 0.8 or full containment, ≤3 partners per entity.
After ANY apply, repoint relationship edges from merged-away entities to
their survivors (the apply currently repoints mentions only — see fixes
ledger) and re-materialize annotations.

## Operational cautions
- Cancelling a run requires BOTH marking the row and deleting/stopping its
  Sinas chats — orphaned keep-alive jobs keep spending otherwise.
- Streamed-response drops can corrupt a chat (dangling tool_result → every
  later call 400s). Repair: list the chat's messages, delete ONLY verified
  orphans — print and eyeball the list before deleting; a large orphan
  count means the detector is wrong, not the chat.
