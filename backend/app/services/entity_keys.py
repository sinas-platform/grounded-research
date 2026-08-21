"""Reference keys for entities: learning them, resolving by them.

Extractors name an edge target by whatever the document calls it — a case
number, a celex id, a registry slug. Resolution matched entity NAMES, so
those proposals parked in the unresolved queue and stayed there: 3,744
is_full_text_of edges at the time this module was written, which is why 81%
of court decisions had no issuing body and no authority tier for the
planner to see.

The design is learning, not declaration. Nothing here parses a key or knows
a citation format. A key becomes an alias the moment it is PROVEN — the
extractor supplied both a name and a reference and the name resolved, or a
key match resolved an edge, or a human confirmed a proposal. From then on
that key resolves directly. A clinical corpus's trial ids work identically
to a legal corpus's case numbers, because the platform never looks inside
either.

Matching is over a normalized form (uppercase, alphanumerics only) so that
"COMP/M.11936", "M.11936" and "Case M.11936" agree. A pure-number key is
never matched by containment — "11936" would match both M.11936 and an
unrelated C11936 — and a containment hit that lands on more than one live
entity resolves nothing: ambiguity is an answer, not a coin flip.
"""

from __future__ import annotations

import bisect
import re
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, EntityAlias

_ALNUM = re.compile(r"[^A-Z0-9]+")
# Below this, normalized keys are too short to mean one thing ("ART5").
MIN_KEY_LEN = 5


def key_norm(s: str) -> str:
    """Canonical comparison form: uppercase, alphanumerics only."""
    return _ALNUM.sub("", (s or "").upper())


def containable(norm_key: str) -> bool:
    """May this key be matched by containment in an entity name?

    Requires a letter: a digits-only key matches too many things. Equality
    matches (alias, natural_key) are exempt — they are exact, not fuzzy.
    """
    return len(norm_key) >= MIN_KEY_LEN and any(c.isalpha() for c in norm_key)


class KeyIndex:
    """In-memory key resolver over live entities, for replay and ingestion.

    Built once per batch: alias and natural-key lookups are exact on the
    normalized form; containment scans names only when exact misses. Merged
    entities resolve through to their survivor.
    """

    def __init__(self) -> None:
        self.by_key: dict[str, set[uuid.UUID]] = {}
        self.names: list[tuple[str, uuid.UUID, uuid.UUID | None]] = []
        self.types: dict[uuid.UUID, uuid.UUID] = {}
        self.merged: dict[uuid.UUID, uuid.UUID] = {}
        # One blob of every normalized name, "|"-separated, plus the offset
        # where each name starts. Containment then runs at C speed twice
        # over: blob.find() locates every occurrence of the key, and a
        # bisect over the offsets maps each occurrence back to its name.
        # A key that names nothing (most of the queue) costs one scan; a
        # key that names something costs one scan plus a lookup per hit —
        # never a Python sweep over 400k names.
        self._blob: str | None = None
        self._offsets: list[int] = []

    @classmethod
    async def load(cls, session: AsyncSession) -> "KeyIndex":
        idx = cls()
        rows = (await session.execute(
            select(Entity.id, Entity.canonical_form, Entity.natural_key,
                   Entity.entity_type_id, Entity.merged_into_id))).all()
        for eid, name, nk, tid, merged_into in rows:
            idx.types[eid] = tid
            if merged_into is not None:
                idx.merged[eid] = merged_into
                continue
            if nk:
                idx.by_key.setdefault(key_norm(nk), set()).add(eid)
            idx.names.append((key_norm(name or ""), eid, tid))
        for alias, eid in (await session.execute(
                select(EntityAlias.alias, EntityAlias.entity_id))).all():
            eid = idx._live(eid)
            if eid is not None:
                idx.by_key.setdefault(key_norm(alias), set()).add(eid)
        return idx

    def _live(self, eid: uuid.UUID) -> uuid.UUID | None:
        seen = set()
        while eid in self.merged:
            if eid in seen:
                return None
            seen.add(eid)
            eid = self.merged[eid]
        return eid

    def resolve(self, key: str,
                entity_type_id: uuid.UUID | None = None) -> uuid.UUID | None:
        """The single live entity this key names, or None.

        Exact alias/natural-key first; containment in names as fallback.
        More than one candidate of the requested type resolves nothing.
        """
        nk = key_norm(key)
        if not nk:
            return None

        def _only(cands: set[uuid.UUID]) -> uuid.UUID | None:
            if entity_type_id is not None:
                cands = {e for e in cands if self.types.get(e) == entity_type_id}
            return next(iter(cands)) if len(cands) == 1 else None

        hit = _only(self.by_key.get(nk, set()))
        if hit is not None:
            return hit
        if not containable(nk):
            return None
        if self._blob is None:
            parts, off, pos = [], [], 0
            for name, _, _ in self.names:
                off.append(pos)
                parts.append(name)
                pos += len(name) + 1
            self._blob, self._offsets = "|".join(parts), off
        cands: set[uuid.UUID] = set()
        start = self._blob.find(nk)
        while start != -1:
            i = bisect.bisect_right(self._offsets, start) - 1
            _name, eid, tid = self.names[i]
            if entity_type_id is None or tid == entity_type_id:
                cands.add(eid)
            start = self._blob.find(nk, start + 1)
        return _only(cands)

    def learn(self, entity_id: uuid.UUID, key: str) -> None:
        nk = key_norm(key)
        if nk:
            self.by_key.setdefault(nk, set()).add(entity_id)


_shared: dict = {"index": None, "at": 0.0}
_SHARED_TTL = 600.0


async def shared_index(session: AsyncSession) -> "KeyIndex":
    """One KeyIndex per process, rebuilt at most every ten minutes.

    Loading the index reads every entity and alias — built once per replay
    batch, that is fine. The ingestion runner, though, resolves per document,
    and building it per document turned a 789-document run into a projected
    22 hours and starved the API: twelve workers each re-scanning 446k
    entities continuously. Freshness is not correctness here — learn() keeps
    this process's copy current for everything it resolves itself, and keys
    minted elsewhere are picked up by the next rebuild or by the replay that
    runs at ingestion completion.
    """
    now = time.monotonic()
    if _shared["index"] is None or now - _shared["at"] > _SHARED_TTL:
        _shared["index"] = await KeyIndex.load(session)
        _shared["at"] = now
    return _shared["index"]


async def learn_aliases(session: AsyncSession, entity_id: uuid.UUID,
                        keys: list[str]) -> int:
    """Store proven keys as aliases; returns how many were new.

    Idempotent on the normalized form: "COMP/M.11936" is not added when
    "comp/m.11936" is already there.
    """
    keys = [k.strip() for k in keys if k and k.strip()]
    if not keys:
        return 0
    have = {key_norm(a) for (a,) in (await session.execute(
        select(EntityAlias.alias).where(EntityAlias.entity_id == entity_id)
    )).all()}
    added = 0
    for k in keys:
        nk = key_norm(k)
        if not nk or nk in have:
            continue
        have.add(nk)
        session.add(EntityAlias(entity_id=entity_id, alias=k[:500]))
        added += 1
    return added
