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
from app.models import AnswerClaim, ClaimEvidence, Document, DocumentClass, DocumentVersion
from app.services.toc import derive_toc

# Lines of context around the cited span; enough to judge support without
# re-litigating the document.
_SPAN_MARGIN = 5
_MAX_SPAN_CHARS = 4000
_MAX_CONCURRENCY = 8

_VALIDATOR_AGENT = "sgr/evidence-check-agent"
# The pre-publication sweep runs on the strong tier: voice and modality
# judgments proved beyond the cheap model exactly where the verdict is
# final. Cycle-internal checks stay on the cheap tier.
_FINAL_VALIDATOR_AGENT = "sgr/evidence-check-final-agent"

_PROMPT = """CLAIM: {claim}

The claim cites {n} evidence span(s). Spans support a claim JOINTLY — one
span may carry one clause of the claim and another span the rest. Judge each
span on whether it substantively supports the part of the claim it covers,
given the other spans.

{spans_block}

A span may carry a `section:` label — the heading of the part of the
document it sits in, computed from the document's own table of contents.
Use it: the same sentence carries different weight inside a section that
recites background or positions the document did not author than inside
the part where the document states its own findings or its outcome.

A span FAILS if it is only tangentially related, contradicts the claim, or
covers no part of it (e.g. the claim's precise figure appears in no span).
A span PASSES if it substantively grounds a part of the claim, even if other
parts are grounded by the other spans.

Then judge the claim AS A WHOLE. Per-span verdicts say whether each span
carries the part it covers; they cannot say whether anything is left over.
Take every proposition the claim asserts — each figure, date, attribution,
causal reason, and any generalisation across the things the document treats
separately — and check it against the union of the passing spans.

A NAME IS NOT A STATEMENT. A title, a heading, a caption, an index or
reference entry, a table-of-contents line, a list of names, a
bibliographic line: each establishes that the thing it names exists and
how this document refers to it, and nothing else. It asserts nothing ABOUT
that thing. Only running text does. A claim resting on such a line is
carried only for the existence of what the line names, and FAILS for
anything said about it.

Attribution is itself a proposition. If the claim says WHERE its content
comes from — "in X", "the author concluded", "according to Y" — that
provenance must be carried by the passages or by the document's own
identification below. A passage may discuss X while belonging to a
document that is something else entirely; if the document identifies
itself as something other than what the claim attributes, COVERAGE is
PARTIAL and the mismatch is what you name.

Voice is part of attribution. A passage may be the document's OWN voice —
the author or body whose document this is, speaking for itself — or the
document REPORTING speech it did not author: a position it records, a
submission it recounts, a message or exhibit it reproduces. Check how the
sentence is introduced, and check the document identification, for whose
words these are. If the claim presents reported words as the document's
own finding, or as something established rather than merely asserted by
the speaker, that proposition is NOT carried: the span FAILS for it, and
you name whose voice the passage actually is. A claim that attributes the
words to their true voice IS carried. Reported speech NESTS: a document by
one author may itself recite a position held by someone else ("in the view
of…", "according to…") — judge the words introducing the quoted sentences
themselves, not only who wrote the document. This is about reported
positions only: a document neutrally reporting what another document
established is ordinary support — do not fail a span for being a secondary
account.

Modality is part of coverage. The claim may not state more strongly, or
more broadly, than the source: a hedge ("possibly", "may"), a statement
confined to the particular situation the passage addresses, and a finding
the document marks as provisional must all survive into the claim.
"Possibly X" is not "X", and something said of one situation is not a
general proposition. Name any strengthening or broadening as missing
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

_SPAN_TMPL = """SPAN {i} (stance: {stance}; lines {line_from}-{line_to}{section}):
---
{span_text}
---"""

_STANCES = {
    "supports": ("is claimed to SUPPORT the claim", "support"),
    "contradicts": ("is claimed to CONTRADICT the claim", "contradict"),
    "qualifies": ("is claimed to QUALIFY (limit) the claim", "qualify"),
}


async def _domain_guidance(cited_classes: set[str] | None = None) -> str:
    """Deployment-supplied validation guidance (playbooks of kind
    `validation`), scope-filtered.

    The core prompt states the generic rules — voice, modality, attribution.
    What those look like in a given corpus (the reporting formulas, the
    hedges, the document conventions) is domain knowledge and comes from the
    deployment's config, never from this file. A playbook with no scope rows
    applies to every validation; one scoped to document classes is included
    only when the answer actually cites a document of that class — so
    per-family conventions accumulate in their own entries instead of
    bloating the generic guidance. Empty when none is installed.
    """
    from app.db import AsyncSessionLocal
    from app.models import DocumentClass, Playbook, PlaybookScope

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Playbook.id, Playbook.content)
            .where(Playbook.kind == "validation")
            .order_by(Playbook.name))).all()
        if not rows:
            return ""
        scoped = (await session.execute(
            select(PlaybookScope.playbook_id, DocumentClass.name)
            .outerjoin(DocumentClass,
                       DocumentClass.id == PlaybookScope.document_class_id)
            .where(PlaybookScope.playbook_id.in_([r[0] for r in rows]))
        )).all()
    classes_by_pb: dict = {}
    for pb_id, cls in scoped:
        classes_by_pb.setdefault(pb_id, set()).add(cls)
    parts = []
    for pb_id, content in rows:
        if not content:
            continue
        pb_classes = classes_by_pb.get(pb_id) or set()
        # No scope rows, or only the everywhere-sentinel (None) → global.
        applies = pb_classes <= {None} or bool(
            pb_classes & (cited_classes or set()))
        if applies:
            parts.append(content.strip())
    if not parts:
        return ""
    return ("CORPUS GUIDANCE (what the rules above look like in this "
            "corpus):\n" + "\n\n".join(parts) + "\n\n")


def _front_matter_extent(content: str) -> int:
    """Last line (1-based) of the ingestion-written front-matter block, or 0.

    Ingestion normalizes every document to open with a `---` fenced
    key:value block. Those lines are the pipeline's own envelope — they
    identify the document; they are not its text. The boundary is exact
    because this code's own ingestion wrote it, so no pattern matching and
    no assumption about the source document is involved.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, min(len(lines), 60)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def _fold(text: str) -> str:
    """Letters and digits, lower-cased, everything else gone.

    What a paragraph label survives being copied through: a source printing
    "42." and a drafter writing "42", "r.o. 4.2" and "R.O. 4.2", a label
    that picked up a trailing space. Punctuation and spacing are the part
    that varies; the characters that identify the paragraph are the part
    that does not. Pure.
    """
    return "".join(ch.lower() for ch in (text or "") if ch.isalnum())


