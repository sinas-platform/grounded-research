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
from app.models import (
    Document,
    Entity,
    EntityAlias,
    EntityMention,
    UnresolvedEntityMention,
)
from app.models.config import EntityType
from app.models.runtime import DocumentVersion, EntityProposal
from app.services.query_runner import _Sinas

RESOLVER_AGENT = "sgr/entity-resolution-agent"
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
            .where(EntityMention.status == "active")
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

    learned_aliases: set[tuple[uuid.UUID, str]] = set()

    def link(m: EntityMention, eid: uuid.UUID, method: str, conf: float, ev: dict):
        m.entity_id = eid
        m.link_method = method
        m.link_confidence = conf
        m.link_evidence = ev
        report[method if method in report else "adjudicated"] += 1
        # A surface that needed judgment to resolve is a spelling of this
        # entity we did not know. Record it, so the next document matches it
        # for free instead of paying for the same call — or, worse, creating
        # a second entity. Aliases were previously written only by merges,
        # which is why 4.1M links ran through the gazetteer and only 19,876
        # through an alias.
        if method in ("adjudicated", "alias") and m.surface_form:
            surf = normalize(m.surface_form)
            ent = index.by_id.get(eid)
            if surf and (ent is None or normalize(ent.canonical_form) != surf):
                learned_aliases.add((eid, m.surface_form[:500]))

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

    # T3 — batched adjudication, chunked. List-type documents (anti-dumping
    # regulations enumerating thousands of exporters) produced single
    # prompts of 3,510 mentions / 3.5MB — over Gemini's 1,048,576-token
    # input cap (three 400s on 15-16 Aug). 200 mentions ≈ 300KB stays far
    # under the cap while keeping call count low for normal documents.
    _JUDGE_CHUNK = 200
    for start in range(0, len(needs_judge), _JUDGE_CHUNK):
        chunk_judge = needs_judge[start:start + _JUDGE_CHUNK]
        blocks = []
        for i, (m, cands) in enumerate(chunk_judge, start=1):
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
        for i, (m, cands) in enumerate(chunk_judge, start=1):
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
    await _t4_create(session, index, types, document_id, needs_creation,
                     link, report, write)
    if write and learned_aliases:
        existing = set()
        ids = {eid for eid, _ in learned_aliases}
        if ids:
            existing = {
                (a.entity_id, normalize(a.alias))
                for a in (await session.execute(
                    select(EntityAlias).where(EntityAlias.entity_id.in_(ids))
                )).scalars()
            }
        for eid, surf in learned_aliases:
            if (eid, normalize(surf)) in existing:
                continue
            session.add(EntityAlias(entity_id=eid, alias=surf))
            report["aliases_learned"] = report.get("aliases_learned", 0) + 1
    if write:
        await session.commit()
    return report


