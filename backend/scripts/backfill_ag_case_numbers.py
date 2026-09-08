"""Give the Advocate General Opinion class the identifier it already claims.

The class sets identifier_property = 'case_number' and has no such property
defined, so the join in `claim_naming` matches nothing and all 293 of its
documents are invisible to the naming checks. Nothing reports this: a class
that points its identifier at a property it does not define looks, from the
checks, exactly like a class that opted out.

Two steps, and the second is why this is a script rather than a migration.
Adding the property is a schema row. Filling it for 293 documents is derived
data, and derived data should be looked at before it is written.

    python -m scripts.backfill_ag_case_numbers              # print, write nothing
    python -m scripts.backfill_ag_case_numbers --apply      # write

The derivation reads the CELEX filename: 62020CC0117 is the Advocate General's
Opinion in case C-117/20. Validated against every CELEX-named document in the
corpus that already carries a case number by hand, 1498 of them, matching all
1498 with no disagreement. That is good evidence and not a proof, which is why
--dry-run is the default and prints every value it would write.

Idempotent: a document that already has a value is left alone, so a partial
run can be finished by running it again.
"""

from __future__ import annotations

import argparse
import asyncio
import re

import sqlalchemy as sa

from app.db import AsyncSessionLocal

# Casts are written `cast(:x as uuid)` rather than `:x::uuid`: SQLAlchemy
# only recognises a bind parameter when the character after its name is
# not a colon, so the `::` form silently leaves `:x` in the SQL and
# Postgres rejects it. The value written into jsonb_build_object is cast
# for a different reason: the function is variadic "any", so Postgres
# cannot infer a bare parameter's type and refuses to prepare the
# statement.
CLASS_NAME = "Advocate General Opinion"
PROPERTY = "case_number"

# CELEX sector 6 is case law. The two letters after the year are the document
# type, and the court they belong to is the first half of a case number.
# Unlisted types are declined rather than guessed: writing a wrong court letter
# would be worse than leaving the document as it is now, because a wrong
# identifier is believed.
_COURT = {
    "CJ": "C",  # Court of Justice, judgment
    "CC": "C",  # Advocate General's Opinion
    "CO": "C",  # Court of Justice, order
    "CV": "C",  # Court of Justice, opinion
    "CP": "C",  # View of an Advocate General
    "CB": "C",
    "CA": "C",
    "TJ": "T",  # General Court, judgment
    "TO": "T",  # General Court, order
    "TC": "T",
    "TB": "T",
    "TA": "T",
    "FJ": "F",  # Civil Service Tribunal
    "FO": "F",
    "FT": "F",
}

_CELEX = re.compile(r"^6(\d{4})([A-Z]{2})(\d{4})")


def case_number_from_celex(filename: str) -> str | None:
    """The case a CELEX-named document belongs to, or None if it cannot say."""
    m = _CELEX.match(filename)
    if not m:
        return None
    year, doc_type, number = m.groups()
    court = _COURT.get(doc_type)
    if court is None:
        return None
    return f"{court}-{int(number)}/{year[2:]}"


_CLASS = sa.text(
    "select id::text from document_class where name = :name"
)

_PROPERTY_ID = sa.text(
    """
    select id::text from document_class_property
     where document_class_id = cast(:class_id as uuid) and name = :property
    """
)

_ADD_PROPERTY = sa.text(
    """
    insert into document_class_property (document_class_id, name, description)
    values (cast(:class_id as uuid), :property, :description)
    returning id::text
    """
)

_DOCUMENTS = sa.text(
    """
    select d.id::text, d.filename
      from document d
     where d.document_class_id = cast(:class_id as uuid)
       and not exists (
             select 1 from property_value pv
              where pv.document_id = d.id
                and pv.property_id = cast(:property_id as uuid))
     order by d.filename
    """
)

_WRITE = sa.text(
    """
    insert into property_value (document_id, property_id, value)
    values (cast(:document_id as uuid), cast(:property_id as uuid),
            jsonb_build_object('_', cast(:value as text)))
    """
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write; without it nothing is written")
    args = ap.parse_args()

    async with AsyncSessionLocal() as session:
        class_id = (await session.execute(
            _CLASS, {"name": CLASS_NAME})).scalar_one_or_none()
        if class_id is None:
            raise SystemExit(f"no document class named {CLASS_NAME!r}")

        property_id = (await session.execute(
            _PROPERTY_ID, {"class_id": class_id, "property": PROPERTY}
        )).scalar_one_or_none()
        if property_id is None:
            print(f"property {PROPERTY!r} is missing from {CLASS_NAME!r} "
                  f"and would be added")
            if args.apply:
                property_id = (await session.execute(_ADD_PROPERTY, {
                    "class_id": class_id, "property": PROPERTY,
                    "description": "The case this opinion was delivered in.",
                })).scalar_one()
                await session.commit()
                print(f"  added, id {property_id}")
            else:
                # Nothing to write into and nothing to compare against, so the
                # documents are read straight from the class instead.
                property_id = None

        if property_id is None:
            rows = (await session.execute(sa.text(
                "select id::text, filename from document "
                " where document_class_id = cast(:class_id as uuid) order by filename"
            ), {"class_id": class_id})).all()
        else:
            rows = (await session.execute(_DOCUMENTS, {
                "class_id": class_id, "property_id": property_id})).all()

        derived = [(doc_id, name, case_number_from_celex(name))
                   for doc_id, name in rows]
        writable = [(d, n, v) for d, n, v in derived if v]
        declined = [(d, n) for d, n, v in derived if not v]

        for _, name, value in writable:
            print(f"  {name:24} -> {value}")
        for _, name in declined:
            print(f"  {name:24} -> declined, not a CELEX case-law name")

        print(f"\n{len(writable)} to write, {len(declined)} declined, "
              f"{len(rows)} without a value")
        if not args.apply:
            print("dry run, nothing written. Re-run with --apply to write.")
            return

        for doc_id, _, value in writable:
            await session.execute(_WRITE, {
                "document_id": doc_id, "property_id": property_id,
                "value": value})
        await session.commit()
        print(f"wrote {len(writable)} values")


if __name__ == "__main__":
    asyncio.run(main())
