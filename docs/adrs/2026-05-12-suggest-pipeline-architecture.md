# Suggest pipeline combines deterministic front-matter scan with LLM discovery

- **Status**: accepted

## Context

Bootstrapping a SGR deployment against an unknown corpus requires designing a schema (document classes, properties, entity types, relationships) before the auto-pipeline can do anything useful. Running the LLM-driven discovery agent on a 30k-document corpus to "figure out the schema" is expensive and noisy, especially when many corpora already arrive with structured metadata (YAML front-matter, CMS exports) that declares most of the schema for free.

We needed a way to:

1. Extract high-confidence schema candidates from existing structured metadata, cheap and deterministic.
2. Discover schema candidates from unstructured content (the long tail) via the LLM.
3. Present both in a unified review queue so reviewers don't have to reason about two parallel proposal sets.

## Decision

Two complementary inputs, one shared output queue:

- **Front-matter scan** (`backend/app/services/front_matter*.py`) is pure-Python and synchronous. It parses leading YAML, classifies top-level keys (scalar → property, list-of-scalars → multi-value property, list-of-dicts → entity type, well-known `category`/`type`/`sgr.class` keys → document class), and aggregates across the selected docs.
- **LLM discovery** runs the existing `discovery-agent` + `discovery-consolidator-agent` chain as a background `DiscoveryRun`.
- Both write to the same `DiscoveryRun → DiscoveryCandidate → ConfigProposal` tables that the existing review UI already understands.

A single orchestrator endpoint `POST /discovery/suggest` fires both in parallel: front-matter executes synchronously inside the request and returns proposal counts immediately; LLM discovery is queued for the background worker and progresses through the standard run lifecycle. Two flags control composition: `include_front_matter` (default true) and `skip_llm` (default false).

Front-matter runs are recorded as `DiscoveryRun(kind="front_matter", status="completed")` from the moment they're created — they never transition through `scanning`/`consolidating`. The whole scan lives inside one transaction; the worker only resumes runs in those in-flight statuses, so a crashed front-matter request can't get picked up later and re-invoked as an LLM run.

## Alternatives considered

**LLM-only discovery.** Skip the front-matter scan; trust the LLM agent to find structured fields it sees in document headers. Rejected because (a) it pays LLM cost to re-derive information that's already in the YAML, (b) string keys are canonical so trivial dedupe works on them deterministically — wasted on the LLM, and (c) front-matter accuracy is materially higher than free-form extraction for fields the source system already cared enough to declare.

**Front-matter-only suggest.** Cheap and tempting for corpora with rich headers. Rejected as a general solution because most real corpora have *partial* structure: front-matter covers admin metadata (title, date, author) but not domain concepts (case parties, market definition, theory of harm). The LLM finds the long tail.

**Two separate endpoints.** `POST /discovery/front-matter-suggest` and `POST /discovery/runs` already exist independently. We kept the unified endpoint as the *recommended* path but didn't remove the separate ones — they're useful for the rare case where a user only wants one half (e.g., quick FM preview without spending LLM budget).

**Run the consolidator on FM candidates.** Have the FM scan write only `DiscoveryCandidate` rows, then invoke `discovery-consolidator-agent` to dedupe. Rejected: front-matter keys cluster by exact string equality, no LLM judgment needed. Running an agent over canonical strings is overhead with no quality gain.

## Consequences

**Easier:**
- Cold-start on a structured corpus: front-matter alone often produces an approve-and-ship schema with no LLM cost.
- Reviewers see one unified queue regardless of which path produced a proposal.
- Front-matter results are immediate; the user gets a useful artifact before the LLM run finishes.
- Per-document evidence is uniform (`DiscoveryCandidate.evidence_document_id` + `evidence_span`), so the review UI doesn't need to special-case kinds.

**Harder:**
- Duplicate proposals are possible when both passes propose the same thing (e.g., FM says `title` is a property, LLM also finds `title` in document bodies). The review UI lets users merge, but no automatic dedup between the two passes today.
- The front-matter parser is a contract: changing its inference rules retroactively affects what's been proposed. New top-level-key inference rules should be additive.
- The `kind="front_matter"` value on `DiscoveryRun` is a sentinel that callers must not confuse with the agent-driven kinds (`document_class`, `entity_type`, etc.) used as agent-dispatch keys.

**To revisit if reversed:** the worker's `_resume_running_runs` skips `completed` status, so the crash-safety story relies on the front-matter run committing atomically. If we ever introduce partial commits or break the transaction, the run could end up in a state that the LLM consolidator picks up incorrectly.
