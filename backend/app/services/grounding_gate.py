"""Grounding gate: drop extractor names the document does not support.

Extractors sometimes emit names that are not in the text (translations,
summaries of a reference, outright hallucination). The gate checks every
active mention against the document content:

  verbatim   surface_form appears in content_md (exact or case-insensitive)
             — passes free, nothing written
  judged     everything else goes to ONE batched CHEAP_LLM call per
             document ("does this text refer to this name?"); the verdict
             and reason land in link_evidence["grounding"], kept mentions
             stay active, ungrounded ones become status='rejected_ungrounded'

Soft drop: rejected rows stay for audit; every consumer filters
status = 'active'. link_method is untouched — that field records how a
mention was linked, not why it was hidden. A judge reply that cannot be
parsed leaves every mention active: a gate outage must never mass-hide
mentions. Idempotent: gating an already-gated document re-judges only
mentions still active and non-verbatim.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import Document, Entity, EntityMention
from app.models.runtime import DocumentVersion
from app.services.query_runner import _Sinas

GROUNDING_AGENT = "grove/grounding-agent"
_CONTEXT_CHARS = 240
_REJECT_CONFIDENCE = 0.6

STATUS_ACTIVE = "active"
STATUS_REJECTED = "rejected_ungrounded"


def is_verbatim(surface: str, content: str) -> bool:
    if not surface:
        return False
    return surface in content or surface.lower() in content.lower()


def _slice(content: str, span: dict | None) -> str:
    """Context around the mention when the stored offsets are usable;
    empty otherwise (extractor offsets are frequently approximate)."""
    if not isinstance(span, dict):
        return ""
    start = span.get("char_start", span.get("start"))
    end = span.get("char_end", span.get("end"))
    try:
        a = max(0, int(start) - _CONTEXT_CHARS)
        b = min(len(content), int(end) + _CONTEXT_CHARS)
    except (TypeError, ValueError):
        return ""
    return content[a:b].replace("\n", " ")


_JUDGE_PROMPT = """For each name, judge from the document whether the document
actually refers to it. A name is grounded when the document names it, in any
spelling, abbreviation or language, or unambiguously describes it. A name is
ungrounded when nothing in the document refers to it. Prefer grounded when in
doubt: wrongly hiding a real name is worse than keeping a doubtful one.
Reply ONLY JSON:
{{"verdicts": [{{"name": <n>, "grounded": true|false, "confidence": <0..1>, "reason": "<short>"}}]}}

DOCUMENT (beginning):
{head}

NAMES:
{names}"""


def _parse_verdicts(reply: str) -> dict[int, dict] | None:
    try:
        cleaned = reply.strip().strip("`").removeprefix("json").strip()
        data = json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])
        out: dict[int, dict] = {}
        for v in data.get("verdicts") or []:
            out[int(v.get("name") or 0)] = v
        return out
    except Exception:
        return None


async def ground_document(
    session: AsyncSession,
    sinas: _Sinas,
    document_id: uuid.UUID,
    *,
    write: bool = True,
) -> dict[str, Any]:
    doc = await session.get(Document, document_id)
    mentions = (
        await session.execute(
            select(EntityMention)
            .where(EntityMention.document_id == document_id)
            .where(EntityMention.status == STATUS_ACTIVE)
        )
    ).scalars().all()
    # legacy mentions (pre mentions-first) carry no surface_form and no
    # span text; their extractor's claim survives only as the linked
    # entity's canonical form — load those for the fallback
    canonical: dict[uuid.UUID, str] = {}
    linked_ids = [m.entity_id for m in mentions if m.entity_id is not None]
    if linked_ids:
        canonical = dict((
            await session.execute(
                select(Entity.id, Entity.canonical_form)
                .where(Entity.id.in_(linked_ids))
            )
        ).all())
    report: dict[str, Any] = {
        "document": doc.filename if doc else str(document_id),
        "active": len(mentions),
        "verbatim": 0, "kept": 0, "rejected": 0, "unparsed_kept": 0,
        "no_surface_skipped": 0,
        "llm_calls": 0,
    }
    if not mentions:
        return report
    content = ""
    if doc and doc.current_version_id:
        content = (
            await session.execute(
                select(DocumentVersion.content_md).where(
                    DocumentVersion.id == doc.current_version_id
                )
            )
        ).scalar_one_or_none() or ""

    def surface_of(m: EntityMention) -> str:
        return str(
            m.surface_form
            or (m.span or {}).get("text")
            or canonical.get(m.entity_id)
            or ""
        ).strip()

    needs_judge: list[EntityMention] = []
    for m in mentions:
        surface = surface_of(m)
        if not surface:
            # nothing to check against: no surface, no span text, no
            # linked canonical form — never send "?" to the judge
            report["no_surface_skipped"] += 1
        elif is_verbatim(surface, content):
            report["verbatim"] += 1
        else:
            needs_judge.append(m)
    if not needs_judge:
        return report

    blocks = []
    for i, m in enumerate(needs_judge, start=1):
        surface = surface_of(m)
        line = f'NAME {i}: "{surface}"'
        ctx = _slice(content, m.span)
        if ctx:
            line += f"\nCONTEXT: …{ctx}…"
        blocks.append(line)
    prompt = _JUDGE_PROMPT.format(
        head=content[:6000].replace("\n", " "), names="\n\n".join(blocks)
    )
    reply = await sinas.invoke(GROUNDING_AGENT, prompt)
    report["llm_calls"] = 1
    verdicts = _parse_verdicts(reply)
    if verdicts is None:
        # unparseable judge reply: keep everything active, never mass-hide
        report["unparsed_kept"] = len(needs_judge)
        return report

    for i, m in enumerate(needs_judge, start=1):
        v = verdicts.get(i) or {}
        grounded = bool(v.get("grounded", True))
        conf = float(v.get("confidence") or 0.0)
        evidence = {
            "grounded": grounded,
            "confidence": conf,
            "reason": str(v.get("reason") or "")[:300],
            "judge": "grounding-gate",
        }
        if write:
            m.link_evidence = {**(m.link_evidence or {}), "grounding": evidence}
        if not grounded and conf >= _REJECT_CONFIDENCE:
            if write:
                m.status = STATUS_REJECTED
            report["rejected"] += 1
        else:
            report["kept"] += 1
    if write:
        await session.commit()
    return report


async def ground_documents(
    document_ids: list[uuid.UUID] | None = None, *, write: bool = True
) -> list[dict[str, Any]]:
    """Gate the given documents (or every document with active mentions)."""
    sinas = _Sinas()
    if document_ids is None:
        async with AsyncSessionLocal() as session:
            document_ids = list((
                await session.execute(
                    select(EntityMention.document_id)
                    .where(EntityMention.status == STATUS_ACTIVE)
                    .distinct()
                )
            ).scalars().all())
    reports = []
    for did in document_ids:
        async with AsyncSessionLocal() as session:
            reports.append(
                await ground_document(session, sinas, did, write=write)
            )
    return reports
