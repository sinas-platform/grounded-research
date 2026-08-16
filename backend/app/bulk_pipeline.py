"""Bulk ingestion pipeline — batch ETL, not service behavior.

The weekend of 15-16 Aug proved that driving bulk corpus loads through the
API process's event loop breaks in compounding ways (pool exhaustion, loop
starvation, persist stampedes, serial reply fetching). This module is the
batch-shaped alternative, built from the pieces that never failed:

  round 1  build front-matter prompts, submit provider batches (<=400)
  round 2  fetch replies (bounded-concurrent), derive props/chunk prompts
           from round-1 branching, submit those
  persist  replay all stored replies through the NORMAL oneshot pipeline
           in bounded groups (the recover_from_chats pattern: groups of
           25, concurrency 8 — zero failures across 584 docs this weekend)
  resolve  ground + resolve in bounded groups (direct function calls)
  rels     build relationship prompts from resolved mentions, batch,
           persist via the standard extractor with stored replies

Every stage derives its worklist FROM THE DATA (extraction needed == no
summary; relationships needed == no coverage), so any stage is resumable
and re-runnable by construction. Batch ids are checkpointed to the job dir
before polling so a crashed job resumes by polling, never by resubmitting.

Run standalone (never inside uvicorn):
    cd backend && ../.venv/bin/python -m app.bulk_pipeline \
        --ids-file /tmp/ids.txt --stages extract,resolve,relationships \
        --job-dir ~/grove-bulk-jobs/p5-probe

The API trigger (app/api/v1/bulk.py) spawns exactly this as a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path

import httpx
from sqlalchemy import select

log = logging.getLogger("bulk")

SUBMIT_MAX = 400          # inputs per Sinas batch POST (~15MB body ceiling)
FETCH_CONCURRENCY = int(os.environ.get("BULK_FETCH_CONCURRENCY", "16"))    # parallel chat-reply fetches (serial fetch cost
                          # ~1h on a 2,854-reply wave, 16 Aug)
PERSIST_GROUP = 25        # docs per persist group
PERSIST_CONCURRENCY = int(os.environ.get("BULK_PERSIST_CONCURRENCY", "8"))   # concurrent doc pipelines inside a group
MIDDLE_GROUP = 50         # docs per ground/resolve group
MIDDLE_CONCURRENCY = int(os.environ.get("BULK_MIDDLE_CONCURRENCY", "8"))
POLL_SECONDS = 20

CHUNK_RE = re.compile(r"CHUNK (\d+)/(\d+) OF DOCUMENT ([^\n:]+):")
FILENAME_RE = re.compile(r"^FILENAME: (.+)$", re.M)

# ── process-pool gazetteer scanning ─────────────────────────────────────────
# Prompt prep is CPU-bound on gazetteer_scan (~250ms/doc at 63K entries) and
# the GIL makes threads useless for it. A small process pool with the
# gazetteer loaded once per worker turns a 35-minute prep (8K docs) into ~7.
_SCAN_WORKERS = int(os.environ.get("BULK_SCAN_WORKERS", "4"))
_worker_gazetteer = None


def _pool_init(gazetteer):
    global _worker_gazetteer
    _worker_gazetteer = gazetteer


def _pool_scan(content: str):
    from app.services.ingestion_oneshot import gazetteer_scan

    return gazetteer_scan(content, _worker_gazetteer)


async def scan_many(contents: list[str], gazetteer) -> list[dict]:
    """Gazetteer-scan many documents on a process pool. Falls back to
    in-process scanning if the pool can't start (e.g. spawn issues)."""
    import concurrent.futures as cf

    from app.services.ingestion_oneshot import gazetteer_scan

    if len(contents) < 50:
        return [gazetteer_scan(c, gazetteer) for c in contents]
    try:
        loop = asyncio.get_event_loop()
        with cf.ProcessPoolExecutor(
            max_workers=_SCAN_WORKERS,
            initializer=_pool_init, initargs=(gazetteer,),
        ) as pool:
            futs = [loop.run_in_executor(pool, _pool_scan, c)
                    for c in contents]
            return list(await asyncio.gather(*futs))
    except Exception as exc:  # noqa: BLE001 — never lose a job to the pool
        log.warning("scan pool failed (%s); falling back in-process",
                    str(exc)[:120])
        return [gazetteer_scan(c, gazetteer) for c in contents]


