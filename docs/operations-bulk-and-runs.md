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
  parallel freely. Resolve stages may now also run concurrently: entity
  identity is enforced by a unique index on (entity_type_id,
  normalized_form), so a losing race links to the winner instead of
  creating a twin. Before that index existed this was a hard rule, and
  breaking it produced 267k duplicate entities.
- Checkpoints in `<job-dir>/batches.json`; jobs resume. Caveat: resuming
  an extract stage with a SHRUNKEN worklist misaligns cached chat ids —
  use a fresh job dir + fresh ids-file of the remaining docs instead.
- Known repair pattern: if registered documents have empty
  `document_version.content_md`, restore content from source files and
  re-run extract; every stage treats empty content as failure.

## Completeness gate (run before ANY question batch)

`GET /api/v1/maintenance/completeness` returns this per document class,
plus a total row: registered / with_content / with_entities /
with_relationships. It scans the corpus, so expect it to take a minute or
two on a large one. The equivalent SQL, per source bucket:

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

### Settings (backend `.env`, read at process start)
- `GROVE_DRAFT_MODE` — `extract` is the only value. Drafting is a plan
  (strong model) → verbatim passage extraction (cheap model, quotes
  string-verified against document lines) → one drafting call, all
  stateless. **Grounding is on raw source text only**: the document
  manifest (summaries, classes, annotations) decides what to READ and
  never reaches a drafting prompt, because it is interpretation produced
  at ingestion and verified against nothing.
- `GROVE_RUN_COST_CAP_USD` is currently inert. It measured one Sinas chat
  and drafting no longer opens one; llm_usage carries no run id, so spend
  cannot be attributed to a run without over-counting concurrent ones. A
  run is bounded by its validation rounds and gate cycles instead.
- `GROVE_BENCH_DIR` — regression benchmark folder for retrieval_first.
- Effort buys persistence as well as breadth: retrieval depth (low 1, medium
  2, high 3) and the number of times a run may act on the answer gate's
  verdict before settling for a partial (low 2, medium 3, high 5).
- `GROVE_DOMAIN`, `GROVE_AUDIENCE` — the only place a deployment states what
  kind of corpus and reader it serves (prompt wording only; nothing branches
  on them). Unset = generic framing; a legal deployment sets e.g. `legal` and
  `a legal researcher`. Grove itself must stay domain-neutral — never
  hardcode a domain into a prompt. Query languages are deliberately NOT
  configured: the planner reads them off the corpus schema, whose entity and
  property examples are in the corpus's own languages.

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

Or over HTTP: `POST /api/v1/maintenance/dedup/report` (read-only) and
`POST /api/v1/maintenance/dedup/apply {mode: "exact"|"llm", tighten,
types}`, which returns a `job_id` to poll at
`/api/v1/maintenance/dedup/jobs/{job_id}`.

`--tighten`: jaccard ≥ 0.8 or full containment, ≤3 partners per entity.
Every apply repoints relationship edges from merged-away entities to their
terminal survivors. Re-materialize annotations afterwards, and re-apply
authority tiers if alias merges changed which entity is canonical.

## Operational cautions
- Cancelling a run requires BOTH marking the row and deleting/stopping its
  Sinas chats — orphaned keep-alive jobs keep spending otherwise.
- Streamed-response drops can corrupt a chat (dangling tool_result → every
  later call 400s). Repair: list the chat's messages, delete ONLY verified
  orphans — print and eyeball the list before deleting; a large orphan
  count means the detector is wrong, not the chat.
