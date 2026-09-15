"""answer.open_notes

Points the completeness review raised that the answer did not take up. The
review and the drafter now argue in both directions — the drafter can refuse
a request with a reason, and the review must accept that reason or press it
with something new — and an argument that ends without agreement has to end
somewhere the reader can see.

Most entries are a record: the review asked, the drafter explained, the
review accepted. The ones marked `caveat` are a disagreement about whether
the answer is complete, over a source the review called essential and
justified; those are rendered into the answer beside the part they bear on.

Null on every answer written before this existed, and on any answer where
nothing was argued.

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "answer",
        sa.Column("open_notes", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column("answer", "open_notes")
