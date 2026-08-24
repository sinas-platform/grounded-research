"""Bulk ingestion API — thin trigger over app.bulk_pipeline.

Design rule (16 Aug): the API process NEVER executes bulk work. It
registers documents (cheap DB writes) and spawns the batch pipeline as a
detached subprocess. Job progress is derived from data, so a crashed
subprocess leaves a resumable job, not a mystery.

POST /bulk/upload    multipart zip of .md files -> register all -> spawn
                     pipeline over them -> {job_id}
POST /bulk/jobs      {document_ids, stages?} -> spawn pipeline -> {job_id}
GET  /bulk/jobs/{id} progress counts derived from the database
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity, get_caller
from app.db import get_session
from app.models import Document, DocumentVersion

router = APIRouter(prefix="/bulk", tags=["bulk"])

JOBS_ROOT = Path.home() / "sgr-bulk-jobs"


def _spawn(job_id: str, doc_ids: list[str], stages: str) -> Path:
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    ids_file = job_dir / "ids.txt"
    ids_file.write_text("\n".join(doc_ids))
    log_path = job_dir / "pipeline.log"
    backend = Path(__file__).resolve().parents[3]
    with open(log_path, "ab") as logf:
        subprocess.Popen(
            [sys.executable, "-m", "app.bulk_pipeline",
             "--ids-file", str(ids_file), "--stages", stages,
             "--job-dir", str(job_dir)],
            cwd=str(backend), stdout=logf, stderr=logf,
            start_new_session=True,
        )
    return job_dir


class BulkJobIn(BaseModel):
    document_ids: list[uuid.UUID]
    stages: str = "extract,resolve,relationships"


class BulkJobOut(BaseModel):
    job_id: str
    document_count: int
    job_dir: str


@router.post("/jobs", response_model=BulkJobOut,
             status_code=status.HTTP_202_ACCEPTED)
async def create_job(payload: BulkJobIn,
                     caller: CallerIdentity = Depends(get_caller)):
    if not payload.document_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no documents")
    job_id = uuid.uuid4().hex[:12]
    _spawn(job_id, [str(d) for d in payload.document_ids], payload.stages)
    return BulkJobOut(job_id=job_id,
                      document_count=len(payload.document_ids),
                      job_dir=str(JOBS_ROOT / job_id))


@router.post("/upload", response_model=BulkJobOut,
             status_code=status.HTTP_202_ACCEPTED)
async def upload_zip(file: UploadFile,
                     source: str | None = Query(
                         default=None,
                         description="Connector/source name; with it each "
                                     "file's name becomes its external_ref "
                                     "(the source's natural key)."),
                     staged: bool = Query(
                         default=False,
                         description="Hold the documents back from the live "
                                     "corpus (schema still being designed). "
                                     "Nothing unstages them automatically."),
                     session: AsyncSession = Depends(get_session),
                     caller: CallerIdentity = Depends(get_caller)):
    """Zip of .md files in -> registered, pipeline spawned.

    Documents land live by default. Staging is a deliberate choice — it hides
    them from the default listing until someone unstages them, and a bulk load
    that stages by mistake leaves a corpus that looks empty.
    """
    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not a zip")
    job_id = uuid.uuid4().hex[:12]
    doc_ids: list[str] = []
    unchanged = 0
    duplicates = 0
    from app.services.toc import normalize_line_density

    for name in zf.namelist():
        base = Path(name).name
        if not base.endswith(".md") or base.startswith("."):
            continue
        content = zf.read(name).decode("utf-8", errors="replace")
        content = content.replace("\x00", "").replace("\\u0000", "")
        content = normalize_line_density(content)
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Identity resolution, strongest first: the connector's natural key
        # (source, external_ref) when given; else filename — the external
        # ref of last resort. Grove is the record; no Sinas collection id
        # is minted (24,721 dangling fabricated ids taught us that).
        existing = None
        if source:
            existing = (await session.execute(
                select(Document).where(Document.source == source,
                                       Document.external_ref == base)
            )).scalars().first()
        if existing is None:
            existing = (await session.execute(
                select(Document).where(Document.filename == base)
            )).scalars().first()
        if existing is not None:
            doc = existing
            latest = (await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version.desc()).limit(1)
            )).scalars().first()
            if latest is not None and latest.content_hash == content_hash:
                # Same identity, same bytes: the retry-amplification case.
                # Idempotent — nothing new to store or process.
                unchanged += 1
                continue
            version = (latest.version + 1) if latest else 1
        else:
            # No identity match — but identical content under a different
            # name is an exact duplicate, not a new document.
            same = (await session.execute(
                select(Document)
                .join(DocumentVersion,
                      DocumentVersion.id == Document.current_version_id)
                .where(DocumentVersion.content_hash == content_hash)
            )).scalars().first()
            if same is not None:
                duplicates += 1
                continue
            doc = Document(filename=base,
                           source=source or None,
                           external_ref=base if source else None,
                           owner_id=caller.user_id,
                           roles=caller.roles or [], staged=staged)
            session.add(doc)
            await session.flush()
            version = 1
        dv = DocumentVersion(document_id=doc.id, version=version,
                             content_md=content, content_hash=content_hash)
        session.add(dv)
        await session.flush()
        doc.current_version_id = dv.id
        doc_ids.append(str(doc.id))
    await session.commit()
    if not doc_ids and not unchanged and not duplicates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "zip had no .md files")
    _spawn(job_id, doc_ids, "extract,resolve,relationships")
    return BulkJobOut(job_id=job_id, document_count=len(doc_ids),
                      job_dir=str(JOBS_ROOT / job_id))


@router.get("/jobs/{job_id}")
async def job_status(job_id: str,
                     session: AsyncSession = Depends(get_session)):
    job_dir = JOBS_ROOT / job_id
    ids_file = job_dir / "ids.txt"
    if not ids_file.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job")
    ids = [uuid.UUID(l) for l in ids_file.read_text().splitlines() if l.strip()]
    from sqlalchemy import func, text

    extracted = (await session.execute(text(
        "SELECT count(*) FROM document WHERE id = ANY(:ids) "
        "AND summary IS NOT NULL AND summary != ''"),
        {"ids": ids})).scalar()
    with_rels = (await session.execute(text(
        "SELECT count(DISTINCT d.id) FROM document d "
        "WHERE d.id = ANY(:ids) AND (EXISTS (SELECT 1 FROM relationship r "
        "WHERE r.evidence_document_id = d.id) OR EXISTS (SELECT 1 FROM "
        "unresolved_relationship ur WHERE ur.evidence_document_id = d.id))"),
        {"ids": ids})).scalar()
    report_path = job_dir / "report.json"
    return {
        "job_id": job_id,
        "document_count": len(ids),
        "extracted": extracted,
        "with_relationships": with_rels,
        "finished": report_path.exists(),
        "report": json.loads(report_path.read_text())
        if report_path.exists() else None,
    }
