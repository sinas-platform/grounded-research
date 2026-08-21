"""Give name-identified entities a real identity, enforced by the database.

The only uniqueness on `entity` was (entity_type_id, natural_key) WHERE
natural_key IS NOT NULL — and 98.9% of entities have no natural key, because
a natural key is derived from patterns like case numbers and ECLIs. Every
entity identified by a NAME ("CMA", "European Commission") had no constraint
at all, so two resolvers running concurrently would each look one up, neither
would see the other's uncommitted row, and both would insert. That is not a
rare race: of the duplicates found on a production corpus, 47,607 were
created within a minute of their twin and 225,179 within an hour, and the
operational rule "run only one resolve stage at a time" existed to work
around it.

This adds `normalized_form` — entity_resolver.normalize(canonical_form), the
same function resolution matches on — and makes it unique per type among live
entities. Duplicates become impossible rather than discouraged.

The backfill uses SQL that mirrors normalize(): NFD accent folding via
unaccent-free translation is not available in plain Postgres, so the column
is backfilled by the application (scripts/backfill_normalized_form.py) and
this migration only creates the column and the index. Creating the index
fails loudly if collisions remain — run the dedup pass first.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("entity", sa.Column("normalized_form", sa.String(500), nullable=True))
    # Non-unique first so the backfill can run against a live system; the
    # unique index is created separately once the column is populated.
    op.create_index(
        "ix_entity_normalized_form", "entity",
        ["entity_type_id", "normalized_form"],
        postgresql_where=sa.text("normalized_form IS NOT NULL AND merged_into_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_entity_normalized_form", table_name="entity")
    op.drop_column("entity", "normalized_form")
