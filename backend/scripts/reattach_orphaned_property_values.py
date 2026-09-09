"""Re-point property values left on another class's property row.

A property value belongs to a document and to one class's declared property.
Reclassify the document and the value stays where it was: attached to the
property row of the class the document used to be in. Every reader that scopes
a property to the document's own class stops seeing it, and the document reads
as though the value was never extracted.

Nothing reports this. A class whose values all sit on another class's row looks
exactly like a class whose documents were never extracted, and a check that
scopes by class returns nothing either way. That is why the repair is
re-attachment and not re-derivation: the values exist, they are the extracted
ones, and deriving replacements loses whatever the extractor saw that a
derivation cannot reconstruct.

    python -m scripts.reattach_orphaned_property_values                # print
    python -m scripts.reattach_orphaned_property_values --apply        # write
    python -m scripts.reattach_orphaned_property_values --class "..." --property ...

Printing is the default because this is derived-state repair over data someone
else extracted, and because the run that cannot be re-attached is as
interesting as the one that can: a value whose class does not declare the
property is reported, not skipped in silence.

Nothing here knows what a class or a property means. Which ones to repair is
given on the command line or, given nothing, read from whatever the corpus
turns out to hold.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass

import sqlalchemy as sa

from app.db import AsyncSessionLocal


@dataclass(frozen=True)
class Orphan:
    """A value sitting on a property row belonging to another class."""

    document_id: str
    filename: str
    property_name: str
    value_id: str
    held_by: str
    belongs_to: str
    target_property_id: str | None
    target_occupied: bool


@dataclass(frozen=True)
class Action:
    kind: str
    orphan: Orphan

    @property
    def target_property_id(self) -> str | None:
        """Only ever set for an action that writes."""
        return self.orphan.target_property_id if self.kind == "reattach" else None


def plan(orphans: list[Orphan]) -> list[Action]:
    """What to do with each orphan. Pure: no I/O, no ordering assumptions.

    Four outcomes and only one of them writes. A target that already holds a
    value is left alone rather than overwritten, because which of the two is
    better is a judgement about the corpus that this script has no way to make
    and no business making.

    The same reasoning covers two orphans competing for one empty target.
    `target_occupied` is read per row when the orphans are selected, so both
    see the target empty and both would be reattached, leaving the destination
    holding two values with nothing recording that they came from a race. The
    script has no more business picking between two orphans than between an
    orphan and a value already there, so it picks neither and says so.

    Not reachable in this corpus today: no document holds two orphaned rows for
    the same property. It becomes reachable as soon as a property is declared
    `cardinality: many`, which is what makes a second row legitimate.
    """
    by_target: Counter[tuple[str, str]] = Counter(
        (o.document_id, o.target_property_id)
        for o in orphans
        if o.target_property_id is not None and not o.target_occupied
    )
    actions: list[Action] = []
    for orphan in orphans:
        if orphan.target_property_id is None:
            actions.append(Action("no_property_on_class", orphan))
        elif orphan.target_occupied:
            actions.append(Action("target_occupied", orphan))
        elif by_target[(orphan.document_id, orphan.target_property_id)] > 1:
            actions.append(Action("contested_target", orphan))
        else:
            actions.append(Action("reattach", orphan))
    return actions


def summarise(actions: list[Action]) -> dict[str, int]:
    return dict(Counter(a.kind for a in actions))


_ORPHANS = sa.text(
    """
    select d.id::text, d.filename, p.name, pv.id::text,
           owner.name, dc.name, tgt.id::text,
           exists (select 1 from property_value x
                    where x.document_id = d.id and x.property_id = tgt.id)
      from property_value pv
      join document d on d.id = pv.document_id
      join document_class_property p on p.id = pv.property_id
      join document_class owner on owner.id = p.document_class_id
      join document_class dc on dc.id = d.document_class_id
      left join document_class_property tgt
        on tgt.document_class_id = dc.id and tgt.name = p.name
     where owner.id <> dc.id
       and (cast(:class_name as text) is null
            or dc.name = cast(:class_name as text))
       and (cast(:property_name as text) is null
            or p.name = cast(:property_name as text))
     order by dc.name, p.name, d.filename
    """
)

# Every parameter is cast, for two different reasons. `cast(:x as uuid)` rather
# than `:x::uuid`, because SQLAlchemy only recognises a bind parameter when the
# character after its name is not a colon, so the `::` form leaves `:x` in the
# statement. And the optional filters above are cast because `$1 is null` gives
# Postgres nothing to infer a type from and it refuses to prepare the
# statement. Both were found by running the dry run, which is what it is for.
# The occupancy the plan read is a fact about the moment the orphans were
# selected, and the plan is printed for a human before anything is written.
# Extraction, the ingestion API or a second run of this script can fill the
# target in between, and there is no uniqueness constraint on
# (document_id, property_id) to catch it. So the condition is restated here,
# where it is evaluated against the row being written rather than remembered:
# a target that has since acquired a value refuses the move instead of
# doubling up, and the caller counts what did not land.
_REATTACH = sa.text(
    """
    update property_value pv
       set property_id = cast(:target as uuid), updated_at = now()
     where pv.id = cast(:value_id as uuid)
       and not exists (select 1 from property_value x
                        where x.document_id = pv.document_id
                          and x.property_id = cast(:target as uuid))
    """
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write; without it nothing is written")
    ap.add_argument("--class", dest="class_name", default=None,
                    help="only documents currently in this class")
    ap.add_argument("--property", dest="property_name", default=None,
                    help="only values of this property")
    args = ap.parse_args()

    async with AsyncSessionLocal() as session:
        # One repair at a time, transaction-scoped: two concurrent --apply
        # runs could each see the same destination empty (the conditional
        # UPDATE checks NOT EXISTS under READ COMMITTED, which neither locks
        # an absent row nor is backed by a uniqueness constraint) and both
        # write. The advisory lock serialises the script against itself; a
        # concurrent ingestion filling the same property remains possible in
        # principle, but ingestion writes properties for documents it is
        # ingesting, and an orphan by definition belongs to a document whose
        # class changed after ingestion — the sets do not meet in practice,
        # and the conditional UPDATE still refuses any fill it can see.
        await session.execute(sa.text(
            "SELECT pg_advisory_xact_lock(hashtext('reattach_orphans'))"))
        rows = (await session.execute(_ORPHANS, {
            "class_name": args.class_name,
            "property_name": args.property_name})).all()
        orphans = [Orphan(*r) for r in rows]
        actions = plan(orphans)

        for a in actions:
            o = a.orphan
            if a.kind == "reattach":
                print(f"  {o.filename:34} {o.property_name:22} "
                      f"{o.held_by} -> {o.belongs_to}")
        for a in actions:
            o = a.orphan
            if a.kind == "no_property_on_class":
                print(f"  {o.filename:34} {o.property_name:22} "
                      f"stranded: {o.belongs_to} declares no such property")
            elif a.kind == "target_occupied":
                print(f"  {o.filename:34} {o.property_name:22} "
                      f"left alone: {o.belongs_to} already holds a value")
            elif a.kind == "contested_target":
                print(f"  {o.filename:34} {o.property_name:22} "
                      f"left alone: another orphan wants the same empty "
                      f"property on {o.belongs_to}")

        counts = summarise(actions)
        print(f"\n{counts.get('reattach', 0)} to re-attach, "
              f"{counts.get('target_occupied', 0)} already hold a value, "
              f"{counts.get('contested_target', 0)} contested by another "
              f"orphan, "
              f"{counts.get('no_property_on_class', 0)} stranded with nowhere "
              f"to go, {len(actions)} orphaned values seen")
        if not args.apply:
            print("dry run, nothing written. Re-run with --apply to write.")
            return

        written = refused = 0
        for a in actions:
            if a.kind != "reattach":
                continue
            result = await session.execute(_REATTACH, {
                "target": a.target_property_id, "value_id": a.orphan.value_id})
            if result.rowcount:
                written += 1
            else:
                refused += 1
                print(f"  {a.orphan.filename:34} {a.orphan.property_name:22} "
                      f"not moved: the target acquired a value after the plan "
                      f"was made")
        await session.commit()
        print(f"re-attached {written} values"
              + (f", refused {refused} whose target filled in the meantime"
                 if refused else ""))


if __name__ == "__main__":
    asyncio.run(main())
