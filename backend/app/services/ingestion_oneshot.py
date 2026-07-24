"""One-shot ingestion: rules → gazetteer → a single tool-less LLM pass.

The staged ingestion pipeline (ingestion_runner) runs one agentic CHAT per
stage per document — classifier, summarizer, property-extractor,
entity-extractor — each re-reading the document and each paying tool-call
round-trips. This module replaces those four stages with:

  1. classify_by_rules   deterministic filename patterns — free, instant
  2. gazetteer_pass      alias string-matching against known entities —
                         free, and covers more of each document as the
                         entity table grows
  3. front_matter_oneshot ONE tool-less completion (CHEAP_LLM, temp 0,
                         forced JSON) returning class + summary +
                         properties + residual entities; the SERVER
                         writes every row. The document is read once.

Relationship extraction is intentionally NOT replaced here: it needs graph
context and stays on the existing agentic stage.

Property extraction depends on the class being known (each class has its
own property schema). When rules classify the document, properties ride in
the same single call; otherwise the class comes back from call one and a
second call extracts properties — still at most two completions per
document, with no chats and no tool loops.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import Document, DocumentClass, Entity, EntityAlias, EntityMention
from app.models.config import DocumentClassProperty, EntityType
from app.models.runtime import EntityProposal, PropertyValue
from app.services.query_runner import _Sinas

FRONT_MATTER_AGENT = "grove/front-matter-agent"
GAZETTEER_MIN_ALIAS_LEN = 4
RULE_WRITE_CONFIDENCE = 0.95  # rules at/above this write the class directly

# Filename → class rules. Patterns are matched against the bare filename.
# Confidence below RULE_WRITE_CONFIDENCE is passed to the LLM as a hint
# instead of being written directly.
CLASS_RULES: list[tuple[str, str, float, str]] = [
    (r"^\d{5}M\d+", "Regulatory Decision", 0.98, "EU merger-register filename (3xxxxMxxxx)"),
    (r"^\d{5}AT\d+", "Regulatory Decision", 0.98, "EU antitrust-register filename (5xxxxATxxxx)"),
    (r"-merger-inquiry", "Regulatory Decision", 0.97, "CMA merger-inquiry filename"),
    (r"^c\d{6}", "Regulatory Decision", 0.95, "CNMC case-number filename"),
    (r"^\d{4,6}(_\d+)?\.md$", "Bulletin article", 0.80, "numeric Bulletin-feed id"),
]


def classify_by_rules(filename: str) -> tuple[str, float, str] | None:
    for pattern, cls, conf, reason in CLASS_RULES:
        if re.search(pattern, filename):
            return cls, conf, reason
    return None


# ── gazetteer ───────────────────────────────────────────────────────────────

_WORD = re.compile(r"\w")


def _boundary_ok(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    return not _WORD.match(before) and not _WORD.match(after)


async def _load_gazetteer(session: AsyncSession) -> list[tuple[str, uuid.UUID, str]]:
    """(alias_lower, entity_id, canonical_form) for every alias and every
    canonical form long enough to match safely."""
    out: list[tuple[str, uuid.UUID, str]] = []
    ents = (await session.execute(select(Entity.id, Entity.canonical_form))).all()
    canon_by_id = {eid: cf for eid, cf in ents}
    for eid, cf in ents:
        if len(cf) >= GAZETTEER_MIN_ALIAS_LEN:
            out.append((cf.lower(), eid, cf))
    aliases = (await session.execute(select(EntityAlias.alias, EntityAlias.entity_id))).all()
    for alias, eid in aliases:
        if len(alias) >= GAZETTEER_MIN_ALIAS_LEN and eid in canon_by_id:
            out.append((alias.lower(), eid, canon_by_id[eid]))
    # longest first so overlapping shorter aliases don't shadow longer ones
    out.sort(key=lambda t: -len(t[0]))
    return out


def gazetteer_scan(
    content: str, gazetteer: list[tuple[str, uuid.UUID, str]]
) -> dict[uuid.UUID, tuple[str, int]]:
    """entity_id → (canonical_form, first_offset) for every entity whose
    alias appears in the content on a word boundary."""
    lowered = content.lower()
    found: dict[uuid.UUID, tuple[str, int]] = {}
    for alias, eid, canon in gazetteer:
        if eid in found:
            continue
        i = lowered.find(alias)
        while i != -1:
            if _boundary_ok(lowered, i, i + len(alias)):
                found[eid] = (canon, i)
                break
            i = lowered.find(alias, i + 1)
    return found


# ── one-shot LLM pass ──────────────────────────────────────────────────────

_MAX_CONTENT_CHARS = 60_000  # front-matter window (class/summary/properties)
_ENTITY_CHUNK_CHARS = 30_000  # entities are extracted exhaustively per chunk
_ENTITY_CHUNK_OVERLAP = 1_500


def _clip(content: str) -> str:
    if len(content) <= _MAX_CONTENT_CHARS:
        return content
    return content[:_MAX_CONTENT_CHARS] + "\n[... truncated for extraction ...]"


def _entity_chunks(content: str) -> list[str]:
    if len(content) <= _ENTITY_CHUNK_CHARS + _ENTITY_CHUNK_OVERLAP:
        return [content]
    return [
        content[i : i + _ENTITY_CHUNK_CHARS + _ENTITY_CHUNK_OVERLAP]
        for i in range(0, len(content), _ENTITY_CHUNK_CHARS)
    ]


_ENTITY_CHUNK_PROMPT = """Extract EVERY named entity from this document chunk. Reply with ONLY a JSON object, no prose:
{{"entities": [{{"name": "<most complete name as written>", "type": "<one of: {types}>", "confidence": <0..1>}}]}}

