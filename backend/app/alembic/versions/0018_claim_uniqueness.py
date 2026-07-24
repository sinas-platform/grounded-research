"""Answer claims: one row per (answer_id, sequence).

The drafting agent can re-post its claim batch (slow-response retry); with
no uniqueness constraint both waves persisted, duplicating every claim and
doubling validation cost. Dedupe keeps the NEWEST row per (answer_id,
sequence) — a re-post supersedes the original — then enforces uniqueness.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM answer_claim ac
        USING answer_claim newer
        WHERE ac.answer_id = newer.answer_id
          AND ac.sequence = newer.sequence
          AND (ac.created_at, ac.id) < (newer.created_at, newer.id)
        """
    )
    op.create_index(
        "uq_answer_claim_sequence",
        "answer_claim",
        ["answer_id", "sequence"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_answer_claim_sequence", table_name="answer_claim")