async def _t4_create(session, index, types, document_id, needs_creation,
                     link, report, write):
    """T4 — shared by the interactive path and the batched apply phase."""
    for m in needs_creation:
        t = types.get(m.entity_type_id)
        if t is None:
            report["left_unlinked"] += 1
            continue
        if t.creation_mode == "closed":
            # A closed type cannot grow on its own, but the mention is still
            # evidence the corpus mentions something we do not model. File it
            # for review rather than dropping it: the reviewer can match it to
            # an existing body, promote it, or dismiss it. Without this the
            # residue of a locked type is invisible — an unlinked mention and
            # nothing in any queue.
            if write:
                session.add(UnresolvedEntityMention(
                    entity_type_id=t.id,
                    mention_text=(m.surface_form or "")[:500],
                    document_id=document_id,
                    document_version_id=getattr(m, "document_version_id", None),
                    span=m.span or {},
                    confidence=m.confidence if hasattr(m, "confidence") else None,
                    proposing_agent="entity-resolver",
                    reasoning="no candidate matched in a closed type",
                    status="unresolved",
                ))
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
                norm = normalize(m.surface_form or "")
                # An entity created concurrently in another session is not in
                # our index; the unique index on (type, normalized_form) is
                # the only thing that can see it, so consult the database
                # before inserting rather than trusting the snapshot.
                # only for entities with no natural key: those are the ones
                # the database could not protect, and the keyed path above
                # already handles the rest without a wasted insert
                if norm and not nk:
                    # in-document twin: the index is updated on every create,
                    # so a second spelling in the same document finds the
                    # first here — the same guard the keyed path has above
                    seen = [e for e in index.norm.get(norm, [])
                            if index.by_id[e].entity_type_id == t.id]
                    if seen:
                        link(m, seen[0], "exact", 0.9,
                             {"matched": index.by_id[seen[0]].canonical_form,
                              "linked_instead_of_created": True})
                        continue
                    twin = (await session.execute(
                        select(Entity).where(
                            Entity.entity_type_id == t.id,
                            Entity.normalized_form == norm,
                            Entity.merged_into_id.is_(None),
                        ).limit(1)
                    )).scalars().first()
                    if twin is not None:
                        link(m, twin.id, "exact", 0.9,
                             {"matched": twin.canonical_form,
                              "linked_instead_of_created": True})
                        continue
                ent = Entity(
                    entity_type_id=t.id,
                    canonical_form=(m.surface_form or "")[:500],
                    natural_key=nk,
                    normalized_form=norm or None,
                )
                try:
                    # savepoint: a plain failed flush would roll back
                    # every link already made for this document
                    async with session.begin_nested():
                        session.add(ent)
                        await session.flush()
                except IntegrityError:
                    # cross-session race: an owner appeared after our index
                    # was built. Either unique index can be the one that
                    # fired — natural_key for a keyed entity, normalized_form
                    # for a name — so recover on whichever identity we have.
                    stmt = select(Entity).where(
                        Entity.entity_type_id == t.id,
                        Entity.merged_into_id.is_(None),
                    )
                    stmt = (stmt.where(Entity.natural_key == nk) if nk
                            else stmt.where(Entity.normalized_form == norm))
                    owner = (await session.execute(stmt)).scalars().first()
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


# ── two-phase (batched) resolution ──────────────────────────────────────────
# collect: run T1/T2 deterministically (committed), emit T3 adjudication
# prompts as DATA — [{prompt, bindings: [[mention_id, [candidate_ids]]]}] —
# so a provider batch can answer them at half price and without interactive
# latency. apply: link verdicts onto the captured bindings and run T4.
# The candidate lists freeze at collect time by design (16 Aug decision).

