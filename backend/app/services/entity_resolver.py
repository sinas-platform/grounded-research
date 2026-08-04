"""Tiered entity resolution: link raw mentions to canonical entities.

Mentions arrive from ingestion as verbatim surface forms with character
offsets and NULL entity_id (mentions-first, PR #40). This module links them
in four tiers, cheapest first:

  T1 natural key    a pattern-derived identity (case number, ECLI) matches
                    an entity's — deterministic, immune to spelling
  T2 exact match    normalized surface equals exactly one entity's
                    canonical form (or its party-set, for case styles) —
                    ambiguity or a type mismatch REFUSES and falls to T3
  T3 adjudication   one stateless CHEAP_LLM call per document: each
                    remaining mention with its context slice (a free
                    string-slice, thanks to stored offsets) against a small
                    candidate list — the judge picks a letter or NONE
  T4 creation       per the type's creation_mode: open → create + link,
                    review → proposal, closed → leave unlinked

Every link records link_method / link_confidence / link_evidence.
Idempotent: only rows with entity_id IS NULL are touched; re-running
relinks nothing and costs nothing.

Natural-key patterns are code for now (same pragmatic precedent as
CLASS_RULES in ingestion_oneshot) — flagged to move to package config.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import Document, Entity, EntityMention
from app.models.config import EntityType
from app.models.runtime import DocumentVersion, EntityProposal
from app.services.query_runner import _Sinas

RESOLVER_AGENT = "grove/entity-resolution-agent"
_CONTEXT_CHARS = 240
_MAX_CANDIDATES = 8

NATURAL_KEY_PATTERNS: list[tuple[str, str]] = [
    # (regex, key template using backrefs) — first hit wins
    (r"ECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Z0-9.]+", r"\g<0>"),
    (r"\b([CT])-(\d{1,4})/(\d{2})(?:\s?P)?\b", r"CJEU:\1-\2/\3"),
    (r"\bM[.\s]?(\d{4,5})\b", r"EUMR:M.\1"),
    (r"\bAT[.\s]?(\d{5})\b", r"EUAT:AT.\1"),
    (r"\bC/(\d{4})/(\d{2})\b", r"CNMC:C/\1/\2"),
]


def natural_key(text: str) -> str | None:
    for pattern, template in NATURAL_KEY_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.expand(template).upper()
    return None


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def party_set(s: str) -> frozenset[str] | None:
    """Case styles: 'Facebook / WhatsApp' == 'WhatsApp v Facebook'."""
    parts = re.split(r"\s+v\.?\s+|\s+vs\.?\s+|\s*/\s*", s)
    parts = [normalize(p) for p in parts if normalize(p)]
    return frozenset(parts) if len(parts) >= 2 else None


class _EntityIndex:
    """In-memory index over live entities; built once per resolver run."""

    def __init__(self, rows: list[Entity]):
        self.by_id: dict[uuid.UUID, Entity] = {}
        self.nk: dict[str, list[uuid.UUID]] = defaultdict(list)
        self.norm: dict[str, list[uuid.UUID]] = defaultdict(list)
        self.party: dict[frozenset, list[uuid.UUID]] = defaultdict(list)
        self.token: dict[str, set[uuid.UUID]] = defaultdict(set)
        for e in rows:
            if e.merged_into_id is not None:
                continue
            self.by_id[e.id] = e
            nk = e.natural_key or natural_key(e.canonical_form)
            if nk:
                self.nk[nk].append(e.id)
            self.norm[normalize(e.canonical_form)].append(e.id)
            ps = party_set(e.canonical_form)
            if ps:
                self.party[ps].append(e.id)
            for tok in set(normalize(e.canonical_form).split()):
                if len(tok) >= 3:
                    self.token[tok].add(e.id)

    def candidates(self, surface: str, type_id: uuid.UUID | None) -> list[Entity]:
        toks = [t for t in set(normalize(surface).split()) if len(t) >= 3]
        scores: dict[uuid.UUID, float] = defaultdict(float)
        for t in toks:
            for eid in self.token.get(t, ()):
                scores[eid] += 1.0
        for eid in scores:
            if type_id and self.by_id[eid].entity_type_id == type_id:
                scores[eid] += 0.5
        ranked = sorted(scores, key=lambda i: -scores[i])
        return [self.by_id[i] for i in ranked[:_MAX_CANDIDATES]]


def _slice(content: str, span: dict | None) -> str:
    if not span or "char_start" not in span:
        return ""
    a = max(0, int(span["char_start"]) - _CONTEXT_CHARS)
    b = min(len(content), int(span["char_end"]) + _CONTEXT_CHARS)
    return content[a:b].replace("\n", " ")


_ADJUDICATE_PROMPT = """Link each mention to the entity it refers to, or NONE.
A mention refers to a candidate only if the CONTEXT supports it — the same
words in different documents can mean different entities. Do not guess:
prefer NONE over a doubtful match. Reply ONLY JSON:
{{"links": [{{"mention": <n>, "choice": "<letter>" | null, "confidence": <0..1>, "reason": "<short>"}}]}}