# ── stored-reply client (the proven fake) ───────────────────────────────────
class StoredReplyClient:
    """Serves batch replies keyed by the prompt's own markers. Non-chunk
    prompts are served in call order per filename."""

    def __init__(self, front: dict[str, list[str]], chunks: dict):
        self.front = {k: list(v) for k, v in front.items()}
        self.chunks = chunks
        self.misses: list[str] = []

    async def invoke(self, agent: str, message: str) -> str:
        m = CHUNK_RE.search(message)
        if m:
            key = (m.group(3).strip(), int(m.group(1)))
            reply = self.chunks.get(key)
            if reply is None:
                self.misses.append(f"chunk {key}")
                raise RuntimeError(f"no stored reply for chunk {key}")
            return reply
        fm = FILENAME_RE.search(message)
        fn = fm.group(1).strip() if fm else None
        queue = self.front.get(fn)
        if not queue:
            self.misses.append(f"front {fn}")
            raise RuntimeError(f"no stored reply left for {fn}")
        return queue.pop(0)


# ── sinas batch client (bounded, checkpointed, retrying) ────────────────────
class BatchClient:
    def __init__(self, job_dir: Path):
        from app.config import get_settings

        s = get_settings()
        self.base = s.sinas_url
        self.headers = {"Authorization": f"Bearer {s.sinas_api_key}"}
        self.job_dir = job_dir
        self.state_path = job_dir / "batches.json"
        self.state: dict = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists() else {}
        )

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1))

    async def run_round(self, round_key: str, agent: str,
                        prompts: list[str]) -> list[str | None]:
        """Submit prompts (resuming prior submissions), await completion,
        return replies aligned with the prompt list."""
        rd = self.state.setdefault(round_key, {"chunks": []})
        # submit missing chunks
        for start in range(0, len(prompts), SUBMIT_MAX):
            idx = start // SUBMIT_MAX
            if idx < len(rd["chunks"]) and rd["chunks"][idx].get("batch_id"):
                continue  # resumed: already submitted
            chunk = prompts[start:start + SUBMIT_MAX]
            for attempt in (1, 2, 3, 4, 5, 6):
                try:
                    async with httpx.AsyncClient(timeout=180.0) as c:
                        r = await c.post(
                            f"{self.base}/agents/{agent}/chats/batch",
                            headers=self.headers,
                            json={"inputs": [{"message": p} for p in chunk],
                                  "execution_mode": "provider",
                                  "trigger_id_prefix": f"grove-bulk-{round_key}"},
                        )
                    if r.is_error:
                        raise RuntimeError(
                            f"submit rejected ({r.status_code}): {r.text[:200]}")
                    sub = r.json()
                    while len(rd["chunks"]) <= idx:
                        rd["chunks"].append({})
                    rd["chunks"][idx] = {"batch_id": sub["batch_id"],
                                         "chat_ids": sub["chat_ids"],
                                         "n": len(chunk)}
                    self._save()
                    log.info("%s: submitted chunk %d (%d prompts, batch %s)",
                             round_key, idx, len(chunk), sub["batch_id"])
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 6:
                        raise
                    # rate-limit windows (Google 429 on file uploads,
                    # 16 Aug 20:30) need minutes, not seconds
                    delay = min(60 * attempt, 300)
                    log.warning("%s submit attempt %d failed: %s — retry %ds",
                                round_key, attempt, str(exc)[:150], delay)
                    await asyncio.sleep(delay)
        # await all batches
        for ch in rd["chunks"]:
            while True:
                try:
                    async with httpx.AsyncClient(timeout=60.0) as c:
                        r = await c.get(f"{self.base}/batches/{ch['batch_id']}",
                                        headers=self.headers)
                        r.raise_for_status()
                        st = r.json()
                    if (st["completed"] + st["failed"] + st["cancelled"]
                            >= st["total"]) or st.get("status") in (
                            "completed", "failed", "cancelled"):
                        break
                except Exception as exc:  # noqa: BLE001 — poll must not kill
                    log.warning("poll %s failed (%s); retrying",
                                ch["batch_id"], str(exc)[:120])
                await asyncio.sleep(POLL_SECONDS)
        # fetch replies, bounded-concurrent
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        replies: list[str | None] = [None] * len(prompts)

        async def fetch(i_global: int, chat_id: str) -> None:
            async with sem:
                for attempt in (1, 2, 3, 4, 5, 6):
                    try:
                        async with httpx.AsyncClient(timeout=60.0) as c:
                            r = await c.get(f"{self.base}/chats/{chat_id}",
                                            headers=self.headers)
                            r.raise_for_status()
                            msgs = r.json().get("messages") or []
                        replies[i_global] = next(
                            (m.get("content") or "" for m in reversed(msgs)
                             if m.get("role") == "assistant"), None)
                        return
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 3:
                            log.warning("fetch %s failed: %s", chat_id,
                                        str(exc)[:120])
                            return
                        await asyncio.sleep(5)

        tasks = []
        for ci, ch in enumerate(rd["chunks"]):
            base = ci * SUBMIT_MAX
            for j, chat_id in enumerate(ch["chat_ids"]):
                tasks.append(fetch(base + j, chat_id))
        await asyncio.gather(*tasks)
        got = sum(1 for x in replies if x)
        log.info("%s: %d/%d replies retrieved", round_key, got, len(prompts))
        return replies


