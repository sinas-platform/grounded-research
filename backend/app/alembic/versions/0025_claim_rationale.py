"""Record why each claim rests on the source it cites.

Everything stored about a claim looked backwards: the passage, and the
validator's account of whether that passage carries the sentence. Nothing
recorded the forward argument — why this authority, for this part of the
question, and what was passed over to get here.

Two things need it. Reviewers asked for the reasoning claim by claim rather
than the source list alone. And when the answer gate names a stronger source
the draft did not use, keeping the original citation is often the right call;
without somewhere to say so, that decision is invisible and indistinguishable
from having missed it.

Nullable: answers written before this migration have no rationale and are
still valid answers.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("answer_claim", sa.Column("rationale", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("answer_claim", "rationale")
