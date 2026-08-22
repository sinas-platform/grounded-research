"""Stateless per-row faithfulness validation.

Each evidence verdict — does this span support this claim with this stance? —
is an independent judgment over ~1k tokens. Running it inside a conversational
validator agent made every verdict carry the whole answer transcript
(measured: $1.50–3 and minutes of sequential rounds per answer). Here SGR
assembles each (claim, span) pair server-side and fans them out as parallel
single-shot invocations of the tool-less `sgr/evidence-check-agent`, then
writes the verdicts directly. No transcript, no enumeration round-trips, no
paging; usage still lands in Sinas's llm_usage ledger.

The judging model/prompt stays independent of the drafting agent, which is the
property the faithfulness gate actually needs — independence, not
conversation.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CallerIdentity
from app.config import get_settings
from app.models import AnswerClaim, ClaimEvidence, Document, DocumentVersion

# Lines of context around the cited span; enough to judge support without
# re-litigating the document.
_SPAN_MARGIN = 5
_MAX_SPAN_CHARS = 4000
_MAX_CONCURRENCY = 8

_VALIDATOR_AGENT = "sgr/evidence-check-agent"

_PROMPT = """CLAIM: {claim}

The claim cites {n} evidence span(s). Spans support a claim JOINTLY — one
span may carry one clause of the claim and another span the rest. Judge each
span on whether it substantively supports the part of the claim it covers,
given the other spans.

{spans_block}