async def resolve_collect(
    session: AsyncSession, index: "_EntityIndex",
    types: dict[uuid.UUID, EntityType], document_id: uuid.UUID,
) -> dict[str, Any]:
    doc = await session.get(Document, document_id)
    mentions = (
        await session.execute(
            select(EntityMention)
            .where(EntityMention.document_id == document_id)
            .where(EntityMention.entity_id.is_(None))
            .where(EntityMention.status == "active")
        )
    ).scalars().all()
    out: dict[str, Any] = {"chunks": [], "creation": []}
    if not mentions:
        return out
    content = ""
    if doc and doc.current_version_id:
        content = (
            await session.execute(
                select(DocumentVersion.content_md).where(
                    DocumentVersion.id == doc.current_version_id
                )
            )
        ).scalar_one_or_none() or ""

    report = {"natural_key": 0, "exact": 0, "adjudicated": 0}

    def link(m, eid, method, conf, ev):
        m.entity_id = eid
        m.link_method = method
        m.link_confidence = conf
        m.link_evidence = ev

    needs_judge: list[tuple[EntityMention, list[Entity]]] = []
    for m in mentions:
        surface = m.surface_form or ""
        nk = natural_key(surface)
        if nk and len(index.nk.get(nk, [])) == 1:
            link(m, index.nk[nk][0], "natural_key", 0.98, {"key": nk})
            continue
        hits = index.norm.get(normalize(surface), [])
        if not hits:
            ps = party_set(surface)
            hits = index.party.get(ps, []) if ps else []
        if len(hits) == 1:
            ent = index.by_id[hits[0]]
            if m.entity_type_id is None or ent.entity_type_id == m.entity_type_id:
                link(m, ent.id, "exact", 0.9, {"matched": ent.canonical_form})
                continue
        cands = ([index.by_id[h] for h in hits] or
                 index.candidates(surface, m.entity_type_id))
        if m.entity_type_id is not None:
            cands = [c for c in cands if c.entity_type_id == m.entity_type_id]
        if cands:
            needs_judge.append((m, cands))
        else:
            out["creation"].append(str(m.id))
    await session.commit()  # T1/T2 links are final regardless of T3 outcome

    _JUDGE_CHUNK = 200
    for start in range(0, len(needs_judge), _JUDGE_CHUNK):
        chunk_judge = needs_judge[start:start + _JUDGE_CHUNK]
        blocks = []
        bindings: list[list] = []
        for i, (m, cands) in enumerate(chunk_judge, start=1):
            tname = types[m.entity_type_id].name if m.entity_type_id in types else "?"
            lines = [f'MENTION {i}: "{m.surface_form}" (type: {tname})',
                     f'CONTEXT: …{_slice(content, m.span)}…', "CANDIDATES:"]
            for letter, c in zip("ABCDEFGH", cands):
                ctype = types[c.entity_type_id].name if c.entity_type_id in types else "?"
                lines.append(f"  {letter}. {c.canonical_form} (type: {ctype})")
            blocks.append("\n".join(lines))
            bindings.append([str(m.id), [str(c.id) for c in cands[:8]]])
        out["chunks"].append({
            "prompt": _ADJUDICATE_PROMPT.format(mentions="\n\n".join(blocks)),
            "bindings": bindings,
        })
    return out


async def resolve_apply(
    session: AsyncSession, index: "_EntityIndex",
    types: dict[uuid.UUID, EntityType], document_id: uuid.UUID,
    chunks: list[dict], replies: list[str | None],
    creation_ids: list[str], *, write: bool = True,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "document": str(document_id),
        "natural_key": 0, "exact": 0, "adjudicated": 0,
        "created": 0, "proposed": 0, "left_unlinked": 0,
    }

    def link(m, eid, method, conf, ev):
        m.entity_id = eid
        m.link_method = method
        m.link_confidence = conf
        m.link_evidence = ev
        report[method if method in report else "adjudicated"] += 1

    async def fetch(mid: str) -> EntityMention | None:
        m = await session.get(EntityMention, uuid.UUID(mid))
        # only touch mentions still unlinked and active
        if m is None or m.entity_id is not None or m.status != "active":
            return None
        return m

    needs_creation: list[EntityMention] = []
    for chunk, reply in zip(chunks, replies):
        verdicts: dict[int, dict] = {}
        if reply:
            try:
                cleaned = reply.strip().strip("`").removeprefix("json").strip()
                data = json.loads(cleaned[cleaned.find("{"): cleaned.rfind("}") + 1])
                for v in data.get("links") or []:
                    verdicts[int(v.get("mention") or 0)] = v
            except Exception:  # noqa: BLE001 — unparseable → creation path
                pass
        for i, (mid, cand_ids) in enumerate(chunk["bindings"], start=1):
            m = await fetch(mid)
            if m is None:
                continue
            v = verdicts.get(i) or {}
            choice = v.get("choice")
            conf = float(v.get("confidence") or 0.0)
            if choice and conf >= 0.6 and str(choice).strip().upper()[:1] in "ABCDEFGH":
                idx = "ABCDEFGH".index(str(choice).strip().upper()[0])
                if idx < len(cand_ids):
                    eid = uuid.UUID(cand_ids[idx])
                    if eid in index.by_id:
                        link(m, eid, "adjudicated", conf,
                             {"reason": str(v.get("reason") or "")[:300]})
                        continue
            needs_creation.append(m)
    for mid in creation_ids:
        m = await fetch(mid)
        if m is not None:
            needs_creation.append(m)

    await _t4_create(session, index, types, document_id, needs_creation,
                     link, report, write)
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
                    .where(EntityMention.status == "active")
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
