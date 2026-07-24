"""Answer remediation: gate findings closed by one stateless call each run.

The chat-based remediation loop under-complies: the drafter fixes the easy
findings and silently skips the hard ones (observed on run dd6615c9 — claims
revised, but no reconciling claim and no citation rebinds). This module
replaces the chat nudge with the pipeline's own mechanism:

  1. the answer gate produces findings,
  2. ONE stateless remediation-agent call must return a disposition per
     finding — a concrete claim action with line-numbered evidence, or a
     reasoned rejection; silent skipping is structurally impossible,
  3. the server applies the actions to the same tables the drafting API
     writes,
  4. the standard evidence checker judges every new span; failures are
     rolled back (added claims deleted, revised claims restored),
  5. the gate reads the result once more; remaining findings are recorded,
     not re-looped.

Single pass, hard stop. No chat, no drafter discretion.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import AnswerClaim, ClaimEvidence, Document
from app.models.runtime import DocumentVersion
from app.models.query import QueryRun
from app.services.query_runner import _gate_answer, _runner_caller, _Sinas, _tele

REMEDIATION_AGENT = "grove/remediation-agent"
_MAX_DOCS = 6
_MAX_DOC_CHARS = 40_000


def _numbered(content: str) -> str:
    lines = content.splitlines()
    out, used = [], 0
    for i, line in enumerate(lines, start=1):
        row = f"{i}│{line}"
        used += len(row) + 1
        if used > _MAX_DOC_CHARS:
            out.append(f"[... truncated at line {i} of {len(lines)} ...]")
            break
        out.append(row)
    return "\n".join(out)


def _filenames_in(texts: list[str]) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for m in re.findall(r"[\w][\w.\-]*\.md", t):
            if m not in seen:
                seen.append(m)
    return seen


def _claim_seqs_in(texts: list[str]) -> set[int]:
    seqs: set[int] = set()
    for t in texts:
        for grp in re.findall(r"[Cc]laims? ([\d,\sand]+)", t):
            seqs.update(int(n) for n in re.findall(r"\d+", grp))
    return seqs


def _parse_reply(reply: str) -> dict:
    cleaned = reply.strip().strip("`").removeprefix("json").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    return json.loads(cleaned[start : end + 1])


async def remediate_answer(run_id: uuid.UUID) -> dict[str, Any]:
    """One remediation pass over a run's answer. Returns a full audit dict."""
    sinas = _Sinas()
    async with AsyncSessionLocal() as session:
        run = await session.get(QueryRun, run_id)
        answer_id, question = run.answer_id, run.question
        caller = _runner_caller(run)

    ok, missing, issues = await _gate_answer(sinas, question, answer_id)
    findings = ([f"The claims do not fully answer the question. Missing: {missing}"]
                if not ok else []) + issues
    audit: dict[str, Any] = {"findings": findings}
    if not findings:
        audit["result"] = "nothing to remediate"
        return audit

    # ── assemble context ───────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        claims = (
            await session.execute(
                select(AnswerClaim)
                .where(AnswerClaim.answer_id == answer_id)
                .order_by(AnswerClaim.sequence)
            )
        ).scalars().all()
        cited: dict[uuid.UUID, list[str]] = {}
        for c in claims:
            files = (
                await session.execute(
                    select(Document.filename)
                    .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
                    .where(ClaimEvidence.claim_id == c.id)
                )
            ).scalars().all()
            cited[c.id] = sorted(set(files))

        # documents worth showing: named in findings first, then those cited
        # by claims the findings name
        want = _filenames_in(findings)
        flagged_seqs = _claim_seqs_in(findings)
        for c in claims:
            if c.sequence in flagged_seqs:
                for f in cited[c.id]:
                    if f not in want:
                        want.append(f)
        want = want[:_MAX_DOCS]

        excerpts: dict[str, str] = {}
        doc_ids: dict[str, tuple[uuid.UUID, uuid.UUID | None]] = {}
        for fn in want:
            row = (
                await session.execute(select(Document).where(Document.filename == fn))
            ).scalars().first()
            if row is None or row.current_version_id is None:
                continue
            content = (
                await session.execute(
                    select(DocumentVersion.content_md).where(
                        DocumentVersion.id == row.current_version_id
                    )
                )
            ).scalar_one_or_none()
            if content:
                excerpts[fn] = _numbered(content)
                doc_ids[fn] = (row.id, row.current_version_id)

    claims_block = "\n".join(
        f"{c.sequence}. [{c.claim_type or '?'}] {c.claim_text}\n   cites: {', '.join(cited[c.id]) or '(none)'}"
        for c in claims
    )
    findings_block = "\n".join(f"F{i+1}. {f}" for i, f in enumerate(findings))
    docs_block = "\n\n".join(f"### {fn}\n{txt}" for fn, txt in excerpts.items())
    schema = (
        '{"dispositions": [\n'
        '  {"finding": <F-number>, "action": "add_claim", "claim_type": "<type>", "claim_text": "...",\n'
        '   "evidence": [{"filename": "<one of the excerpted files>", "line_from": N, "line_to": M, "stance": "supports"}]},\n'
        '  {"finding": <F-number>, "action": "revise_claim", "sequence": N, "claim_text": "...",\n'
        '   "evidence": [ ... optional: REPLACES the claim\'s evidence ... ]},\n'
        '  {"finding": <F-number>, "action": "reject", "reason": "..."}\n'
        "]}"
    )
    message = (
        f"QUESTION:\n{question}\n\nCURRENT CLAIMS:\n{claims_block}\n\n"
        f"REVIEW FINDINGS (one disposition each):\n{findings_block}\n\n"
        f"NUMBERED DOCUMENT EXCERPTS:\n{docs_block}\n\nReply ONLY JSON:\n{schema}"
    )

    reply = await sinas.invoke(REMEDIATION_AGENT, message)
    try:
        dispositions = _parse_reply(reply).get("dispositions") or []
    except Exception as exc:
        audit["result"] = f"unparseable remediation reply: {exc}"
        audit["reply_head"] = reply[:500]
        return audit
    audit["dispositions"] = dispositions

    # ── apply ──────────────────────────────────────────────────────────────
    added_ids: list[uuid.UUID] = []
    backups: dict[uuid.UUID, dict] = {}
    applied, skipped = [], []
    async with AsyncSessionLocal() as session:
        seq_max = max((c.sequence for c in claims), default=0)
        by_seq = {c.sequence: c.id for c in claims}
        for d in dispositions:
            action = d.get("action")
            if action == "reject":
                skipped.append(d)
                continue
            evs = d.get("evidence") or []
            resolved = [
                (doc_ids[e["filename"]], e) for e in evs if e.get("filename") in doc_ids
            ]
            if action == "add_claim":
                if not resolved:
                    skipped.append({**d, "reason": "no resolvable evidence"})
                    continue
                seq_max += 1
                row = AnswerClaim(
                    answer_id=answer_id,
                    sequence=seq_max,
                    claim_text=str(d.get("claim_text") or "")[:8000],
                    claim_type=str(d.get("claim_type") or "synthesis")[:100],
                )
                session.add(row)
                await session.flush()
                added_ids.append(row.id)
                for (did, dvid), e in resolved:
                    session.add(
                        ClaimEvidence(
                            claim_id=row.id,
                            document_id=did,
                            document_version_id=dvid,
                            span={"line_from": int(e["line_from"]), "line_to": int(e["line_to"])},
                            stance=str(e.get("stance") or "supports"),
                            validated=False,
                        )
                    )
                applied.append({"action": "add_claim", "sequence": seq_max})
            elif action == "revise_claim":
                cid = by_seq.get(int(d.get("sequence") or 0))
                if cid is None:
                    skipped.append({**d, "reason": "unknown sequence"})
                    continue
                claim = await session.get(AnswerClaim, cid)
                old_evs = (
                    await session.execute(
                        select(ClaimEvidence).where(ClaimEvidence.claim_id == cid)
                    )
                ).scalars().all()
                backups[cid] = {
                    "text": claim.claim_text,
                    "evidence": [
                        {
                            "document_id": e.document_id,
                            "document_version_id": e.document_version_id,
                            "span": e.span,
                            "stance": e.stance,
                            "validated": e.validated,
                            "validation_reasoning": e.validation_reasoning,
                        }
                        for e in old_evs
                    ],
                }
                claim.claim_text = str(d.get("claim_text") or claim.claim_text)[:8000]
                if resolved:
                    for e in old_evs:
                        await session.delete(e)
                    for (did, dvid), e in resolved:
                        session.add(
                            ClaimEvidence(
                                claim_id=cid,
                                document_id=did,
                                document_version_id=dvid,
                                span={"line_from": int(e["line_from"]), "line_to": int(e["line_to"])},
                                stance=str(e.get("stance") or "supports"),
                                validated=False,
                            )
                        )
                applied.append({"action": "revise_claim", "sequence": claim.sequence})
        await session.commit()
    audit["applied"], audit["rejected_by_agent"] = applied, skipped

    # ── validate the new spans with the standard checker ───────────────────
    from app.services.faithfulness import validate_answer_evidence

    async with AsyncSessionLocal() as session:
        verdict = await validate_answer_evidence(
            session, caller, answer_id, pending_only=True
        )
    audit["validation"] = {
        "judged": verdict["judged"],
        "passed": verdict["passed"],
        "failed": len(verdict["failed"]),
        "errors": len(verdict["errors"]),
    }

    # ── roll back what failed ──────────────────────────────────────────────
    failed_claims = {f["claim_id"] for f in verdict["failed"]}
    rolled_back = []
    async with AsyncSessionLocal() as session:
        for cid in failed_claims:
            cid = uuid.UUID(str(cid))
            if cid in set(added_ids):
                await session.execute(
                    ClaimEvidence.__table__.delete().where(ClaimEvidence.claim_id == cid)
                )
                await session.execute(
                    AnswerClaim.__table__.delete().where(AnswerClaim.id == cid)
                )
                rolled_back.append({"added_claim": str(cid), "outcome": "deleted"})
            elif cid in backups:
                b = backups[cid]
                claim = await session.get(AnswerClaim, cid)
                claim.claim_text = b["text"]
                await session.execute(
                    ClaimEvidence.__table__.delete().where(ClaimEvidence.claim_id == cid)
                )
                for e in b["evidence"]:
                    session.add(ClaimEvidence(claim_id=cid, **e))
                rolled_back.append({"revised_claim": str(cid), "outcome": "restored"})
        await session.commit()
    audit["rolled_back"] = rolled_back

    # ── final gate read; record, never loop ────────────────────────────────
    ok2, missing2, issues2 = await _gate_answer(sinas, question, answer_id)
    audit["final_gate"] = {"publishable": ok2, "missing": missing2, "issues": issues2}
    await _tele(run_id, "validate", remediation=audit)
    return audit
