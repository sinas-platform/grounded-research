"""LLM adjudication for queued citation edges the keys could not resolve.

The deterministic ladder (natural key, alias, containment) has already
taken everything it can. What is left are citations whose key names an
entity ambiguously or in words: "the 2018 Nexans judgment", "Regulation
1/2003 as amended". A cheap model call, given the citing quote and a short
list of plausible candidates, settles most of them.

Scope is deliberately narrow: the model only ever picks among EXISTING
candidate entities or answers "none". It cannot mint — a cited case that is
not in the graph stays queued rather than becoming a name-only stub, so the
LLM cannot pollute the entity set, only connect it. Every confirmed pick
teaches the key as an alias, so the deterministic ladder absorbs the
pattern and this pass shrinks over time.

    python -m app.services.citation_adjudicate --dry-run
    python -m app.services.citation_adjudicate --defs cites,appealed_in
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import Relationship, RelationshipDefinition, UnresolvedRelationship
from app.services.entity_keys import KeyIndex, key_norm, learn_aliases

_log = logging.getLogger("sgr.citation_adjudicate")

_AGENT = "sgr/entity-resolution-agent"
_BATCH = 15
_DEFAULT_DEFS = ["cites", "book_cites_decision", "article_review_cites_decision",
                 "appealed_in", "cites_legal_instrument"]


def _candidates(index: KeyIndex, key: str, type_id, limit: int = 6) -> list[tuple]:
    """Plausible existing entities for this key: token overlap over names,
    within the definition's target type."""
    toks = {t for t in key_norm(key).split() if t} or {key_norm(key)}
    kn = key_norm(key)
    scored = []
    for name, eid, tid in index.names:
        if tid != type_id or not name:
            continue
        score = sum(1 for t in toks if t in name)
        if kn and kn in name:
            score += 2
        if score:
            scored.append((score, eid, name))
    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


async def adjudicate(defs: list[str] | None = None, *,
                     write: bool = True, limit: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    report = {"considered": 0, "linked": 0, "none": 0, "no_candidates": 0,
              "unparseable": 0, "batches": 0}
    async with AsyncSessionLocal() as session:
        index = await KeyIndex.load(session)
        wanted = (await session.execute(
            select(RelationshipDefinition)
            .where(RelationshipDefinition.name.in_(defs or _DEFAULT_DEFS)))).scalars().all()
        by_id = {d.id: d for d in wanted}
        rows = list((await session.execute(
            select(UnresolvedRelationship)
            .where(UnresolvedRelationship.status == "unresolved")
            .where(UnresolvedRelationship.relationship_definition_id.in_(by_id))
        )).scalars())
        if limit:
            rows = rows[:limit]

        existing = set((await session.execute(
            select(Relationship.relationship_definition_id,
                   Relationship.source_id, Relationship.target_id))).all())

        # rows with at least one plausible candidate, batched for the model
        work = []
        for r in rows:
            d = by_id[r.relationship_definition_id]
            if d.target_ref_type != "entity_type":
                continue
            cands = _candidates(index, r.target_key, d.target_ref_id)
            report["considered"] += 1
            if not cands:
                report["no_candidates"] += 1
                continue
            work.append((r, cands))

        async with httpx.AsyncClient(timeout=180.0) as client:
            for i in range(0, len(work), _BATCH):
                batch = work[i:i + _BATCH]
                items = "\n".join(
                    f'{j}. reference: "{r.target_key}"'
                    + (f'\n   cited as: "{(r.reasoning or "")[:200]}"' if r.reasoning else "")
                    + "\n   candidates:\n" + "\n".join(
                        f"      {k}) {name[:150]}" for k, (_s, _e, name) in enumerate(cands))
                    for j, (r, cands) in enumerate(batch))
                prompt = (
                    "For each numbered reference below, decide which candidate "
                    "entity it refers to, or none. A reference matches a "
                    "candidate only if they plainly denote the SAME case, "
                    "judgment or instrument — same proceeding, not merely the "
                    "same parties or subject. When unsure, answer none: a "
                    "wrong link is worse than no link.\n\n" + items
                    + '\n\nReply ONLY JSON: {"choices": [{"i": <item number>, '
                    '"pick": <candidate letter-index as integer, or null>}]}'
                )
                try:
                    resp = await client.post(
                        f"{settings.sinas_url}/agents/{_AGENT}/invoke",
                        headers={"Authorization": f"Bearer {settings.sinas_api_key}"},
                        json={"message": prompt})
                    resp.raise_for_status()
                    reply = (resp.json().get("reply") or "").strip()
                    cleaned = reply.strip("`").removeprefix("json").strip()
                    data = json.loads(cleaned[cleaned.find("{"):cleaned.rfind("}") + 1])
                except Exception:  # noqa: BLE001 — a bad batch is skipped, not fatal
                    report["unparseable"] += 1
                    continue
                report["batches"] += 1
                for c in data.get("choices") or []:
                    try:
                        r, cands = batch[int(c["i"])]
                        pick = c.get("pick")
                    except (KeyError, ValueError, IndexError, TypeError):
                        continue
                    if pick is None or not str(pick).lstrip("-").isdigit() \
                            or not (0 <= int(pick) < len(cands)):
                        report["none"] += 1
                        continue
                    _score, eid, _name = cands[int(pick)]
                    edge = (r.relationship_definition_id, r.source_id, eid)
                    if write:
                        if edge not in existing:
                            existing.add(edge)
                            rel = Relationship(
                                relationship_definition_id=r.relationship_definition_id,
                                source_id=r.source_id, target_id=eid,
                                evidence_document_id=r.evidence_document_id,
                                evidence_span=r.evidence_span,
                                confidence=r.confidence,
                                notes=f"adjudicated from queue key '{r.target_key}'")
                            session.add(rel)
                            await session.flush()
                            r.resolved_relationship_id = rel.id
                        r.status = "resolved"
                        r.resolved_at = datetime.now(timezone.utc)
                        await learn_aliases(session, eid, [r.target_key])
                    index.learn(eid, r.target_key)
                    report["linked"] += 1
                if write:
                    await session.commit()
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--defs", type=str)
    args = ap.parse_args()
    out = asyncio.run(adjudicate(
        args.defs.split(",") if args.defs else None,
        write=not args.dry_run, limit=args.limit))
    print(json.dumps(out, indent=1))
