"""Title on query_run — the human name of the question a run answers.

`reference` identifies the logical question and `tags` group a batch, but
neither is readable. A review platform showing a list of runs had nothing to
put in front of a person except the question text, which is a paragraph.

The title is the benchmark's own name for the question — its number, its
topic and its sub-topic, as "Q41 — Dawn raids — electronic data". It is not
derived from `reference` or from the question text, because the number and
the topic live in the benchmark question set and nowhere in this system;
deriving either would invent it.

Nullable and editable after the fact, like the other two, so existing runs
can be stamped retroactively.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "query_run",
        sa.Column("title", sa.String(length=300), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("query_run", "title")
