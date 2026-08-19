"""One-shot dossier assignment: one tool-less LLM call per document.

The retired agentic dossier stage walked tools (get_dossier_classes,
find_candidate_dossiers, assign per match). Deployments without dossier
classes paid an agent invocation just to find that out.
Here the server checks the config first: no dossier classes means no LLM
call and a free skip. With classes configured, one CHEAP_LLM call gets the
document's identity (class, summary, properties) and the candidate
dossiers, and returns assignments by name; the server maps names to ids
and writes DossierDocument links (unique per pair, so reruns are
idempotent). Unmatched suggestions are reported, never written.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import Document
from app.models.config import DossierClass
from app.models.runtime import Dossier, DossierDocument, PropertyValue
from app.services.ingestion_oneshot import _parse_json_reply
from app.services.query_runner import _Sinas

log = logging.getLogger(__name__)

DOSSIER_AGENT = "grove/dossier-oneshot-agent"
_MAX_CANDIDATES = 100

_PROMPT = """Assign one document to the dossiers it belongs to. Reply with ONLY a JSON object, no prose.

DOSSIER CLASSES:
{classes}

CANDIDATE DOSSIERS (assign only to these, by exact name):
{candidates}

DOCUMENT:
filename: {filename}
class: {doc_class}
summary: {summary}
properties: {properties}

Reply JSON schema:
{{"assignments": [{{"dossier": "<exact candidate name>", "confidence": <0..1>, "reason": "<short>"}}]}}

Rules: only assign when the document clearly belongs (same matter, case,
transaction or investigation). No matches = empty list. Never invent
dossier names."""


async def assign_document(
    session: AsyncSession,
    sinas: _Sinas,
    document_id: uuid.UUID,
    *,
    write: bool = True,
) -> dict[str, Any]:
    doc = await session.get(Document, document_id)
    report: dict[str, Any] = {
        "document": doc.filename if doc else str(document_id),
        "llm_calls": 0, "assigned": 0, "unmatched": 0,
    }
    if doc is None:
        report["skipped"] = "document not found"
        return report

    classes = (await session.execute(select(DossierClass))).scalars().all()
    if not classes:
        report["skipped"] = "no dossier classes configured"
        return report

    dossiers = (
        await session.execute(
            select(Dossier).where(Dossier.closed_at.is_(None)).limit(_MAX_CANDIDATES)
        )
    ).scalars().all()
    if not dossiers:
        report["skipped"] = "no open dossiers"
        return report

    existing = {
        d
        for d in (
            await session.execute(
                select(DossierDocument.dossier_id).where(
                    DossierDocument.document_id == document_id
                )
            )
        ).scalars().all()
    }

    props = (
        await session.execute(
            select(PropertyValue).where(PropertyValue.document_id == document_id)
        )
    ).scalars().all()

    prompt = _PROMPT.format(
        classes="\n".join(f"- {c.name}: {c.description or ''}" for c in classes),
        candidates="\n".join(f"- {d.name}" for d in dossiers),
        filename=doc.filename or "",
        doc_class=str(doc.document_class_id or "unclassified"),
        summary=(doc.summary or "")[:2000],
        properties={str(p.property_id): p.value for p in props},
    )
    reply = await sinas.invoke(DOSSIER_AGENT, prompt)
    report["llm_calls"] = 1
    try:
        assignments = _parse_json_reply(reply).get("assignments") or []
    except ValueError:
        report["unparsed"] = True
        return report

    by_name = {d.name.strip().lower(): d for d in dossiers}
    for a in assignments if isinstance(assignments, list) else []:
        if not isinstance(a, dict):
            continue
        target = by_name.get(str(a.get("dossier") or "").strip().lower())
        if target is None:
            report["unmatched"] += 1
            continue
        if target.id in existing:
            continue
        existing.add(target.id)
        if write:
            session.add(
                DossierDocument(
                    dossier_id=target.id,
                    document_id=document_id,
                    role="auto",
                )
            )
        report["assigned"] += 1
    if write:
        await session.commit()
    return report


async def assign_dossiers(
    document_ids: list[uuid.UUID], *, write: bool = True
) -> list[dict[str, Any]]:
    """Assign the given documents to dossiers. Free no-op per document
    when no dossier classes are configured."""
    sinas = _Sinas()
    reports = []
    for did in document_ids:
        async with AsyncSessionLocal() as session:
            try:
                reports.append(
                    await assign_document(session, sinas, did, write=write)
                )
            except Exception as exc:  # noqa: BLE001 — per-doc isolation
                reports.append({"document": str(did), "error": str(exc)[:300]})
    return reports
