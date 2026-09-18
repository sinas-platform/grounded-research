"""Entity names and aliases are searchable by substring.

The planner resolves every name and probe it proposes with an
`ILIKE '%…%'` over the entity table and over the alias table — some forty
lookups per question — and nothing indexed either column, so each lookup
was a scan of the entity table: 545 MB, 1.1 million rows. Measured on
18 September 2026, with the per-entity counts already moved out of the
lookup: 20 to 30 seconds per lookup, four minutes of planning between two
model calls of 18 seconds.

Trigram, because the match is a substring of a name written by a model in
its own words — "Kestrel Holdings" against "Kestrel Holdings v Northmoor
Authority" — and a B-tree cannot serve that. With these two indexes the
same lookup takes 0.26 s as a union of two indexed branches (the resolver
was rewritten to that shape: an OR across a column and a subquery cannot
use the index and still scans). The entity index is 79 MB and took 3¾
minutes to build on that table; the alias index 3.5 MB.

CONCURRENTLY, and therefore outside a transaction: both tables are written
during ingestion and an exclusive lock on them stops a bulk load.

Revision ID: 0050
Revises: 0049
"""
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entity_canonical_form_trgm "
            "ON entity USING gin (canonical_form gin_trgm_ops)")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entity_alias_alias_trgm "
            "ON entity_alias USING gin (alias gin_trgm_ops)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entity_alias_alias_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entity_canonical_form_trgm")
