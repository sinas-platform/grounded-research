"""Discovery — config auto-suggestion runs and proposal review."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller, require_permission
from app.db import get_session
from app.models import (
    ConfigProposal,
    DiscoveryCandidate,
    DiscoveryRun,
    DocumentClass,
    DocumentClassProperty,
    DossierClass,
    EntityType,
    RelationshipDefinition,
)
from app.schemas.config import (
    DocumentClassOut,
    DocumentClassPropertyOut,
    DossierClassOut,
    EntityTypeOut,
    RelationshipDefinitionOut,
    slugify,
)
from app.schemas.discovery import (
    ConfigProposalOut,
    ConsolidatedProposalIn,
    DiscoveryCandidateIn,
    DiscoveryRunCreateIn,
    DiscoveryRunCreateOut,
    DiscoveryRunOut,
    FrontMatterSuggestIn,
    FrontMatterSuggestOut,
    ProposalEditIn,
    ProposalMergeIn,
    SuggestIn,
    SuggestOut,
)
from app.services.discovery_runner import (
    expand_filter,
    materialize_run,
    progress as discovery_progress,
    submit_scan,
)
from app.services.front_matter_suggest import run_front_matter_suggest
from sinas import SinasClient
from app.config import get_settings

router = APIRouter(prefix="/discovery", tags=["discovery"])


# ─────────────────────── runs ───────────────────────
@router.post(
    "/runs",
    response_model=DiscoveryRunCreateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def create_discovery_run(
    payload: DiscoveryRunCreateIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    if payload.kind == "document_class_property" and payload.parent_class_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "parent_class_id is required for document_class_property discovery",
        )
    count, sampled = await expand_filter(
        session, payload.filter, payload.parent_class_id, payload.sample_size
    )

    if payload.dry_run:
        return DiscoveryRunCreateOut(
            run_id=None, document_count=count, sampled=sampled, status="would_start"
        )
    if count == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "filter selected zero documents — refusing to create an empty run",
        )

    run = DiscoveryRun(
        kind=payload.kind,
        status="pending",
        mode=payload.mode,
        filter=payload.filter.model_dump(mode="json"),
        parent_class_id=payload.parent_class_id,
        sample_size=payload.sample_size,
        total_docs=count,
        scanned_docs=0,
        failed_docs=0,
        candidate_count=0,
        proposal_count=0,
        started_by=caller.user_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(run)
    await session.flush()
    await materialize_run(session, run)
    # Submit the scan batch synchronously, in this handler, using the
    # caller's bearer. No background worker; subsequent state transitions
    # are driven by GET /discovery/runs/{id} (live-fetch + atomic claim).
    client = SinasClient(base_url=get_settings().sinas_url, token=caller.sinas_token)
    await submit_scan(session, run, client)
    await session.commit()
    return DiscoveryRunCreateOut(
        run_id=run.id, document_count=count, sampled=sampled, status="started"
    )


_FRONT_MATTER_KINDS: frozenset[str] = frozenset(
    {"document_class", "document_class_property", "entity_type"}
)


@router.post(
    "/suggest",
    response_model=SuggestOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def suggest(
    payload: SuggestIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Unified suggest pipeline: deterministic front-matter scan + LLM
    discovery, both writing into the same proposal queue. The front-matter
    pass runs synchronously (free, fast); the LLM run is queued for the
    background worker."""
    if payload.kind == "document_class_property" and payload.parent_class_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "parent_class_id is required for document_class_property suggest",
        )

    fm_run_id: uuid.UUID | None = None
    fm_proposal_count = 0
    fm_with_fm = 0
    if payload.include_front_matter and payload.kind in _FRONT_MATTER_KINDS:
        fm_run, _, fm_proposal_count, fm_with_fm = await run_front_matter_suggest(
            session,
            f=payload.filter,
            sample_size=None,  # front-matter is cheap; scan everything matched
            parent_class_id=payload.parent_class_id,
            started_by=caller.user_id,
        )
        fm_run_id = fm_run.id

    discovery_run_id: uuid.UUID | None = None
    discovery_count = 0
    discovery_sampled = False
    if not payload.skip_llm:
        count, sampled = await expand_filter(
            session, payload.filter, payload.parent_class_id, payload.sample_size
        )
        if count == 0 and fm_run_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "filter selected zero documents",
            )
        if count > 0:
            run = DiscoveryRun(
                kind=payload.kind,
                status="pending",
                mode=payload.mode,
                filter=payload.filter.model_dump(mode="json"),
                parent_class_id=payload.parent_class_id,
                sample_size=payload.sample_size,
                total_docs=count,
                scanned_docs=0,
                failed_docs=0,
                candidate_count=0,
                proposal_count=0,
                started_by=caller.user_id,
                created_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()
            await materialize_run(session, run)
            client = SinasClient(
                base_url=get_settings().sinas_url, token=caller.sinas_token
            )
            await submit_scan(session, run, client)
            await session.commit()
            discovery_run_id = run.id
            discovery_count = count
            discovery_sampled = sampled

    if fm_run_id is None and discovery_run_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "neither front-matter nor LLM discovery ran — check kind, "
            "skip_llm, and include_front_matter flags",
        )

    return SuggestOut(
        front_matter_run_id=fm_run_id,
        front_matter_proposal_count=fm_proposal_count,
        front_matter_documents_with_fm=fm_with_fm,
        discovery_run_id=discovery_run_id,
        discovery_document_count=discovery_count,
        discovery_sampled=discovery_sampled,
    )