# ── stages ──────────────────────────────────────────────────────────────────
async def _load_shared(session):
    from app.models import DocumentClass, EntityType
    from app.services.ingestion_oneshot import _load_gazetteer

    gazetteer = await _load_gazetteer(session)
    classes = [(c.id, c.name, c.description or "") for c in
               (await session.execute(select(DocumentClass))).scalars()]
    entity_types = [
        {"id": t.id, "name": t.name,
         "guidance": (t.guidance or t.description or "").strip(),
         "creation_mode": t.creation_mode}
        for t in (await session.execute(select(EntityType))).scalars()]
    return gazetteer, classes, entity_types


async def stage_extract(doc_ids: list[uuid.UUID], job_dir: Path) -> dict:
    """Round 1 (front prompts) -> round 2 (props+chunk prompts derived from
    round-1 replies) -> bounded replay-persist through the normal pipeline."""
    from app.db import AsyncSessionLocal
    from app.models import Document, DocumentVersion
    from app.services import ingestion_oneshot as one
    from app.services.ingestion_runner import _wipe_extracted_artifacts

    client = BatchClient(job_dir)
    agent = one.DOC_METADATA_AGENT

    # worklist from data
    async with AsyncSessionLocal() as session:
        gazetteer, classes, entity_types = await _load_shared(session)
        work: list[tuple[uuid.UUID, str, str]] = []  # (id, filename, content)
        for did in doc_ids:
            doc = await session.get(Document, did)
            if doc is None or (doc.summary or "").strip():
                continue
            version = (await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == did)
                .order_by(DocumentVersion.version.desc()).limit(1)
            )).scalars().first()
            if version is None or not (version.content_md or "").strip():
                continue
            work.append((did, doc.filename or "", version.content_md))
    log.info("extract: %d docs need extraction", len(work))
    if not work:
        return {"extracted": 0, "skipped": len(doc_ids)}

    log.info("scanning %d docs on %d-process pool", len(work), _SCAN_WORKERS)
    scans = await scan_many([c for _, _, c in work], gazetteer)
    known_by_idx = {i: sc for i, sc in enumerate(scans)}

    # round 1: front-matter prompts (rule-hinted, same construction as
    # the live pipeline; class-props preloaded for rule hints)
    front_prompts: list[str] = []
    async with AsyncSessionLocal() as session:
        from app.models import DocumentClassProperty

        props_by_class: dict = {}
        for cid, _, _ in classes:
            rows = (await session.execute(
                select(DocumentClassProperty).where(
                    DocumentClassProperty.document_class_id == cid,
                    DocumentClassProperty.manual.is_(False)))).scalars().all()
            props_by_class[cid] = [
                {"name": p.name, "description": p.description,
                 "schema": p.schema, "id": p.id} for p in rows]
        for wi, (did, fn, content) in enumerate(work):
            rule = one.classify_by_rules(fn)
            hint = rule
            class_props = None
            if rule is not None:
                cid = next((c for c, n, _ in classes if n == rule[0]), None)
                if cid is not None:
                    class_props = props_by_class.get(cid) or None
            known = known_by_idx[wi]
            front_prompts.append(one._front_matter_prompt(
                filename=fn, content=content,
                classes=[(n, d) for _, n, d in classes],
                entity_types=entity_types,
                known_entities=[c for c, _ in known.values()],
                class_hint=hint, properties=class_props))
    r1 = await client.run_round("extract-front", agent, front_prompts)

    # round 1.5: props follow-up prompts. Docs WITHOUT a filename-rule hint
    # get classified by round 1's reply; the live pipeline then makes a
    # second call for that class's properties. Mirror that branching here
    # or the replay starves on its second front call (smoke test, 16 Aug).
    props_prompts: list[str] = []
    props_owners: list[str] = []
    async with AsyncSessionLocal() as session:
        for wi, ((did, fn, content), reply) in enumerate(zip(work, r1)):
            if not reply:
                continue
            rule = one.classify_by_rules(fn)
            try:
                data = one._parse_json_reply(reply)
            except Exception:  # noqa: BLE001 — replay will surface it per-doc
                continue
            cls_name = str(data.get("document_class") or "")
            cid = next((c for c, n, _ in classes if n == cls_name), None)
            hinted_matches = rule is not None and cls_name == rule[0]
            if cid is None or hinted_matches:
                continue
            cprops = props_by_class.get(cid) or []
            if not cprops:
                continue
            known = known_by_idx[wi]
            props_prompts.append(one._front_matter_prompt(
                filename=fn, content=content,
                classes=[(n, d) for _, n, d in classes],
                entity_types=entity_types,
                known_entities=[c for c, _ in known.values()],
                class_hint=(cls_name, 1.0, "already classified"),
                properties=cprops))
            props_owners.append(fn)
    r15 = (await client.run_round("extract-props", agent, props_prompts)
           if props_prompts else [])

    # round 2: chunk prompts for long docs
    chunk_prompts: list[str] = []
    chunk_keys: list[tuple[str, int]] = []
    for wi, ((did, fn, content), reply) in enumerate(zip(work, r1)):
        if not reply:
            continue
        chunks = one._entity_chunks(content)
        if len(chunks) <= 1:
            continue
        known = known_by_idx[wi]
        known_names = ", ".join(sorted(c for c, _ in known.values())[:120]) or "(none)"
        for i, chunk in enumerate(chunks, start=1):
            chunk_prompts.append(one._ENTITY_CHUNK_PROMPT.format(
                types=", ".join(t["name"] for t in entity_types),
                type_guidance="\n".join(
                    f"- {t['name']}: {t['guidance']}" for t in entity_types),
                known=known_names, i=i, n=len(chunks), filename=fn,
                chunk=chunk))
            chunk_keys.append((fn, i))
    r2 = (await client.run_round("extract-chunks", agent, chunk_prompts)
          if chunk_prompts else [])

    # persist via the normal pipeline with stored replies, bounded groups
    front_map: dict[str, list[str]] = defaultdict(list)
    for (did, fn, _), reply in zip(work, r1):
        if reply:
            front_map[fn].append(reply)
    for fn, reply in zip(props_owners, r15):
        if reply:
            front_map[fn].append(reply)  # served second, after the front reply
    chunk_map = {k: v for k, v in zip(chunk_keys, r2) if v}

    ok = 0
    failed: dict[str, str] = {}
    for start in range(0, len(work), PERSIST_GROUP):
        group = work[start:start + PERSIST_GROUP]
        for did, _, _ in group:
            async with AsyncSessionLocal() as session:
                await _wipe_extracted_artifacts(session, did)
                await session.commit()
        replay = StoredReplyClient(front_map, chunk_map)
        reports = await one.oneshot_ingest(
            [did for did, _, _ in group], write=True,
            concurrency=PERSIST_CONCURRENCY, sinas=replay)
        for (did, fn, _), rep in zip(group, reports):
            if rep.get("error"):
                failed[fn] = str(rep["error"])[:200]
            else:
                ok += 1
        log.info("extract-persist: %d/%d (ok %d, failed %d)",
                 min(start + PERSIST_GROUP, len(work)), len(work), ok,
                 len(failed))
    return {"extracted": ok, "failed": failed}


