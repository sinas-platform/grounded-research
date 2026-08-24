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
    from app.services.document_registry import register_document

    for name in zf.namelist():
        base = Path(name).name
        if not base.endswith(".md") or base.startswith("."):
            continue
        content = zf.read(name).decode("utf-8", errors="replace")
        reg = await register_document(
            session, filename=base, content=content,
            owner_id=caller.user_id, roles=caller.roles,
            source=source, staged=staged)
        if reg.outcome == "unchanged":
            unchanged += 1
        elif reg.outcome == "duplicate":
            duplicates += 1
        else:
            doc_ids.append(str(reg.document.id))
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
