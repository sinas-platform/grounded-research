"""Export runs as self-contained JSON documents — schema "sgr-review/1".

One document per run, holding everything a reader needs with no pointer
back into this system: the question, the outcome (a partial's note is the
deliverable, not an error), every claim with its rationale and its verbatim
evidence passages, and the ranked retrieval set with a `cited` flag per
document. Any consumer may anchor to these paths — a review platform
configures its display and its regression checks against them — so the
field paths are a CONTRACT:

    /schema                       "sgr-review/1"
    /question_id                  the run's reference (see below)
    /title                        the benchmark's name for the question, or null
    /run_id                       unique per export unit; idempotency key
    /question                     plain text
    /outcome/status               "published" | "partial" | ...
    /outcome/partial              {cause, note} or null
    /outcome/error                string or null — why a failed run failed
    /claims/N/{seq,text,type,rationale}
    /claims/N/evidence/M/{filename,line_from,line_to,passage}
    /retrieval/N/{rank,filename,class,cited}
    /quality_issues               list of strings
    /generated_at, /produced_by

There is deliberately no top-level answer field: the ordered claims
sequence IS the answer, stable by contract. Additions are fine; renaming
or moving any of these is a breaking change and bumps the schema string.

Cancelled runs are not exported by the selection helpers: a cancellation
is an operator abort with nothing reviewable, and an empty document is
indistinguishable from a broken export. Failed runs export only when
named explicitly by id, carrying /outcome/error so the reader sees a
failure rather than an absence.

`question_id` is the run's `reference` — the caller-supplied identifier
that reruns of the same logical question share. A run without one exports
with `question_id: null` rather than a derived stand-in: a consumer that
needs cross-round identity needs the reference actually set, and a silent
fallback would manufacture identities nobody chose.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.models import (
    AnswerClaim,
    ClaimEvidence,
    Document,
    DocumentClass,
    DocumentVersion,
    QueryRun,
)
from app.models.runtime import ResultDocument

SCHEMA = "sgr-review/1"


def _passage_text(content: str | None, span: dict | None) -> str:
    """The verbatim lines an evidence span points at, from the pinned
    document version — self-contained beats a pointer, and the span's own
    version id is what keeps this stable after re-ingestion."""
    if not content or not span or span.get("line_from") is None:
        return ""
    lf = int(span["line_from"])
    lt = int(span.get("line_to") or lf)
    lines = content.splitlines()
    return "\n".join(lines[max(0, lf - 1): lt])[:4000]


async def export_run(session, run: QueryRun) -> dict[str, Any]:
    """One run as one sgr-review/1 document."""
    claims_out: list[dict[str, Any]] = []
    cited_files: set[str] = set()
    if run.answer_id is not None:
        rows = (
            await session.execute(
                select(AnswerClaim, ClaimEvidence, Document.filename,
                       DocumentVersion.content_md)
                .outerjoin(ClaimEvidence, ClaimEvidence.claim_id == AnswerClaim.id)
                .outerjoin(Document, Document.id == ClaimEvidence.document_id)
                .outerjoin(DocumentVersion,
                           DocumentVersion.id == ClaimEvidence.document_version_id)
                .where(AnswerClaim.answer_id == run.answer_id)
                .order_by(AnswerClaim.sequence)
            )
        ).all()
        by_seq: dict[int, dict[str, Any]] = {}
        for claim, ev, fn, content in rows:
            entry = by_seq.setdefault(claim.sequence, {
                "seq": claim.sequence,
                "text": claim.claim_text or "",
                "type": claim.claim_type or "",
                "rationale": claim.rationale or "",
                "evidence": [],
            })
            if ev is not None and fn:
                cited_files.add(fn)
                entry["evidence"].append({
                    "filename": fn,
                    "line_from": (ev.span or {}).get("line_from"),
                    "line_to": (ev.span or {}).get("line_to"),
                    "passage": _passage_text(content, ev.span),
                })
        claims_out = [by_seq[k] for k in sorted(by_seq)]

    retrieval_out: list[dict[str, Any]] = []
    if run.parent_result_id is not None:
        rrows = (
            await session.execute(
                select(ResultDocument.rank, Document.filename, DocumentClass.name)
                .join(Document, Document.id == ResultDocument.document_id)
                .outerjoin(DocumentClass,
                           DocumentClass.id == Document.document_class_id)
                .where(ResultDocument.result_id == run.parent_result_id)
                .order_by(ResultDocument.rank)
            )
        ).all()
        retrieval_out = [
            {"rank": rank, "filename": fn, "class": cls or "",
             "cited": fn in cited_files}
            for rank, fn, cls in rrows
        ]

    tel = run.telemetry or {}
    partial = tel.get("partial")
    issues = ((tel.get("validate") or {}).get("gate_issues")
              or tel.get("quality_issues") or [])

    return {
        "schema": SCHEMA,
        "question_id": run.reference,
        "title": run.title,
        "run_id": str(run.id),
        "tags": list(run.tags or []),
        "question": run.question,
        "outcome": {
            "status": run.status,
            "partial": ({"cause": partial.get("cause"),
                         "note": partial.get("message") or partial.get("note")}
                        if isinstance(partial, dict) else None),
            "error": run.error or None,
        },
        "claims": claims_out,
        "retrieval": retrieval_out,
        "quality_issues": [str(i) for i in issues][:20],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "produced_by": "sgr",
    }


async def select_runs(
    session,
    *,
    run_ids: list[uuid.UUID] | None = None,
    tag: str | None = None,
    reference: str | None = None,
    latest_per_reference: bool = False,
    visible,
) -> list[QueryRun]:
    """The runs an export request names, visibility-filtered by the caller.

    `latest_per_reference` keeps, within the selection, only the newest
    COMPLETED run for each reference — "one version per question". Runs
    without a reference cannot be grouped and are kept as themselves.
    """
    stmt = select(QueryRun).where(visible)
    if run_ids:
        stmt = stmt.where(QueryRun.id.in_(run_ids))
    else:
        # Bulk selections carry only reviewable outcomes. A cancelled run is
        # an operator abort with nothing to judge; a failed one is a fault
        # report — both reachable by naming the run id explicitly, never
        # swept into a round's export by a tag.
        stmt = stmt.where(QueryRun.status.notin_(["cancelled", "failed"]))
    if tag:
        stmt = stmt.where(QueryRun.tags.contains([tag]))
    if reference:
        stmt = stmt.where(QueryRun.reference == reference)
    stmt = stmt.order_by(QueryRun.created_at.desc())
    runs = (await session.execute(stmt)).scalars().all()
    if not latest_per_reference:
        return list(runs)
    picked: dict[str, QueryRun] = {}
    loose: list[QueryRun] = []
    for r in runs:  # newest first
        if not r.reference:
            loose.append(r)
            continue
        if r.reference not in picked and r.completed_at is not None:
            picked[r.reference] = r
    return list(picked.values()) + loose