async def stage_resolve(doc_ids: list[uuid.UUID], job_dir: Path) -> dict:
    """Batched middle: grounding round then adjudication round, both via
    provider batches (half price, no interactive latency — the 2-min/call
    interactive path measured 16 Aug made this stage a 7-18h wall)."""
    from sqlalchemy import select as _select

    from app.db import AsyncSessionLocal
    from app.models import Entity, EntityType
    from app.services import entity_resolver as er
    from app.services import grounding_gate as gg

    client = BatchClient(job_dir)
    errors: dict[str, str] = {}

    # ── grounding round ──
    g_items: list[tuple[uuid.UUID, dict]] = []
    for did in doc_ids:
        try:
            async with AsyncSessionLocal() as session:
                col = await gg.ground_collect(session, did)
            if col["prompt"]:
                g_items.append((did, col))
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"ground-collect: {str(exc)[:150]}"
    log.info("middle: %d/%d docs need grounding", len(g_items), len(doc_ids))
    g_replies = await client.run_round(
        "ground", gg.GROUNDING_AGENT, [c["prompt"] for _, c in g_items]
    ) if g_items else []
    g_done = 0
    for (did, col), reply in zip(g_items, g_replies):
        try:
            async with AsyncSessionLocal() as session:
                await gg.ground_apply(session, col["mention_ids"],
                                      col.get("surfaces") or [], reply,
                                      write=True)
            g_done += 1
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"ground-apply: {str(exc)[:150]}"
    log.info("middle: grounding applied %d/%d", g_done, len(g_items))

    # ── adjudication round (post-grounding mention state) ──
    async with AsyncSessionLocal() as session:
        entities = (await session.execute(_select(Entity))).scalars().all()
        index = er._EntityIndex(entities)
        types = {t.id: t for t in
                 (await session.execute(_select(EntityType))).scalars()}

    r_items: list[tuple[uuid.UUID, dict]] = []
    for did in doc_ids:
        try:
            async with AsyncSessionLocal() as session:
                col = await er.resolve_collect(session, index, types, did)
            r_items.append((did, col))
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"resolve-collect: {str(exc)[:150]}"
    flat_prompts = [c["prompt"] for _, col in r_items for c in col["chunks"]]
    log.info("middle: %d adjudication prompts across %d docs",
             len(flat_prompts), len(r_items))
    r_replies = await client.run_round(
        "adjudicate", er.RESOLVER_AGENT, flat_prompts
    ) if flat_prompts else []
    it = iter(r_replies)
    resolved = 0
    for did, col in r_items:
        replies = [next(it) for _ in col["chunks"]]
        try:
            async with AsyncSessionLocal() as session:
                await er.resolve_apply(session, index, types, did,
                                       col["chunks"], replies,
                                       col["creation"], write=True)
            resolved += 1
        except Exception as exc:  # noqa: BLE001
            errors[str(did)] = f"resolve-apply: {str(exc)[:150]}"
        if resolved % 250 == 0:
            log.info("middle: resolve applied %d/%d", resolved, len(r_items))
    return {"resolved": resolved, "grounded": g_done, "errors": errors}