@router.post(
    "/front-matter-suggest",
    response_model=FrontMatterSuggestOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def front_matter_suggest(
    payload: FrontMatterSuggestIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Synchronous front-matter scan. Parses YAML headers across the selected
    docs, aggregates by key, and writes a completed DiscoveryRun with
    DiscoveryCandidate + ConfigProposal rows for review."""
    run, candidate_count, proposal_count, docs_with_fm = await run_front_matter_suggest(
        session,
        f=payload.filter,
        sample_size=payload.sample_size,
        parent_class_id=payload.parent_class_id,
        started_by=caller.user_id,
    )
    return FrontMatterSuggestOut(
        run_id=run.id,
        document_count=run.total_docs,
        documents_with_front_matter=docs_with_fm,
        candidate_count=candidate_count,
        proposal_count=proposal_count,
    )


@router.get("/runs", response_model=list[DiscoveryRunOut])
async def list_discovery_runs(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """List recent discovery runs and drive in-flight runs forward.

    The discovery RunsList view polls this endpoint every few seconds. For
    any non-terminal run we run `progress(...)` so the user doesn't have to
    open each run individually to advance its state (the per-run GET does
    the same thing). Failures here are swallowed — they shouldn't take
    the list endpoint down."""
    rows = list(
        (
            await session.execute(
                select(DiscoveryRun).order_by(DiscoveryRun.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    )
    in_flight = [r for r in rows if r.status not in ("completed", "failed", "cancelled") and r.kind != "front_matter"]
    if in_flight and caller.sinas_token:
        client = SinasClient(base_url=get_settings().sinas_url, token=caller.sinas_token)
        for r in in_flight:
            try:
                await discovery_progress(r, client)
            except Exception as exc:  # noqa: BLE001
                import logging as _log
                _log.getLogger(__name__).warning(
                    "discovery progress fetch failed for run %s: %s", r.id, exc
                )
        # Refresh rows so the response reflects any status transitions.
        for r in rows:
            await session.refresh(r)
    return rows


@router.get("/runs/{run_id}", response_model=DiscoveryRunOut)
async def get_discovery_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    """Returns the run row, **after** advancing its state from Sinas.

    This is the only place run state advances — there's no background worker.
    Each GET fetches live batch status; on terminal scan it reconciles units
    and atomically fires the consolidator; on terminal consolidate it marks
    the run completed.

    Polling cadence is up to the caller (UI). Cheap: one or two batch GETs
    per call.
    """
    row = await session.get(DiscoveryRun, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "discovery run not found")
    # Skip live-fetch for terminal runs — nothing to do.
    if row.status in ("completed", "failed", "cancelled"):
        return row
    if row.kind == "front_matter":
        # Synchronous run — no Sinas batch to track.
        return row
    client = SinasClient(base_url=get_settings().sinas_url, token=caller.sinas_token)
    try:
        await discovery_progress(row, client)
    except Exception as exc:  # noqa: BLE001
        # Don't fail the GET if Sinas is unreachable — return last-known state.
        # Real issues will surface on next poll.
        import logging as _log
        _log.getLogger(__name__).warning(
            "discovery progress fetch failed for run %s: %s", run_id, exc
        )
    # Re-read the row in case progress() mutated it.
    await session.refresh(row)
    return row


@router.get("/runs/{run_id}/candidates")
async def list_run_candidates(
    run_id: uuid.UUID,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
):
    """Used by the consolidator agent to read all raw findings for a run."""
    rows = (
        await session.execute(
            select(DiscoveryCandidate)
            .where(DiscoveryCandidate.run_id == run_id)
            .order_by(DiscoveryCandidate.created_at)
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "kind": r.kind,
            "payload": r.payload,
            "evidence_document_id": str(r.evidence_document_id) if r.evidence_document_id else None,
            "evidence_span": r.evidence_span,
            "confidence": r.confidence,
        }
        for r in rows
    ]


# ─────────────────────── candidate write (agent → sgr) ───────────────────────
@router.post(
    "/candidates",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def submit_discovery_candidate(
    payload: DiscoveryCandidateIn,
    session: AsyncSession = Depends(get_session),
):
    run = await session.get(DiscoveryRun, payload.run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    cand = DiscoveryCandidate(
        run_id=payload.run_id,
        kind=payload.kind,
        payload=payload.payload,
        evidence_document_id=payload.evidence_document_id,
        evidence_span=payload.evidence_span,
        confidence=payload.confidence,
        created_at=datetime.now(timezone.utc),
    )
    session.add(cand)
    await session.commit()
    return {"id": cand.id}


# ─────────────────────── consolidated proposal write (consolidator → sgr) ───────────────────────
@router.post(
    "/proposals",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def submit_consolidated_proposal(
    payload: ConsolidatedProposalIn,
    session: AsyncSession = Depends(get_session),
):
    run = await session.get(DiscoveryRun, payload.run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    proposal = ConfigProposal(
        kind=payload.kind,
        payload=payload.payload,
        status="pending",
        supporting_candidate_ids=[str(cid) for cid in payload.supporting_candidate_ids],
        discovery_run_id=payload.run_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(proposal)
    await session.commit()
    return {"id": proposal.id}


# ─────────────────────── proposal review ───────────────────────
@router.get("/proposals", response_model=list[ConfigProposalOut])
async def list_proposals(
    kind: str | None = None,
    status_filter: str | None = "pending",
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(ConfigProposal).order_by(ConfigProposal.created_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(ConfigProposal.kind == kind)
    if status_filter:
        stmt = stmt.where(ConfigProposal.status == status_filter)
    return (await session.execute(stmt)).scalars().all()


@router.get("/proposals/{proposal_id}", response_model=ConfigProposalOut)
async def get_proposal(
    proposal_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    row = await session.get(ConfigProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    return row


@router.get("/proposals/{proposal_id}/candidates")
async def get_proposal_candidates(
    proposal_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Return the raw discovery_candidate rows folded into this proposal."""
    proposal = await session.get(ConfigProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    ids = [uuid.UUID(s) for s in (proposal.supporting_candidate_ids or [])]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(DiscoveryCandidate).where(DiscoveryCandidate.id.in_(ids))
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "payload": r.payload,
            "evidence_document_id": str(r.evidence_document_id) if r.evidence_document_id else None,
            "evidence_span": r.evidence_span,
            "confidence": r.confidence,
        }
        for r in rows
    ]


@router.patch(
    "/proposals/{proposal_id}",
    response_model=ConfigProposalOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def edit_proposal(
    proposal_id: uuid.UUID,
    payload: ProposalEditIn,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(ConfigProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "proposal already resolved")
    row.payload = payload.payload
    await session.commit()
    await session.refresh(row)
    return row


@router.post(
    "/proposals/{proposal_id}/approve",
    response_model=ConfigProposalOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def approve_proposal(
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = await session.get(ConfigProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "proposal already resolved")

    # Materialize the proposal as a real config row.
    payload = row.payload or {}
    name = payload.get("name") or "unnamed"
    created_id = await _materialize_proposal(session, row.kind, payload, name)

    row.status = "approved"
    row.created_resource_id = created_id
    row.reviewed_at = datetime.now(timezone.utc)
    row.reviewed_by = caller.user_id
    await session.commit()
    await session.refresh(row)
    return row


@router.post(
    "/proposals/{proposal_id}/reject",
    response_model=ConfigProposalOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def reject_proposal(
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = await session.get(ConfigProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "proposal already resolved")
    row.status = "rejected"
    row.reviewed_at = datetime.now(timezone.utc)
    row.reviewed_by = caller.user_id
    await session.commit()
    await session.refresh(row)
    return row


@router.post(
    "/proposals/{proposal_id}/merge",
    response_model=ConfigProposalOut,
    dependencies=[Depends(require_permission("sgr.admin:all"))],
)
async def merge_proposal(
    proposal_id: uuid.UUID,
    payload: ProposalMergeIn,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(get_caller),
):
    row = await session.get(ConfigProposal, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "proposal already resolved")
    row.status = "merged"
    row.merged_into_id = payload.target_id
    row.reviewed_at = datetime.now(timezone.utc)
    row.reviewed_by = caller.user_id
    await session.commit()
    await session.refresh(row)
    return row


# ─────────────────────── helpers ───────────────────────
_REF_TYPE_TO_MODEL = {
    "document_class": DocumentClass,
    "entity_type": EntityType,
    "dossier_class": DossierClass,
}


async def _resolve_ref(
    session: AsyncSession,
    ref_type: str,
    ref_id: str | None,
    ref_name: str | None,
) -> uuid.UUID:
    """Resolve a relationship-ref to a UUID. Accepts either explicit id or name."""
    if ref_id:
        try:
            return uuid.UUID(ref_id)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"invalid {ref_type} id: {ref_id}"
            ) from exc

    if not ref_name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{ref_type} requires either *_ref_id or *_ref_name in the payload",
        )
    model = _REF_TYPE_TO_MODEL.get(ref_type)
    if model is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown ref_type: {ref_type}"
        )
    row = (
        await session.execute(select(model).where(model.name == ref_name))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"no {ref_type} named '{ref_name}' — approve that first or edit the payload",
        )
    return row.id


def _hints_to_text(hints):
    """Discovery may emit classification_hints as a list; the column is Text.
    Join a list into newline-separated text; pass through strings/None."""
    if isinstance(hints, list):
        return "\n".join(str(h) for h in hints)
    return hints


async def _materialize_proposal(
    session: AsyncSession, kind: str, payload: dict, name: str
) -> uuid.UUID:
    """Turn an approved proposal payload into a real config row. Returns its id."""
    if kind == "document_class":
        row = DocumentClass(
            slug=payload.get("slug") or slugify(name),
            name=name,
            description=payload.get("description"),
            classification_hints=_hints_to_text(payload.get("classification_hints")),
            summarization_guidance=payload.get("summarization_guidance"),
        )
    elif kind == "entity_type":
        row = EntityType(
            name=name,
            description=payload.get("description"),
            guidance=payload.get("guidance"),
        )
    elif kind == "relationship_definition":
        # Discovery emits source/target as NAMES; resolve to ids by lookup
        # against the existing config. Either an id or a name must be given.
        source_ref_id = await _resolve_ref(
            session,
            payload["source_ref_type"],
            payload.get("source_ref_id"),
            payload.get("source_ref_name"),
        )
        target_ref_id = await _resolve_ref(
            session,
            payload["target_ref_type"],
            payload.get("target_ref_id"),
            payload.get("target_ref_name"),
        )
        row = RelationshipDefinition(
            name=name,
            description=payload.get("description"),
            source_ref_type=payload["source_ref_type"],
            source_ref_id=source_ref_id,
            target_ref_type=payload["target_ref_type"],
            target_ref_id=target_ref_id,
            cardinality=payload.get("cardinality", "many"),
            extraction_guidance=payload.get("extraction_guidance"),
            discovery_guidance=payload.get("discovery_guidance"),
        )
    elif kind == "dossier_class":
        row = DossierClass(
            slug=payload.get("slug") or slugify(name),
            name=name,
            description=payload.get("description"),
            guidance=payload.get("guidance"),
            summarization_guidance=payload.get("summarization_guidance"),
            classification_hints=_hints_to_text(payload.get("classification_hints")),
        )
    elif kind == "document_class_property":
        row = DocumentClassProperty(
            document_class_id=uuid.UUID(payload["document_class_id"]),
            name=name,
            description=payload.get("description"),
            schema=payload.get("schema") or {"type": "string"},
            guidance=payload.get("guidance"),
            manual=bool(payload.get("manual", False)),
            required=bool(payload.get("required", False)),
            cardinality=payload.get("cardinality", "one"),
        )
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown proposal kind: {kind}"
        )
    session.add(row)
    await session.flush()
    # Suppress unused-import warning for response models referenced by docs/types.
    _ = (DocumentClassOut, EntityTypeOut, RelationshipDefinitionOut, DossierClassOut, DocumentClassPropertyOut)
    return row.id