TYPE GUIDANCE (follow exactly):
{type_guidance}

Be EXHAUSTIVE: every named company, competition authority, court,
decision/case, legal instrument (treaty articles, acts, regulations),
jurisdiction and market in the text — including every item of long
enumerations. Do not invent names; do not stop early; no duplicates.
Skip entities from this already-recorded list: {known}

CHUNK {i}/{n} OF DOCUMENT {filename}:
{chunk}"""


def _presence_filter(name: str, content_lower: str) -> bool:
    """Deterministic hallucination guard: an extracted entity must actually
    occur in the document. Match on the full normalized name or a 3-word
    prefix (survives 'Authority (ACRONYM)' style tails)."""
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    if not words:
        return False
    for k in (len(words), 4, 3, 2):
        if k <= len(words):
            probe = " ".join(words[:k])
            if len(probe) >= 4 and probe in content_lower:
                return True
    return len(words) == 1 and words[0] in content_lower


def _front_matter_prompt(
    *,
    filename: str,
    content: str,
    classes: list[tuple[str, str]],
    entity_types: list[str],
    known_entities: list[str],
    class_hint: tuple[str, float, str] | None,
    properties: list[dict] | None,
) -> str:
    class_lines = "\n".join(f"- {n}: {d or ''}" for n, d in classes)
    type_lines = "\n".join(
        f"- {t['name']}: {t['guidance'][:220]}" for t in entity_types
    )
    known = ", ".join(sorted(known_entities)[:120]) or "(none)"
    hint = (
        f'\nFilename rule suggests class "{class_hint[0]}" ({class_hint[2]}); '
        "confirm or overrule it on the content.\n"
        if class_hint
        else ""
    )
    prop_block = ""
    if properties:
        plines = "\n".join(
            f'- "{p["name"]}": {p.get("description") or ""} (schema: {json.dumps(p["schema"])})'
            for p in properties
        )
        prop_block = (
            "\nPROPERTIES to extract for this class (null when the document "
            f"does not state a value):\n{plines}\n"
        )
    return f"""Extract the front matter of one document. Reply with ONLY a JSON object, no prose.

