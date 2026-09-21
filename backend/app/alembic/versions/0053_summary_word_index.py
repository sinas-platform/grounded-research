"""The summary's words are indexed.

Retrieval's summary channel matches a hypothesis's words against every
document's summary. Without an index that is the summary of every
document tokenised at query time, then ranked: measured at 40 to 45
seconds per hypothesis on 144,200 documents while ingestion was writing,
twelve hypotheses to a question. With the index the channel selects its
candidates the way the full-text and proposition channels do — the
documents carrying the most of the hypothesis's rare words, found in the
index — and tokenises only those.

The expression is the one `app.hypotheses._summary_words` writes, word for
word: an expression index serves a query only when the planner sees the
same expression.

Revision ID: 0053
Revises: 0052
"""

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

INDEX = "ix_document_summary_tsv"


def upgrade() -> None:
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {INDEX} ON document
        USING gin (to_tsvector('simple', coalesce(summary, '')))
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
