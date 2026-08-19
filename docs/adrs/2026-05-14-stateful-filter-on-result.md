# Stateful filter on Result — mutation ops + server-side trace

- **Status**: proposed

## Context

Today the deep-search retrieval loop works like this:

1. Agent constructs a `GroveFilter` object in its conversation context.
2. Agent calls `introspect(filter)` — passes the full filter on every iteration.
3. Agent decides what to mutate, builds the next `GroveFilter`, repeats.
4. Agent is *expected* to call `append_trace(...)` to log what changed and why, but nothing in Grove enforces it.

Two problems:

**1. Token cost.** Every introspect call carries the full filter — every field clause, every regex clause, every explicit include/exclude — even when the agent's actual change is "add `jurisdiction in ['FR']`." Across 20+ iterations of a deep search, the wasted tokens compound.

**2. Trace is agent-discipline-dependent.** A misbehaving or poorly-prompted agent can mutate filters silently, add files to the result, and publish — without a single trace entry. The audit story claims "every decision is logged"; the reality is "every decision the agent *chooses* to log is logged."

Related: the filter never lives in storage. If a search crashes mid-loop and the agent is restarted, the filter state is lost (it was only in chat context). Resume isn't possible.

A pre-existing comment in `app/schemas/runtime.py:213` says the filter "lives in Sinas State." It doesn't — nothing in the package or backend reads/writes a Sinas State store for it.

## Decision

Make the **draft Result the durable home of the filter**. The agent mutates the filter through domain-specific ops; every mutation auto-appends a `result_trace` row before returning; `introspect` reads the persisted filter from the Result.

### Schema changes

```sql
ALTER TABLE result
  ADD COLUMN filter jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN filter_version integer NOT NULL DEFAULT 0;
```

- `filter` — the current `GroveFilter` value, kept in sync with each mutation
- `filter_version` — optimistic concurrency token, incremented on every mutation; mutation requests carry the expected version and 409 on mismatch

### Mutation ops (the validated list)

**Scopes (single-value clauses):**

| Op | Effect |
|---|---|
| `set_document_class_filter(result_id, document_class_id)` | Scope result to one DocumentClass |
| `clear_document_class_filter(result_id)` | Remove the class scope |
| `set_dossier_filter(result_id, dossier_id)` | Scope to one Dossier |
| `set_dossier_class_filter(result_id, dossier_class_id)` | Scope to docs in dossiers of one class |
| `clear_dossier_filters(result_id)` | Clear both dossier_id and dossier_class_id |

**Field filters (`field_filters: list[FieldFilter]`, one entry per field):**

| Op | Effect |
|---|---|
| `set_field_filter(result_id, field, op, values?, value?, join?)` | Upsert (replaces existing on same field) |
| `extend_field_filter_values(result_id, field, values)` | Append to existing values list (op must be `in` or `neq`); auto-creates the filter with `op=in` if absent |
| `shrink_field_filter_values(result_id, field, values)` | Remove specific values; deletes the filter entirely if values becomes empty |
| `remove_field_filter(result_id, field)` | Drop the field filter |

**Regex filters (`regex_filters: list[RegexFilter]`, one entry per field):**

| Op | Effect |
|---|---|
| `set_regex_filter(result_id, field, pattern)` | Upsert |
| `remove_regex_filter(result_id, field)` | Drop |

**Text search (single string):**

| Op | Effect |
|---|---|
| `set_text_search(result_id, query)` | Set or replace |
| `clear_text_search(result_id)` | Clear |

**Explicit lists (pinned/blocked document IDs):**

| Op | Effect |
|---|---|
| `add_explicit_includes(result_id, document_ids)` | Append to includes (dedup) |
| `remove_explicit_includes(result_id, document_ids)` | Remove specific IDs |
| `clear_explicit_includes(result_id)` | Drop all |
| `add_explicit_excludes(result_id, document_ids)` | Append to excludes (dedup) |
| `remove_explicit_excludes(result_id, document_ids)` | Remove specific IDs |
| `clear_explicit_excludes(result_id)` | Drop all |

