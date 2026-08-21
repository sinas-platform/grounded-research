# Architecture Decision Records

This directory records architecturally significant decisions about SGR. ADRs are append-only: when a decision is superseded, write a new ADR that supersedes the old one rather than editing the original.

## When to write an ADR

Write one when the decision is **load-bearing** for future contributors — they'd waste hours reconstructing it from git history otherwise. Typical triggers:

- A design choice between two non-obviously-better options where reasonable engineers would disagree
- A data-model commitment that affects downstream code (new tables, schema shape, field semantics)
- An API contract decision (URL shape, request/response schema, default behaviors)
- A behavior the runtime depends on that isn't obvious from the code (e.g., "stage X automatically does Y")
- Anything where you found yourself thinking "we should document why we picked this"

Do **not** write one for:

- Routine implementation choices (variable names, helper function placement)
- Bug fixes — those belong in commit messages and code comments
- Decisions whose rationale is already obvious from reading the code

## Format

Use the [template](template.md). Keep each ADR to ~1 printed page. The point is the decision and its alternatives, not a tutorial on the topic.

## Filename convention

`YYYY-MM-DD-short-slug.md` where the date is when the decision was *made* (not when written). Two ADRs the same day are fine as long as their slugs differ. Date-prefix avoids merge conflicts that sequential numbering causes on parallel branches, and the directory sorts chronologically out of the box. The filename is the canonical reference — cite an ADR as `2026-05-12-staged-documents` (or with a relative markdown link).

## Status lifecycle

- `proposed` — still under discussion
- `accepted` — in force
- `superseded by YYYY-MM-DD-slug` — replaced by a newer ADR
- `rejected` — proposed but not adopted (keep the file; the reasoning is still useful)