{mentions}"""


async def resolve_document(
    session: AsyncSession,
    sinas: _Sinas,
    index: _EntityIndex,
    types: dict[uuid.UUID, EntityType],
    document_id: uuid.UUID,
    *,
    write: bool = True,
) -> dict[str, Any]:
    doc = await session.get(Document, document_id)
    mentions = (
        await session.execute(
            select(EntityMention)
            .where(EntityMention.document_id == document_id)
            .where(EntityMention.entity_id.is_(None))
        )
    ).scalars().all()
    report: dict[str, Any] = {
        "document": doc.filename if doc else str(document_id),
        "unlinked": len(mentions),
        "natural_key": 0, "exact": 0, "adjudicated": 0,
        "created": 0, "proposed": 0, "left_unlinked": 0,
        "llm_calls": 0, "prompt_chars": 0, "reply_chars": 0,
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

    def link(m: EntityMention, eid: uuid.UUID, method: str, conf: float, ev: dict):
        m.entity_id = eid
        m.link_method = method
        m.link_confidence = conf
        m.link_evidence = ev
        report[method if method in report else "adjudicated"] += 1

    needs_judge: list[tuple[EntityMention, list[Entity]]] = []
    needs_creation: list[EntityMention] = []

    for m in mentions:
        surface = m.surface_form or ""
        # T1 — natural key
        nk = natural_key(surface)
        if nk and len(index.nk.get(nk, [])) == 1:
            link(m, index.nk[nk][0], "natural_key", 0.98, {"key": nk})
            continue
        # T2 — exact normalized / party-set match, unique and type-compatible
        hits = index.norm.get(normalize(surface), [])
        if not hits:
            ps = party_set(surface)
            hits = index.party.get(ps, []) if ps else []
        if len(hits) == 1:
            ent = index.by_id[hits[0]]
            if m.entity_type_id is None or ent.entity_type_id == m.entity_type_id:
                link(m, ent.id, "exact", 0.9, {"matched": ent.canonical_form})
                continue
        # T3 — collect candidates (ambiguous exacts included), filtered to
        # type-compatible ones: a Jurisdiction mention must never be offered
        # a Court entity (observed: 'Croatia' adjudicated onto a court)
        cands = ([index.by_id[h] for h in hits] or
                 index.candidates(surface, m.entity_type_id))
        if m.entity_type_id is not None:
            cands = [c for c in cands if c.entity_type_id == m.entity_type_id]
        if cands:
            needs_judge.append((m, cands))
        else:
            needs_creation.append(m)

    # T3 — one batched adjudication call for the whole document
    if needs_judge:
        blocks = []
        for i, (m, cands) in enumerate(needs_judge, start=1):
            tname = types[m.entity_type_id].name if m.entity_type_id in types else "?"
            lines = [f'MENTION {i}: "{m.surface_form}" (type: {tname})',
                     f'CONTEXT: …{_slice(content, m.span)}…', "CANDIDATES:"]
            for letter, c in zip("ABCDEFGH", cands):
                ctype = types[c.entity_type_id].name if c.entity_type_id in types else "?"
                lines.append(f"  {letter}. {c.canonical_form} (type: {ctype})")
            blocks.append("\n".join(lines))
        prompt = _ADJUDICATE_PROMPT.format(mentions="\n\n".join(blocks))
        reply = await sinas.invoke(RESOLVER_AGENT, prompt)
        report["llm_calls"] += 1
        report["prompt_chars"] += len(prompt)
        report["reply_chars"] += len(reply)
        verdicts: dict[int, dict] = {}
        try:
            cleaned = reply.strip().strip("`").removeprefix("json").strip()
            data = json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])
            for v in data.get("links") or []:
                verdicts[int(v.get("mention") or 0)] = v
        except Exception:
            pass  # unparseable → all fall through to creation
        for i, (m, cands) in enumerate(needs_judge, start=1):
            v = verdicts.get(i) or {}
            choice = v.get("choice")
            conf = float(v.get("confidence") or 0.0)
            if choice and conf >= 0.6 and str(choice).strip().upper()[:1] in "ABCDEFGH":
                idx = "ABCDEFGH".index(str(choice).strip().upper()[0])
                if idx < len(cands):
                    link(m, cands[idx].id, "adjudicated", conf,
                         {"reason": str(v.get("reason") or "")[:300]})
                    continue
            needs_creation.append(m)

    # T4 — creation per the type's creation_mode
    for m in needs_creation:
        t = types.get(m.entity_type_id)
        if t is None:
            report["left_unlinked"] += 1
            continue
        if t.creation_mode == "closed":
            report["left_unlinked"] += 1
        elif t.creation_mode == "review":
            if write:
                session.add(EntityProposal(
                    entity_type_id=t.id,
                    canonical_form=(m.surface_form or "")[:500],
                    proposing_agent="entity-resolver",
                    reasoning="unresolved mention",
                    evidence_document_id=document_id,
                    status="pending",
                ))
            report["proposed"] += 1
        else:  # open
            if write:
                nk = natural_key(m.surface_form or "")
                # create-or-link. The partition above ran before any
                # creation, so an in-document twin ("Case C-110/04" and
                # "Strintzis Lines ... Case C-110/04") lands here twice
                # with the same derived key; inserting both violates
                # ix_entity_natural_key and rolls back the whole
                # document's resolution. The index is updated on every
                # create, so consult it before inserting.
                if nk and index.nk.get(nk):
                    link(m, index.nk[nk][0], "natural_key", 0.95,
                         {"key": nk, "linked_instead_of_created": True})
                    continue
                ent = Entity(
                    entity_type_id=t.id,
                    canonical_form=(m.surface_form or "")[:500],
                    natural_key=nk,
                )
                try:
                    # savepoint: a plain failed flush would roll back
                    # every link already made for this document
                    async with session.begin_nested():
                        session.add(ent)
                        await session.flush()
                except IntegrityError:
                    # cross-session race: the keyed owner appeared after
                    # our index was built. The partial unique index
                    # guarantees exactly one live owner; link to it.
                    owner = (
                        await session.execute(
                            select(Entity)
                            .where(Entity.entity_type_id == t.id)
                            .where(Entity.natural_key == nk)
                            .where(Entity.merged_into_id.is_(None))
                        )
                    ).scalars().first()
                    if owner is None:
                        report["left_unlinked"] += 1
                        continue
                    index.by_id[owner.id] = owner
                    if nk:
                        index.nk[nk].append(owner.id)
                    link(m, owner.id, "natural_key", 0.95,
                         {"key": nk, "linked_after_collision": True})
                    continue
                # future mentions of the same name resolve to this entity
                index.by_id[ent.id] = ent
                index.norm[normalize(ent.canonical_form)].append(ent.id)
                if ent.natural_key:
                    index.nk[ent.natural_key].append(ent.id)
                for tok in set(normalize(ent.canonical_form).split()):
                    if len(tok) >= 3:
                        index.token[tok].add(ent.id)
                link(m, ent.id, "created", 0.8, {"created": True})
            else:
                report["created"] += 1  # dry-run still counts
    if write:
        await session.commit()
    return report


async def resolve_unlinked(
    document_ids: list[uuid.UUID] | None = None, *, write: bool = True
) -> list[dict[str, Any]]:
    """Resolve unlinked mentions for the given documents (or all of them)."""
    sinas = _Sinas()
    async with AsyncSessionLocal() as session:
        entities = (await session.execute(select(Entity))).scalars().all()
        index = _EntityIndex(entities)
        types = {
            t.id: t for t in (await session.execute(select(EntityType))).scalars()
        }
        if document_ids is None:
            document_ids = list((
                await session.execute(
                    select(EntityMention.document_id)
                    .where(EntityMention.entity_id.is_(None))
                    .distinct()
                )
            ).scalars().all())
    reports = []
    for did in document_ids:
        async with AsyncSessionLocal() as session:
            reports.append(
                await resolve_document(session, sinas, index, types, did, write=write)
            )
    return reports
