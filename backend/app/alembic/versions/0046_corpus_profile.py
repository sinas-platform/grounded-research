"""What the corpus holds, per entity type, in round numbers.

The planner is grounded in a description of the corpus — the kinds of thing it
holds, roughly how many of each, a few well-used examples of each — and that
description was computed on every run out of the two largest tables there are:
count every mention of every entity, rank every entity within its type by that
count, keep five names and one exact count per type. Six concurrent runs each
spent over sixteen minutes on it before planning began, and the bulk ingestion
writing to the same database lost two thirds of its throughput to them. The
work is not per-question — the answer is the same for every run until the next
document lands — and it grows with the corpus, so the cost grows every hour.

This table is that answer, computed out of band and read as a lookup.

A MAGNITUDE, NOT A COUNT. What the figure is for is judging how much a filter
will cut, and 512,004 rather than 500,000 changes no plan. `1-2-5` rounding:
the stored value is the largest of 1, 2, 5, 10, 20, 50, 100 ... at or below the
estimate, never wrong by more than a factor of 2.5, visibly round so that
nothing downstream mistakes it for a measurement, and stable enough that a
load of ten thousand entities usually leaves it untouched. 0 means the refresh
observed none. Powers of ten alone were the alternative and are too coarse:
they report 900,000 and 110,000 as the same number.

`examples` is a JSON array of canonical forms. `refreshed_at` is when the
figures were computed; a reader that finds it older than its tolerance uses no
figures at all rather than old ones.

Keyed on the entity type, so dropping a type takes its profile with it.

Revision ID: 0046
Revises: 0045
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corpus_profile",
        sa.Column("entity_type_id", UUID(as_uuid=True),
                  sa.ForeignKey("entity_type.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("entity_count_magnitude", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("examples", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="[]"),
        sa.Column("refreshed_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("corpus_profile")