async def stage_relationships(doc_ids: list[uuid.UUID], job_dir: Path) -> dict:
    """Build relationship prompts from resolved mentions, batch, persist
    through the standard extractor with stored replies."""
    from app.db import AsyncSessionLocal
    from app.services import relationship_oneshot as rel

    client = BatchClient(job_dir)

    async with AsyncSessionLocal() as session:
        definitions = await rel.load_definitions(session)

    # collect prompts per doc via a collecting client run in no-write mode
    class Collector:
        def __init__(self):
            self.prompts: list[str] = []

        async def invoke(self, agent: str, message: str) -> str:
            self.prompts.append(message)
            raise _Collected()

    class _Collected(Exception):
        pass

    doc_prompts: list[tuple[uuid.UUID, list[str]]] = []
    for did in doc_ids:
        col = Collector()
        try:
            async with AsyncSessionLocal() as session:
                await rel.extract_document(session, col, did,
                                           definitions=definitions,
                                           write=False)
        except Exception:  # noqa: BLE001 — _Collected or prep issues
            pass
        if col.prompts:
            doc_prompts.append((did, list(col.prompts)))
    flat = [p for _, ps in doc_prompts for p in ps]
    log.info("relationships: %d docs -> %d prompts",
             len(doc_prompts), len(flat))
    if not flat:
        return {"rel_docs": 0}

    replies = await client.run_round("relationships", rel.RELATIONSHIP_AGENT
                                     if hasattr(rel, "RELATIONSHIP_AGENT")
                                     else "grove/relationship-oneshot-agent",
                                     flat)

    # persist: same extractor, stored replies keyed by consumption order
    it = iter(replies)
    per_doc_replies = {did: [next(it) for _ in ps] for did, ps in doc_prompts}

    class Stored:
        def __init__(self, queue: list):
            self.queue = list(queue)

        async def invoke(self, agent: str, message: str) -> str:
            if not self.queue or self.queue[0] is None:
                raise RuntimeError("missing stored relationship reply")
            return self.queue.pop(0)

    ok = 0
    errors: dict[str, str] = {}
    sem = asyncio.Semaphore(PERSIST_CONCURRENCY)

    async def persist(did: uuid.UUID) -> None:
        nonlocal ok
        async with sem:
            try:
                async with AsyncSessionLocal() as session:
                    await rel.extract_document(
                        session, Stored(per_doc_replies[did]), did,
                        definitions=definitions, write=True)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errors[str(did)] = str(exc)[:200]

    for start in range(0, len(doc_prompts), MIDDLE_GROUP):
        await asyncio.gather(*(persist(did) for did, _ in
                               doc_prompts[start:start + MIDDLE_GROUP]))
        log.info("rel-persist: %d/%d", min(start + MIDDLE_GROUP,
                 len(doc_prompts)), len(doc_prompts))
    return {"rel_docs": ok, "errors": errors}


# ── driver ──────────────────────────────────────────────────────────────────
async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True)
    ap.add_argument("--stages", default="extract,resolve,relationships")
    ap.add_argument("--job-dir", required=True)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    job_dir = Path(args.job_dir).expanduser()
    job_dir.mkdir(parents=True, exist_ok=True)
    doc_ids = [uuid.UUID(l.strip()) for l in
               Path(args.ids_file).read_text().splitlines() if l.strip()]
    stages = args.stages.split(",")
    report: dict = {"doc_count": len(doc_ids), "stages": {}}

    if "extract" in stages:
        report["stages"]["extract"] = await stage_extract(doc_ids, job_dir)
    if "resolve" in stages:
        report["stages"]["resolve"] = await stage_resolve(doc_ids, job_dir)
    if "relationships" in stages:
        report["stages"]["relationships"] = await stage_relationships(
            doc_ids, job_dir)

    (job_dir / "report.json").write_text(json.dumps(report, indent=1,
                                                    default=str))
    print(json.dumps({k: (v if not isinstance(v, dict) else
                          {kk: (vv if not isinstance(vv, dict) else len(vv))
                           for kk, vv in v.items()})
                      for k, v in report["stages"].items()}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
