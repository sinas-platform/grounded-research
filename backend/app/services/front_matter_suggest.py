"""Front-matter suggest pipeline.

Synchronously scans documents' YAML front-matter and writes a fully populated
DiscoveryRun (status='completed') with DiscoveryCandidate + ConfigProposal
rows, so the result lands in the same review UI as agent-driven discovery.

Front-matter is canonical: dedupe by exact key equality, no consolidator-
agent needed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ConfigProposal,
    DiscoveryCandidate,
    DiscoveryRun,
    Document,
    DocumentVersion,
)
from app.schemas.ingestion import RunFilter
from app.services.discovery_runner import _select_documents
from app.services.front_matter import (
    aggregate_candidates,
    infer_candidates,
    split_front_matter,
    upgrade_cardinality,
)

# Read content_md in chunks so 35k-doc corpora don't materialize in one query.
_BATCH = 200


async def run_front_matter_suggest(
    session: AsyncSession,
    *,
    f: RunFilter,
    sample_size: int | None,
    parent_class_id: uuid.UUID | None,
    started_by: uuid.UUID,
) -> tuple[DiscoveryRun, int, int, int]:
    """Run the front-matter scan synchronously. Returns
    (run, candidate_count, proposal_count, docs_with_front_matter)."""
    doc_ids = await _select_documents(session, f, parent_class_id, sample_size)

    now = datetime.now(UTC)
    # Use status="completed" from the start: the whole scan happens inside
    # this single transaction, so there's no in-flight state worth modelling.
    # Importantly this keeps the run out of `_resume_running_runs`'s reach
    # (which only picks up scanning/consolidating) — that path would invoke
    # the consolidator agent with kind="front_matter" and fail.
    run = DiscoveryRun(
        kind="front_matter",
        status="completed",
        mode="greenfield",
        filter=f.model_dump(mode="json"),
        parent_class_id=parent_class_id,
        sample_size=sample_size,
        total_docs=len(doc_ids),
        scanned_docs=0,
        failed_docs=0,
        candidate_count=0,
        proposal_count=0,
        started_by=started_by,
        created_at=now,
        started_at=now,
        completed_at=now,
    )
    session.add(run)
    await session.flush()

    per_doc_raw: list[tuple[uuid.UUID, list[dict[str, Any]]]] = []
    candidate_rows: list[DiscoveryCandidate] = []
    scanned = 0
    failed = 0
    docs_with_fm = 0

    for batch_start in range(0, len(doc_ids), _BATCH):
        batch = doc_ids[batch_start : batch_start + _BATCH]
        rows = (
            await session.execute(
                select(Document.id, DocumentVersion.content_md)
                .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
                .where(Document.id.in_(batch))
            )
        ).all()
        for doc_id, content_md in rows:
            scanned += 1
            if not content_md:
                continue
            fm, _body = split_front_matter(content_md)
            if fm is None:
                continue
            docs_with_fm += 1
            try:
                cands = infer_candidates(fm)
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            if not cands:
                continue
            per_doc_raw.append((doc_id, cands))
            for c in cands:
                candidate_rows.append(
                    DiscoveryCandidate(
                        run_id=run.id,
                        kind=c["kind"],
                        payload=c,
                        evidence_document_id=doc_id,
                        evidence_span={"region": "front_matter"},
                        confidence=1.0,
                        created_at=now,
                    )
                )

    if candidate_rows:
        session.add_all(candidate_rows)
        await session.flush()

    # Aggregate per-(kind, name) and write proposals.
    proposals = aggregate_candidates(per_doc_raw)
    upgrade_cardinality(proposals, per_doc_raw)

    # Index candidate rows by (kind, name, doc_id) so each proposal can list
    # the exact candidate ids it consolidates.
    cand_index: dict[tuple[str, str, uuid.UUID], list[uuid.UUID]] = {}
    for cand in candidate_rows:
        key = (cand.kind, cand.payload["name"], cand.evidence_document_id)
        cand_index.setdefault(key, []).append(cand.id)

    proposal_rows: list[ConfigProposal] = []
    for p in proposals:
        supporting: list[str] = []
        for doc_id in p["supporting_doc_ids"]:
            for cand_id in cand_index.get((p["kind"], p["name"], doc_id), []):
                supporting.append(str(cand_id))
        # Don't store the supporting_doc_ids inside payload; the link goes via
        # supporting_candidate_ids (and each candidate carries its evidence_document_id).
        payload = {k: v for k, v in p.items() if k != "supporting_doc_ids"}
        if p["kind"] == "document_class_property" and parent_class_id is not None:
            payload["document_class_id"] = str(parent_class_id)
        proposal_rows.append(
            ConfigProposal(
                kind=p["kind"],
                payload=payload,
                status="pending",
                supporting_candidate_ids=supporting,
                discovery_run_id=run.id,
                created_at=now,
            )
        )
    if proposal_rows:
        session.add_all(proposal_rows)
        await session.flush()

    run.scanned_docs = scanned
    run.failed_docs = failed
    run.candidate_count = len(candidate_rows)
    run.proposal_count = len(proposal_rows)
    await session.commit()
    await session.refresh(run)

    return run, len(candidate_rows), len(proposal_rows), docs_with_fm