def _span_lines(content: str, span: dict[str, Any]) -> str:
    """The span's own lines, without the judging margin. The margin exists
    so a judge can read around a passage; a locator has to be IN the passage
    it labels, so it is checked against exactly those lines. Empty when the
    coordinates do not name lines that exist. Pure."""
    lines = (content or "").splitlines()
    try:
        lf = int(span.get("line_from") or 0)
        lt = int(span.get("line_to") or lf)
    except (TypeError, ValueError):
        return ""
    if lf < 1 or lt < lf or lt > len(lines):
        return ""
    return "\n".join(lines[lf - 1:lt])


def locator_holds(locator: str | None, span_text: str) -> bool | None:
    """Does the claimed paragraph label actually appear in the span?

    True, False, or None when there is nothing to check. Deterministic and
    total — no model is asked, and no pattern says what a paragraph label
    looks like in a given corpus, because nothing here can know that. The
    drafter proposes the label it read off the passage and this says whether
    the passage shows it; a label the passage does not show is a citation
    pointing somewhere a reader cannot follow, which is worse than a
    citation with no paragraph at all.

    A substring match, on the folded forms, because a label is a short token
    and the ways a faithful copy of one differs from the source are exactly
    what folding removes. It is a floor and not a proof: a bare "42" can
    match digits that are not a paragraph number. A weak check that refuses
    an invented "recital 99" and lets an ambiguous "42" through is the right
    trade for a check whose failure throws a citation away. Pure.
    """
    want = _fold(locator)
    if not want:
        return None
    return want in _fold(span_text)


