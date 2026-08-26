"""Index entity_mention.entity_id — the join column the corpus map ranks on.

entity_mention carried indexes on document, status and the unlinked case, but
never on entity_id, so anything grouping mentions per entity had to hash the
whole table. build_corpus_map() does exactly that on every question: on a
real corpus (484k live entities, 5.2M mentions) it spilled past
temp_file_limit and the query died before a single agent call.

The query is being reshaped in the same change to aggregate entity_mention
before joining, which is what stops the spill; this index is what makes the
reshaped form fast — roughly 100s without it, 14s with.

CONCURRENTLY so an existing deployment keeps serving while it builds; that
requires running outside a transaction.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entity_mention_entity_id "
            "ON entity_mention (entity_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entity_mention_entity_id")
