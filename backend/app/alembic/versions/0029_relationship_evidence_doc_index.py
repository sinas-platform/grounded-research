"""Index relationship.evidence_document_id — the only unindexed FK on the table.

relationship carries indexes on the definition, the source and the target, but
never on evidence_document_id, so anything asking "does this document have
relationships?" had to scan the whole table once per document.

/api/v1/maintenance/completeness does exactly that: a correlated EXISTS per
document, and it is the pre-batch gate an operator runs before treating a
corpus as done. On 35,407 documents and 220,638 relationships it did not
complete in 17 minutes without this index and returns in 41 seconds with it.
The index builds in about 2 seconds.

The completeness gate is not the only consumer. services/relationship_oneshot.py
filters on evidence_document_id per document while deciding what has already
been extracted, so it pays the same scan on every document ingested; the bulk
zip route runs the same EXISTS shape, and api/v1/relationships.py filters on the
column directly.

Plain btree, not partial: only 285 of 220,638 rows are NULL, so excluding them
would save nothing. Not covering either: the EXISTS needs existence and nothing
else, and the planner already answers it with an index-only scan.

CONCURRENTLY so an existing deployment keeps serving while it builds; that
requires running outside a transaction.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rel_evidence_doc "
            "ON relationship (evidence_document_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_rel_evidence_doc")
