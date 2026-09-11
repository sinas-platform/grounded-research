"""claim_evidence.quote

The passage that justified a citation was never kept. The extractor verifies
a quote against the source, records where it sits, and discards the text, so
what survives is a coordinate and nothing to check it against. Two questions
asked of the stored answers on 10 September could not be answered from the
rows: whether a span still covers the sentence it was written for, and what
was lost when a claim was dropped for having no text.

A coordinate is assertable at write time and not afterwards. With the quote
beside it a span is checkable: re-read the offsets, compare, and a span that
has drifted says so instead of quietly slicing the wrong text. It also makes
the character offsets added alongside this backfillable, which without it they
are not, since there is nothing left to match.

Capped at the 2,000 characters the extractor already stores, and nullable:
rows written before this, and the reviser's rows, which carry coordinates the
model reports and no quote at all, have none.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claim_evidence",
        sa.Column("quote", sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("claim_evidence", "quote")
