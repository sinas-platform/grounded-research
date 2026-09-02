"""Reference and tags on query_run — identity and grouping for runs.

`reference` is a caller-supplied identifier for the logical question a run
answers. Deliberately NOT unique: every rerun of benchmark question Q16
carries reference "benchmark-q16", so "the versions of this question over
time" is a WHERE clause instead of matching on question text — which is how
runs had to be found until now. An external caller may put its own request
id here instead.

`tags` group runs into named sets — a regeneration batch is born tagged
("round-3") and thereafter exists as a queryable thing rather than a
timestamp range someone reconstructs later.

Both nullable/empty and both editable after the fact, so existing runs can
be stamped retroactively.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "query_run",
        sa.Column("reference", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "query_run",
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(length=100)),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_index("ix_query_run_reference", "query_run", ["reference"])
    op.create_index(
        "ix_query_run_tags",
        "query_run",
        ["tags"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_query_run_tags", table_name="query_run")
    op.drop_index("ix_query_run_reference", table_name="query_run")
    op.drop_column("query_run", "tags")
    op.drop_column("query_run", "reference")