**Whole-filter ops:**

| Op | Effect |
|---|---|
| `clear_filter(result_id)` | Reset entire filter to empty defaults |
| `replace_filter(result_id, filter)` | Total override (escape hatch; used by UI for one-shot setups) |

**Draft-result document ops (related; symmetry with filter mutations):**

| Op | Effect |
|---|---|
| `add_files_to_result(result_id, document_ids, reason)` | *Exists today.* No change. |
| `remove_files_from_result(result_id, document_ids, reason)` | *New.* Remove specific docs from the draft. |
| `clear_result_files(result_id)` | *New.* Drop all draft docs (rare). |

**Total: 22 new/changed ops** (19 filter mutations + 2 new draft-doc ops + introspect variant below).

### Introspect changes shape

| Op | Purpose |
|---|---|
| `introspect(result_id)` | *New canonical loop call.* Uses the persisted filter on the result. Returns distributions + total. |
| `introspect_preview(result_id, overlay_filter)` | *New.* Probe "what would the distributions look like if I added these clauses?" without committing. Read-only. |
| `introspect(filter)` | *Existing.* Kept for UI one-off queries and any non-agent caller. |

### Server-side auto-trace contract

Every mutation op, before returning, writes a `result_trace` row with:

- `sequence` — auto-incremented from `max(sequence)+1` for the result
- `agent` — read from caller identity / connector context (the calling agent's namespace+name)
- `action` — the op name (e.g., `"set_field_filter"`)
- `parameters` — the inputs as called (jsonb)
- `outcome` — `{"filter_before": {...}, "filter_after": {...}, "candidate_count_before": N, "candidate_count_after": M}` for filter ops; for doc ops, the doc IDs affected and the new doc count
- `occurred_at` — server-side timestamp

The agent can still call `append_trace(...)` for narrative entries (reasoning, intent). Auto-traces are *mechanical fact*; agent traces are *agent reasoning*.

### Mutation request envelope

Every mutation accepts:

```
{
  "filter_version": <expected current version>,
  ...op-specific params
}
```

Returns:

```
{
  "filter_version": <new version>,
  "filter": <new filter state>,
  "candidate_count": <total matching docs>,
  "trace_sequence": <the sequence number of the auto-trace row>
}
```

This gives the agent everything it needs in one round-trip: new state, new version, total count, and the trace anchor it can reference.

### What stays the same

- `GroveFilter` schema — unchanged. We persist exactly that shape on the Result.
- `result_trace` model — unchanged. Auto-trace just writes rows the agent could have written manually.
- `add_files_to_result`, `append_trace`, `publish_result`, `get_result`, `get_result_documents` — unchanged.
- The Result-as-research-session story — strengthened. Result now durably holds in-flight filter + trace + draft docs.

## Alternatives considered

**Generic patch (`patch_filter(result_id, jsonpatch)`).** RFC 6902 JSON Patch is the smallest possible API surface — one op covers everything. Rejected because the trace becomes unreadable (`[{op:add, path:/field_filters/3, value:{...}}]`) and playbooks can't reason about what's happening without parsing JSON Patch dialect. The trace is the audit deliverable; legibility wins over surface minimalism.

**Keep filter in chat context; add server-side auto-trace only.** Solves the audit story but not the token-cost or crash-resume stories. Half a fix.

**New `ResearchSession` table.** Considered (it was suggested in an early deployment brief). Rejected — `Result` already has the right shape (status, parent_result_id, owner, trace). Adding `filter` + `filter_version` columns is two columns; a new table would mean duplicating status/owner/audit/publish across two models. The Result *is* the session.

**Store the filter in Sinas State (state stores).** Rejected — the in-package comment in `schemas/runtime.py:213` claims this is current behavior, but no code reads/writes it. Sinas State adds a cross-service dependency for data that conceptually belongs to a Grove entity (the Result). Keeping it on the Result keeps the data co-located with the trace and the documents.

**No version field for concurrency.** Two agents mutating the same draft is rare (single-user, single-session typical), but cheap to add now and saves debugging pain later. Servers reject mutations with a stale version → 409. Agent retries with the new version.

## Consequences

**Easier:**

- Deep-search loop tokens drop significantly — agent emits `set_field_filter(field, op, values)` instead of the whole filter on every iteration. Compound savings across 20+ iteration loops.
- Audit trail becomes an enforced invariant: server auto-traces every mutation. Cooperative-agent assumption removed.
- Mid-search crash recovery works: rehydrate filter from `result.filter`, resume the agent.
- UI can render `result.filter` directly without replaying the trace.
- The "final filter state at publish" question goes away — it's `result.filter` at the moment of `publish_result`.
- Branching ("fork this search to try a different angle") becomes natural: `POST /retrieval/results/{id}/fork` clones the row including filter, returns a new draft with `parent_result_id` set.

**Harder:**

- 22 new connector ops to specify, implement, document, and prompt-engineer the agent around. Each is small; together they're a chunk of work.
- The agent prompt for `deep-search-agent` needs rewriting — the loop changes from "build a filter, call introspect" to "call mutation ops, call introspect(result_id)."
- `GroveFilter` schema is now a wire format AND a database format. Any change to the schema needs a migration *and* a connector-op compatibility consideration. Mitigate by keeping `GroveFilter` minimal and using version-aware reads.
- Op count is large (22). Risk of agent confusion ("which op do I use?"). Mitigate via the prompt's loop description and the playbooks' guidance.

**To revisit if reversed:** unlikely to fully reverse, but the easiest partial rollback is to keep the schema columns and have agents pass `filter` explicitly to `introspect` again. The auto-trace contract is the part that's hardest to undo because it relocates a responsibility from the agent to the server.

## 2026-06-09 addendum — field_filters narrow the count

The original landing of this ADR shipped `apply_grove_filter` with field_filters intentionally excluded from the count subquery — the comment in `services/introspect.py` referred to an "ARCHITECTURE.md §13" that never made it into the repo, and the practical effect was that the agent's filter loop had no steering signal: adding `sector_targeted = Pharmaceutical` didn't change `candidate_count`, so the agent couldn't tell if its filter was doing anything.

Fixed: field_filters now narrow the count subquery via `EXISTS` against `PropertyValue`, keyed by property name within the active `document_class_id`. Field name resolution is scoped to a class (you can't field_filter without one), which matches how the agent already drives the loop.

Distributions take the standard **faceted** approach: every field's distribution narrows by all *other* field_filters but not by its own, so the agent can still see "what other sectors exist in my current narrowed set" without that filter hiding its own candidate values. `total_documents` per distribution reflects the facet base, not the global pre-filter total.

Operator support in v1: `eq`, `in`, `neq`, `gte`, `lte`, `gt`, `lt`. Comparisons are text-cast on `value->'_'`, which works for ISO dates and exact strings. A typed-comparison variant (numeric / date cast) can be added when a real query needs it. `regex_filters` and dossier scoping remain TODO in `apply_grove_filter`.

## Out of scope for this ADR (follow-ups)

- **Undo via trace.** `revert_to_trace(result_id, sequence)` could reset the filter to its state at a specific trace point. Useful for "agent went down the wrong path, back up three steps." Skip in v1; agent can manually mutate back.
- **Filter validation rules.** Some combinations are nonsense (e.g., `op=eq` with `values=[a, b, c]`). Server-side validation should reject them. Spec the validation table when implementing.
- **Permissions per op.** Today the connector ops are all gated by the same permission. May want finer granularity (e.g., "agent can mutate filter on its own draft, not on others'") — handled at the API level via the existing ownership checks.
- **Removing the misleading `schemas/runtime.py:213` comment** about "filter lives in Sinas State" — cleanup when this lands.