DOCUMENT CLASSES (pick exactly one):
{class_lines}
{hint}
ENTITY TYPES (follow each type's guidance exactly):
{type_lines}

Entities already recorded for this document (do NOT repeat them):
{known}
{prop_block}
Reply JSON schema:
{{
  "document_class": "<class name>",
  "class_confidence": <0..1>,
  "summary": "<8-12 sentence factual summary: parties, authority, dates, outcome, legal basis>",
  "properties": {{"<property name>": <value per its schema> , ...}},
  "entities": [{{"name": "<canonical name>", "type": "<entity type>", "confidence": <0..1>}}, ...]
}}

Rules: entities must be REAL named things from the document (companies,
authorities, courts, decisions/cases, legal instruments, jurisdictions,
markets) that are NOT in the already-recorded list. Use the most complete
canonical name the document gives. No duplicates. Be EXHAUSTIVE: list
every named entity, including every item of long enumerations — do not
summarize or stop early.

FILENAME: {filename}
DOCUMENT:
{_clip(content)}"""


def _parse_json_reply(reply: str) -> dict:
    cleaned = reply.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in reply: {reply[:200]}")
    return json.loads(cleaned[start : end + 1])


# ── writers ────────────────────────────────────────────────────────────────


async def _existing_mention_entity_ids(
    session: AsyncSession, document_id: uuid.UUID
) -> set[uuid.UUID]:
    return set(
        (
            await session.execute(
                select(EntityMention.entity_id).where(
                    EntityMention.document_id == document_id
                )
            )
        ).scalars()
    )


async def oneshot_ingest_document(
    session: AsyncSession,
    sinas: _Sinas,
    document_id: uuid.UUID,
    *,
    gazetteer: list[tuple[str, uuid.UUID, str]],
    classes: list[tuple[uuid.UUID, str, str]],
    entity_types: list[dict],
    write: bool = True,
) -> dict[str, Any]:
    """Run the one-shot path for a single document. Returns a report dict."""
    doc = await session.get(Document, document_id)
    from app.models import DocumentVersion  # local import to avoid cycles

    version = (
        await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version.desc())
            .limit(1)
        )
    ).scalars().first()
    if version is None or not (version.content_md or "").strip():
        return {"document": doc.filename, "skipped": "no extracted content"}
    content = version.content_md

    report: dict[str, Any] = {"document": doc.filename, "llm_calls": 0}

    # 1. rules
    rule = classify_by_rules(doc.filename or "")
    rule_written = False
    if rule and rule[1] >= RULE_WRITE_CONFIDENCE and doc.document_class_id is None:
        cls_id = next((cid for cid, n, _ in classes if n == rule[0]), None)
        if cls_id is not None:
            if write:
                doc.document_class_id = cls_id
                doc.classification_confidence = rule[1]
            rule_written = True
            report["class"] = {"name": rule[0], "confidence": rule[1], "by": "rule"}

    # 2. gazetteer
    known = gazetteer_scan(content, gazetteer)
    already = await _existing_mention_entity_ids(session, document_id)
    new_gaz = {eid: v for eid, v in known.items() if eid not in already}
    if write:
        for eid, (canon, offset) in new_gaz.items():
            session.add(
                EntityMention(
                    document_id=document_id,
                    document_version_id=version.id,
                    entity_id=eid,
                    span={"method": "gazetteer", "char_offset": offset},
                    confidence=0.97,
                )
            )
    report["gazetteer_mentions"] = {
        "matched": len(known),
        "new": len(new_gaz),
        "already_recorded": len(known) - len(new_gaz),
    }

    # 3. one-shot LLM pass (single call when the class is already known)
    # Include property schema in call one when the class is known — or
    # optimistically for the rules-hinted class (saves the second call
    # whenever the LLM agrees with the hint, which is the common case).
    known_class_id = doc.document_class_id
    if known_class_id is None and rule:
        known_class_id = next((cid for cid, n, _ in classes if n == rule[0]), None)
    class_props: list[dict] | None = None
    if known_class_id:
        rows = (
            await session.execute(
                select(DocumentClassProperty).where(
                    DocumentClassProperty.document_class_id == known_class_id,
                    DocumentClassProperty.manual.is_(False),
                )
            )
        ).scalars().all()
        class_props = [
            {"name": p.name, "description": p.description, "schema": p.schema, "id": p.id}
            for p in rows
        ]

    hint = None
    if rule and not rule_written and doc.document_class_id is None:
        hint = rule
    prompt = _front_matter_prompt(
        filename=doc.filename or "",
        content=content,
        classes=[(n, d) for _, n, d in classes],
        entity_types=entity_types,
        known_entities=[c for c, _ in known.values()],
        class_hint=hint,
        properties=class_props,
    )
    reply = await sinas.invoke(FRONT_MATTER_AGENT, prompt)
    report["llm_calls"] += 1
    data = _parse_json_reply(reply)

    # class (when rules didn't decide)
    if doc.document_class_id is None:
        cls_name = str(data.get("document_class") or "")
        cls_id = next((cid for cid, n, _ in classes if n == cls_name), None)
        if cls_id is not None and write:
            doc.document_class_id = cls_id
            doc.classification_confidence = float(data.get("class_confidence") or 0.5)
        report["class"] = {
            "name": cls_name,
            "confidence": data.get("class_confidence"),
            "by": "llm",
        }
        # a second call is needed only when the LLM picked a class we did
        # NOT already carry properties for (no hint, or hint overruled)
        hinted_matches = rule is not None and cls_name == rule[0]
        if cls_id is not None and (class_props is None or not hinted_matches):
            rows = (
                await session.execute(
                    select(DocumentClassProperty).where(
                        DocumentClassProperty.document_class_id == cls_id,
                        DocumentClassProperty.manual.is_(False),
                    )
                )
            ).scalars().all()
            class_props = [
                {"name": p.name, "description": p.description, "schema": p.schema, "id": p.id}
                for p in rows
            ]
            if class_props:
                prop_prompt = _front_matter_prompt(
                    filename=doc.filename or "",
                    content=content,
                    classes=[(n, d) for _, n, d in classes],
                    entity_types=entity_types,
                    known_entities=[c for c, _ in known.values()],
                    class_hint=(cls_name, 1.0, "already classified"),
                    properties=class_props,
                )
                reply2 = await sinas.invoke(FRONT_MATTER_AGENT, prop_prompt)
                report["llm_calls"] += 1
                data2 = _parse_json_reply(reply2)
                data["properties"] = data2.get("properties") or {}

    # summary
    if data.get("summary") and write and not (doc.summary or "").strip():
        doc.summary = str(data["summary"])[:8000]

    # properties
    written_props = 0
    if class_props and isinstance(data.get("properties"), dict):
        by_name = {p["name"]: p for p in class_props}
        existing = set(
            (
                await session.execute(
                    select(PropertyValue.property_id).where(
                        PropertyValue.document_id == document_id
                    )
                )
            ).scalars()
        )
        for name, value in data["properties"].items():
            p = by_name.get(name)
            if p is None or value is None or p["id"] in existing:
                continue
            if write:
                session.add(
                    PropertyValue(
                        property_id=p["id"],
                        document_id=document_id,
                        document_version_id=version.id,
                        value={"value": value},
                        method="auto",
                        confidence=0.8,
                        reason="front-matter one-shot",
                    )
                )
            written_props += 1
    report["properties_written"] = written_props

    # residual entities → mention (known alias) or proposal (new).
    # Long documents get exhaustive per-chunk entity extraction (the
    # front-matter call's entities only cover its clipped window), and
    # every candidate must pass the deterministic presence filter.
    content_lower = content.lower()
    all_entities: list[dict] = list(data.get("entities") or [])
    chunks = _entity_chunks(content)
    if len(chunks) > 1:
        known_names = ", ".join(sorted(c for c, _ in known.values())[:120]) or "(none)"
        chunk_prompts = [
            _ENTITY_CHUNK_PROMPT.format(
                types=", ".join(t["name"] for t in entity_types),
                type_guidance="\n".join(f"- {t['name']}: {t['guidance'][:220]}" for t in entity_types),
                known=known_names,
                i=i,
                n=len(chunks),
                filename=doc.filename or "",
                chunk=chunk,
            )
            for i, chunk in enumerate(chunks, start=1)
        ]
        replies = await asyncio.gather(
            *(sinas.invoke(FRONT_MATTER_AGENT, p) for p in chunk_prompts)
        )
        report["llm_calls"] += len(chunk_prompts)
        for r in replies:
            try:
                all_entities.extend(_parse_json_reply(r).get("entities") or [])
            except Exception:
                continue

    alias_map = {a: (eid, canon) for a, eid, canon in gazetteer}
    mentions_added = 0
    proposals_added = 0
    filtered_out = 0
    seen_names: set[str] = set()
    for ent in all_entities:
        name = str(ent.get("name") or "").strip()
        etype = str(ent.get("type") or "").strip()
        if not name or len(name) < 3 or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        if not _presence_filter(name, content_lower):
            filtered_out += 1
            continue
        hit = alias_map.get(name.lower())
        if hit:
            eid = hit[0]
            if eid not in already and eid not in new_gaz:
                if write:
                    session.add(
                        EntityMention(
                            document_id=document_id,
                            document_version_id=version.id,
                            entity_id=eid,
                            span={"method": "oneshot"},
                            confidence=float(ent.get("confidence") or 0.8),
                        )
                    )
                mentions_added += 1
            continue
        tinfo = next((t for t in entity_types if t["name"] == etype), None)
        if tinfo is None or tinfo["creation_mode"] == "closed":
            continue
        if tinfo["creation_mode"] == "open":
            # open types: the extractor may create the entity directly
            if write:
                new_ent = Entity(
                    entity_type_id=tinfo["id"], canonical_form=name[:500]
                )
                session.add(new_ent)
                await session.flush()
                session.add(
                    EntityMention(
                        document_id=document_id,
                        document_version_id=version.id,
                        entity_id=new_ent.id,
                        span={"method": "oneshot"},
                        confidence=float(ent.get("confidence") or 0.8),
                    )
                )
            mentions_added += 1
        else:  # review
            if write:
                session.add(
                    EntityProposal(
                        entity_type_id=tinfo["id"],
                        canonical_form=name[:500],
                        proposing_agent="front-matter-oneshot",
                        reasoning=f"named in {doc.filename}",
                        evidence_document_id=document_id,
                        status="pending",
                    )
                )
            proposals_added += 1
    report["entity_mentions_llm"] = mentions_added
    report["entity_proposals"] = proposals_added
    report["hallucinations_filtered"] = filtered_out
    report["entity_chunks"] = len(chunks)
    if not write:  # evaluation mode: expose the full extracted set
        report["extracted_names"] = sorted(
            {c for c, _ in known.values()}
            | {
                str(e.get("name")).strip()
                for e in all_entities
                if str(e.get("name") or "").strip().lower() in seen_names
                and _presence_filter(str(e.get("name")), content_lower)
            }
        )

    if write:
        await session.commit()
    return report


async def oneshot_ingest(
    document_ids: list[uuid.UUID], *, write: bool = True, concurrency: int = 4
) -> list[dict[str, Any]]:
    """Drive the one-shot path over many documents."""
    sinas = _Sinas()
    async with AsyncSessionLocal() as session:
        gazetteer = await _load_gazetteer(session)
        classes = [
            (c.id, c.name, c.description or "")
            for c in (await session.execute(select(DocumentClass))).scalars()
        ]
        entity_types = [
            {
                "id": t.id,
                "name": t.name,
                "guidance": (t.guidance or t.description or "").strip(),
                "creation_mode": t.creation_mode,
            }
            for t in (await session.execute(select(EntityType))).scalars()
        ]

    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []

    async def one(doc_id: uuid.UUID) -> None:
        async with sem:
            async with AsyncSessionLocal() as session:
                try:
                    r = await oneshot_ingest_document(
                        session,
                        sinas,
                        doc_id,
                        gazetteer=gazetteer,
                        classes=classes,
                        entity_types=entity_types,
                        write=write,
                    )
                except Exception as exc:  # per-doc isolation
                    r = {"document": str(doc_id), "error": str(exc)[:300]}
                results.append(r)

    await asyncio.gather(*(one(d) for d in document_ids))
    return results