A span FAILS if it is only tangentially related, contradicts the claim, or
covers no part of it (e.g. the claim's precise figure appears in no span).
A span PASSES if it substantively grounds a part of the claim, even if other
parts are grounded by the other spans.

Then judge the claim AS A WHOLE. Per-span verdicts say whether each span
carries the part it covers; they cannot say whether anything is left over.
Take every proposition the claim asserts — each figure, date, attribution,
causal reason, and any generalisation across jurisdictions or authorities —
and check it against the union of the passing spans. A case caption or party
list establishes only that the case exists and which court decided it; it
carries no holding.

Attribution is itself a proposition. If the claim says WHERE its content
comes from — "in case X", "the court held", "according to author Y" — that
provenance must be carried by the passages or by the document's own
identification below. A passage may discuss X while belonging to a
different case entirely; if the document identifies itself as something
other than what the claim attributes, COVERAGE is PARTIAL and the mismatch
is what you name.

Voice is part of attribution. A passage may be the deciding or authoring
voice of its document, or the document REPORTING the words of an advocate
or interested party. Check the passage's framing and the document
identification for whose voice the quoted words are. If the claim presents
a reported advocate's words as the decider's own finding or as an
established rule, that proposition is NOT carried: the span FAILS for it,
and you name whose voice the passage actually is. A claim that attributes
the words to their true voice is carried. Reported speech NESTS: a
document by one author may itself recite a position held by someone else
("in the view of…", "according to…") — judge the words introducing the
quoted sentences themselves, not only who wrote the document. This is
about advocacy voices only: a source neutrally reporting what was decided
is ordinary support — do not fail a span for being a secondary account.

Modality is part of coverage. The claim may not state more strongly than
the source: a hedge ("possibly", "may"), a case-specific aside, or a
provisional finding must survive into the claim; "possibly X" is not "X",
and a speculative aside is not a rule. Name any strengthening as missing
coverage.

DOCUMENT IDENTIFICATION (the opening of each cited document, verbatim):
{doc_heads}

{domain_guidance}COVERAGE is FULL only if every proposition is carried. Otherwise PARTIAL,
and name what is missing.

Reply with exactly one line PER SPAN, in order, then one COVERAGE line,
nothing else:
SPAN 1: PASS — <one short clause>
SPAN 2: FAIL — <one short clause>
COVERAGE: FULL — <one short clause>
or
COVERAGE: PARTIAL — <the propositions no span establishes>"""

_SPAN_TMPL = """SPAN {i} (stance: {stance}; lines {line_from}-{line_to}):
---
{span_text}
---"""

_STANCES = {
    "supports": ("is claimed to SUPPORT the claim", "support"),
    "contradicts": ("is claimed to CONTRADICT the claim", "contradict"),
    "qualifies": ("is claimed to QUALIFY (limit) the claim", "qualify"),
}


async def _domain_guidance() -> str:
    """Deployment-supplied validation guidance (playbook kind `validation`).

    The core prompt states the generic rules — voice, modality, attribution.
    What those look like in a given corpus (the reporting formulas, the
    hedges, the document conventions) is domain knowledge and comes from the
    deployment's config, never from this file. Empty when none is installed.
    """
    from app.db import AsyncSessionLocal
    from app.models import Playbook

    async with AsyncSessionLocal() as session:
        content = (await session.execute(
            select(Playbook.content).where(Playbook.kind == "validation")
            .limit(1))).scalar_one_or_none()
    if not content:
        return ""
    return ("CORPUS GUIDANCE (what the rules above look like in this "
            "corpus):\n" + content.strip() + "\n\n")


def _slice_span(content_md: str, span: dict[str, Any]) -> tuple[str, int, int]:
    lines = content_md.splitlines()
    total = len(lines)
    line_from = max(1, int(span.get("line_from") or 1) - _SPAN_MARGIN)
    line_to = min(total, int(span.get("line_to") or total) + _SPAN_MARGIN)
    text = "\n".join(lines[line_from - 1 : line_to])
    if len(text) > _MAX_SPAN_CHARS:
        text = text[:_MAX_SPAN_CHARS] + "\n[... span truncated for judging ...]"
    return text, line_from, line_to


async def _judge_claim(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    settings,
    claim_text: str,
    rows: list[tuple[ClaimEvidence, str, int, int]],
    claim_id: uuid.UUID | None = None,
    sequence: int | None = None,
    run_id: uuid.UUID | None = None,
    doc_heads: dict[str, str] | None = None,
    domain_guidance: str = "",
) -> list[dict[str, Any]]:
    """One judging call for a claim and ALL its spans; per-span verdicts."""
    spans_block = "\n\n".join(
        _SPAN_TMPL.format(i=i + 1, stance=ev.stance, line_from=lf, line_to=lt, span_text=txt)
        for i, (ev, txt, lf, lt) in enumerate(rows)
    )
    heads = "\n".join(
        f"[{fn}]\n{head}" for fn, head in sorted((doc_heads or {}).items())
    ) or "(unavailable)"
    prompt = _PROMPT.format(claim=claim_text, n=len(rows),
                            spans_block=spans_block, doc_heads=heads,
                            domain_guidance=domain_guidance)
    async with sem:
        try:
            resp = await client.post(
                f"{settings.sinas_url}/agents/{_VALIDATOR_AGENT}/invoke",
                headers={"Authorization": f"Bearer {settings.sinas_api_key}"},
                json={"message": prompt},
                timeout=120.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            reply = (payload.get("reply") or "").strip()
            if run_id is not None:
                from app.services.query_runner import record_llm_call

                await record_llm_call(run_id, payload.get("chat_id"),
                                      _VALIDATOR_AGENT)
        except Exception as exc:
            return [{"evidence_id": ev.id, "error": f"invoke failed: {exc}"} for ev, *_ in rows]

    verdicts: list[dict[str, Any]] = []
    lines = [l.strip() for l in reply.splitlines() if l.strip()]
    for i, (ev, *_rest) in enumerate(rows):
        line = next((l for l in lines if l.upper().startswith(f"SPAN {i + 1}:")), None)
        if line is None:
            verdicts.append({"evidence_id": ev.id, "error": f"no verdict line for span {i + 1}"})
            continue
        body = line.split(":", 1)[1].strip()
        upper = body.upper()
        if upper.startswith("PASS"):
            ok = True
        elif upper.startswith("FAIL"):
            ok = False
        else:
            verdicts.append({"evidence_id": ev.id, "error": f"unparseable: {body[:60]}"})
            continue
        reason = body.split("—", 1)[1].strip() if "—" in body else (
            body.split("-", 1)[1].strip() if "-" in body else body
        )
        verdicts.append({"evidence_id": ev.id, "validated": ok, "reasoning": reason[:500]})

    cov = next((l for l in lines if l.upper().startswith("COVERAGE:")), None)
    if cov:
        body = cov.split(":", 1)[1].strip()
        full = body.upper().startswith("FULL")
        why = body.split("—", 1)[1].strip() if "—" in body else body
        verdicts.append({"claim_coverage": "full" if full else "partial",
                         "claim_id": str(claim_id) if claim_id else None,
                         "claim_sequence": sequence,
                         "claim_text": claim_text,
                         "uncovered": why[:500]})
    return verdicts


async def validate_answer_evidence(
    session: AsyncSession,
    caller: CallerIdentity,
    answer_id: uuid.UUID,
    pending_only: bool = True,
    run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Judge (pending) evidence rows of an answer as parallel stateless calls
    and record the verdicts. Returns a summary the synthesis agent can act on
    directly; rows whose judging errored stay unvalidated and are listed."""
    settings = get_settings()
    stmt = (
        select(ClaimEvidence, AnswerClaim)
        .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
        .where(AnswerClaim.answer_id == answer_id)
        .order_by(AnswerClaim.sequence)
    )
    if pending_only:
        stmt = stmt.where(ClaimEvidence.validated.is_(False))
    rows = (await session.execute(stmt)).all()
    if not rows:
        return {"judged": 0, "passed": 0, "failed": [], "errors": []}

    filenames = dict((await session.execute(
        select(Document.id, Document.filename)
        .where(Document.id.in_({r[0].document_id for r in rows}))
    )).all())

    # Resolve span text per row (documents may repeat across rows — cache).
    version_cache: dict[uuid.UUID, str | None] = {}

    async def _content_for(row: ClaimEvidence) -> str | None:
        vid = row.document_version_id
        if vid is None:
            dv = (
                await session.execute(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == row.document_id)
                    .order_by(DocumentVersion.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if dv is None:
                return None
            vid = dv.id
            version_cache.setdefault(vid, dv.content_md)
        if vid not in version_cache:
            dv = await session.get(DocumentVersion, vid)
            version_cache[vid] = dv.content_md if dv else None
        return version_cache[vid]

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    errors: list[dict[str, Any]] = []
    by_claim: dict[uuid.UUID, dict[str, Any]] = {}
    for ev, claim in rows:
        content = await _content_for(ev)
        if not content:
            errors.append({"evidence_id": ev.id, "error": "no extracted content"})
            continue
        span_text, lf, lt = _slice_span(content, ev.span or {})
        entry = by_claim.setdefault(
            claim.id,
            {"claim_id": claim.id, "claim_text": claim.claim_text,
             "sequence": claim.sequence, "rows": [], "doc_heads": {}},
        )
        entry["rows"].append((ev, span_text, lf, lt))
        # The document's own opening lines, so the judge can check that a
        # claim's stated provenance matches what the document IS. Raw source
        # text, not interpretation: one answer attributed a holding to
        # Delivery Hero/Glovo citing the Naspers/Just Eat Takeaway decision,
        # and per-span entailment could not see it — the passage really does
        # discuss those facts, in a different case's decision.
        fn = filenames.get(ev.document_id) or str(ev.document_id)
        if fn not in entry["doc_heads"]:
            entry["doc_heads"][fn] = "\n".join(
                l for l in content.splitlines()[:15] if l.strip())[:800]
    guidance = await _domain_guidance() if by_claim else ""
    async with httpx.AsyncClient() as client:
        grouped = await asyncio.gather(*[
            _judge_claim(client, sem, settings, e["claim_text"], e["rows"],
                         e["claim_id"], e["sequence"], run_id, e["doc_heads"],
                         domain_guidance=guidance)
            for e in by_claim.values()
        ]) if by_claim else []
    verdicts = [v for group in grouped for v in group]

    by_id = {ev.id: ev for ev, _ in rows}
    claims_by_ev = {ev.id: c for ev, c in rows}
    passed, failed = 0, []
    overreaching: list[dict[str, Any]] = []
    for v in verdicts:
        if v.get("claim_coverage"):
            if v["claim_coverage"] != "full":
                overreaching.append(v)
            continue
        if "error" in v:
            errors.append(v)
            continue
        ev = by_id[v["evidence_id"]]
        ev.validated = v["validated"]
        ev.validation_reasoning = v["reasoning"]
        if v["validated"]:
            passed += 1
        else:
            failed.append(
                {
                    "evidence_id": str(ev.id),
                    "claim_id": str(ev.claim_id),
                    "claim_sequence": claims_by_ev[ev.id].sequence,
                    "reason": v["reasoning"],
                }
            )
    await session.commit()
    return {
        "judged": passed + len(failed),
        "passed": passed,
        "failed": failed,
        # claims whose spans each pass but which assert more than the spans
        # carry — the defect per-span judging structurally cannot see
        "overreaching": overreaching,
        "errors": [
            {**e, "evidence_id": str(e["evidence_id"])} if not isinstance(e.get("evidence_id"), str) else e
            for e in errors
        ],
    }
