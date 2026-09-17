"""The summary is searchable.

Retrieval searches a document's TEXT, which is written in the document's own
language. Its summary is written in one language whatever that is, so a term
in the deployment's working language reaches a document in any other through
its summary and through nothing else. That made the summary the one
cross-language channel available — and nothing had ever searched it, so
nothing had ever indexed it.

Measured on 126,000 summaries: a single `ILIKE '%term%'` took minutes without
this index and 0.6 seconds with it. The lookup is per candidate term, several
per question, so the difference is between a channel that can run inside a
query and one that cannot.

Trigram rather than tsvector, deliberately. The terms searched here are
phrases mined from retrieved text — `virtual data room` — and a substring
match on a phrase is what finds them; `to_tsvector` would need the phrase
tokenised the same way on both sides, and the summaries are prose written by
an extractor rather than a controlled vocabulary.

CONCURRENTLY, and therefore outside a transaction: the table is written to
continuously during ingestion and an exclusive lock on it stops a bulk load.

Revision ID: 0049
Revises: 0048
"""
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_document_summary_trgm "
            "ON document USING gin (summary gin_trgm_ops)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_document_summary_trgm")