def _span_section(toc: list[dict], line_from: int, line_to: int) -> str:
    """The innermost TOC section containing the span, with its parent —
    computed from the document's own headings. Position inside a document's
    structure is data the judge cannot infer from a ±5-line window: the same
    sentence means something different inside a section that reports
    positions than inside the author's own conclusions."""
    inner = None
    for e in toc:
        if e["line"] <= line_from and line_to <= e.get("line_to", 0):
            if inner is None or e["level"] >= inner["level"]:
                inner = e
    if inner is None:
        return ""
    parent = None
    for e in toc:
        if (e["level"] < inner["level"] and e["line"] <= inner["line"]
                and inner["line"] <= e.get("line_to", 0)):
            parent = e
    path = (f"{parent['title']} > " if parent else "") + inner["title"]
    return path[:160]


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
    agent: str = _VALIDATOR_AGENT,
) -> list[dict[str, Any]]:
    """One judging call for a claim and ALL its spans; per-span verdicts."""
    spans_block = "\n\n".join(
        _SPAN_TMPL.format(
            i=i + 1, stance=ev.stance, line_from=lf, line_to=lt, span_text=txt,
            section=f"; section: {sec}" if sec else "")
        for i, (ev, txt, lf, lt, sec) in enumerate(rows)
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
                f"{settings.sinas_url}/agents/{agent}/invoke",
                headers={"Authorization": f"Bearer {settings.sinas_api_key}"},
                json={"message": prompt},
                timeout=120.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            reply = (payload.get("reply") or "").strip()
            if run_id is not None:
                from app.services.query_runner import record_llm_call

                await record_llm_call(run_id, payload.get("chat_id"), agent)
        except Exception as exc:
            return [{"evidence_id": ev.id, "error": f"invoke failed: {exc}"} for ev, *_ in rows]

    verdicts: list[dict[str, Any]] = []
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    for i, (ev, *_rest) in enumerate(rows):
        line = next((line for line in lines if line.upper().startswith(f"SPAN {i + 1}:")), None)
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

    cov = next((line for line in lines if line.upper().startswith("COVERAGE:")), None)
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
    final: bool = False,
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

    doc_rows = (await session.execute(
        select(Document.id, Document.filename, DocumentClass.name)
        .outerjoin(DocumentClass, DocumentClass.id == Document.document_class_id)
        .where(Document.id.in_({r[0].document_id for r in rows}))
    )).all()
    filenames = {i: fn for i, fn, _ in doc_rows}
    doc_classes = {i: cls for i, _, cls in doc_rows}

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
    toc_cache: dict[int, list[dict]] = {}
    fm_cache: dict[int, int] = {}
    pre_verdicts: list[dict[str, Any]] = []
    for ev, claim in rows:
        content = await _content_for(ev)
        if not content:
            errors.append({"evidence_id": ev.id, "error": "no extracted content",
                           "claim_sequence": claim.sequence})
            continue
        span_text, lf, lt = _slice_span(content, ev.span or {})
        # Deterministic: a span wholly inside the front-matter envelope is
        # the document identifying itself, not the document speaking. The
        # expert reviews found claims resting on exactly these lines — an
        # assertion about what a document established, cited to that
        # document's own title block — and the judge sometimes passed them.
        # A span that merely STARTS in the envelope and runs into the body is
        # left to the judge: it covers real text.
        ck = id(content)
        if ck not in fm_cache:
            fm_cache[ck] = _front_matter_extent(content)
        fm_end = fm_cache[ck]
        s_lf = int((ev.span or {}).get("line_from") or lf)
        s_lt = int((ev.span or {}).get("line_to") or lt)
        if fm_end and s_lt <= fm_end:
            pre_verdicts.append({
                "evidence_id": ev.id, "validated": False,
                "reasoning": (
                    f"span (lines {s_lf}-{s_lt}) lies entirely within the "
                    f"document's front-matter block (lines 1-{fm_end}), which "
                    "identifies the document and carries none of its "
                    "reasoning — it cannot support a claim"),
            })
            continue
        # Deterministic, and before any judging call: the paragraph label the
        # drafter attached to this span has to be IN the span. It is the one
        # part of a citation a reader uses to find the passage in the source,
        # and it is the one part nothing downstream can verify — so it is
        # verified here, against the lines the span actually names, or the
        # span is failed and the label discarded. A span that claims no
        # label is untouched: the check is on what was asserted, and
        # asserting nothing is not a defect.
        held = locator_holds((ev.span or {}).get("locator"),
                             _span_lines(content, ev.span or {}))
        if held is False:
            ev.paragraph_ref = None
            pre_verdicts.append({
                "evidence_id": ev.id, "validated": False,
                "reasoning": (
                    f"the paragraph label \"{(ev.span or {}).get('locator')}\" "
                    f"does not appear in the cited lines "
                    f"{s_lf}-{s_lt} — a citation must point at a passage the "
                    "reader can find, and this one points at a label the "
                    "passage does not carry"),
            })
            continue
        if held is True:
            ev.paragraph_ref = str((ev.span or {}).get("locator"))[:50]
        # The span's position in the document's own structure, from the
        # deterministic TOC — cached per content object, not per row.
        ck = id(content)
        if ck not in toc_cache:
            toc_cache[ck] = derive_toc(content)
        section = _span_section(
            toc_cache[ck],
            int((ev.span or {}).get("line_from") or lf),
            int((ev.span or {}).get("line_to") or lt))
        entry = by_claim.setdefault(
            claim.id,
            {"claim_id": claim.id, "claim_text": claim.claim_text,
             "sequence": claim.sequence, "rows": [], "doc_heads": {}},
        )
        entry["rows"].append((ev, span_text, lf, lt, section))
        # The document's own opening lines, so the judge can check that a
        # claim's stated provenance matches what the document IS. Raw source
        # text, not interpretation: one answer attributed a finding to one
        # proceeding while citing the document of another, and per-span
        # entailment structurally could not see it — the passage really does
        # discuss those facts, in a document about something else.
        fn = filenames.get(ev.document_id) or str(ev.document_id)
        if fn not in entry["doc_heads"]:
            # The document's ingestion-assigned class leads its head block:
            # what kind of thing the document IS — in the deployment's own
            # words for its classes — is a judgment input, and the raw head
            # lines below stay as the document's own testimony to check the
            # class against.
            cls = doc_classes.get(ev.document_id)
            head = "\n".join(
                line for line in content.splitlines()[:15] if line.strip())[:800]
            entry["doc_heads"][fn] = (
                (f"(classified at ingestion as: {cls})\n" if cls else "") + head)
    guidance = await _domain_guidance(
        {c for c in doc_classes.values() if c}) if by_claim else ""
    async with httpx.AsyncClient() as client:
        grouped = await asyncio.gather(*[
            _judge_claim(client, sem, settings, e["claim_text"], e["rows"],
                         e["claim_id"], e["sequence"], run_id, e["doc_heads"],
                         domain_guidance=guidance,
                         agent=_FINAL_VALIDATOR_AGENT if final else _VALIDATOR_AGENT)
            for e in by_claim.values()
        ]) if by_claim else []
    verdicts = pre_verdicts + [v for group in grouped for v in group]

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
            # With the sequence attached. An errored row is never marked
            # validated, so it stays pending and is re-judged next round for
            # free — the same shape as a failing span, and a caller that
            # excludes claims on that ground needs to see these too. Without
            # it the claim is invisible by number and its free re-judge looks
            # like work. The id may be missing or unknown on a malformed
            # verdict, so the lookup cannot assume either.
            ev_err = by_id.get(v.get("evidence_id"))
            errors.append({**v, "claim_sequence": claims_by_ev[ev_err.id].sequence}
                          if ev_err is not None else v)
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
