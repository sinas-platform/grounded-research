"""By-name entity matchers must not resurrect merged entities.

`entity.merged_into_id` is the merge tombstone (0017): the loser row stays so
references hold and the merge is reversible. Any matcher that looks entities
up BY NAME must therefore skip tombstones, or the next write re-matches a
merged-away name and lands on the dead row, silently undoing the merge.

Two such matchers are covered here: the one-shot extraction gazetteer and the
entity-upsert alias matcher. Both fake sessions emulate the tombstone filter,
honouring a `merged_into_id IS NULL` clause only when the query carries one,
so each test fails against the unfiltered query and passes with it.

By-id lookups (grounding gate, annotations) cannot resurrect anything and are
deliberately not covered.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.ingestion import _alias_match
from app.services.ingestion_oneshot import _load_gazetteer

TYPE_ID = uuid.uuid4()
LIVE_ID = uuid.uuid4()
MERGED_ID = uuid.uuid4()

LIVE_NAME = "Acme Holdings plc"
MERGED_NAME = "Acme plc"

# (id, canonical_form, merged_into_id)
_ENTITIES = [
    (LIVE_ID, LIVE_NAME, None),
    (MERGED_ID, MERGED_NAME, LIVE_ID),
]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _GazetteerSession:
    async def execute(self, stmt):
        s = str(stmt)
        if "entity_alias" in s:
            return _Result([])
        rows = [(eid, cf) for eid, cf, merged in _ENTITIES]
        if "merged_into_id IS NULL" in s:
            rows = [(eid, cf) for eid, cf, merged in _ENTITIES if merged is None]
        return _Result(rows)


class _UpsertSession:
    """Emulates the entity-upsert lookups.

    After a merge the loser's name is recorded as an alias of the winner, so
    the alias table maps MERGED_NAME to the live row. The canonical_form
    lookup still finds the tombstone unless the query filters it out.
    """

    def __init__(self, wanted: str):
        self.wanted = wanted

    async def execute(self, stmt):
        s = str(stmt)
        filtered = "merged_into_id IS NULL" in s
        if "entity_alias" in s:  # the alias join
            if self.wanted == MERGED_NAME:
                return _Result([_entity(LIVE_ID, LIVE_NAME, None)])
            return _Result([])
        rows = [
            _entity(eid, cf, merged)
            for eid, cf, merged in _ENTITIES
            if cf == self.wanted and not (filtered and merged is not None)
        ]
        return _Result(rows)


def _entity(eid, canonical_form, merged_into_id):
    return SimpleNamespace(
        id=eid,
        canonical_form=canonical_form,
        entity_type_id=TYPE_ID,
        merged_into_id=merged_into_id,
    )


@pytest.mark.asyncio
async def test_merged_entities_are_absent_from_the_gazetteer():
    gazetteer = await _load_gazetteer(_GazetteerSession())

    ids = {eid for _alias, eid, _canon in gazetteer}
    assert LIVE_ID in ids
    assert MERGED_ID not in ids


@pytest.mark.asyncio
async def test_upsert_matcher_resolves_a_merged_name_to_the_winner():
    # The old name must resolve through the winner's alias, never to the
    # tombstone it used to name.
    matched = await _alias_match(_UpsertSession(MERGED_NAME), TYPE_ID, MERGED_NAME)

    assert matched is not None
    assert matched.id == LIVE_ID


@pytest.mark.asyncio
async def test_upsert_matcher_still_matches_a_live_entity():
    matched = await _alias_match(_UpsertSession(LIVE_NAME), TYPE_ID, LIVE_NAME)

    assert matched is not None
    assert matched.id == LIVE_ID
