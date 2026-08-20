"""Promote the name-identity index to UNIQUE.

Separate from 0022 so the backfill sits between them: 0022 adds the column,
the backfill populates it and merges any collisions, and this makes the
constraint real. Run in one go on a fresh deployment; run in sequence on a
corpus that already has duplicates.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_entity_normalized_form", table_name="entity")
    op.create_index(
        "ix_entity_normalized_form", "entity",
        ["entity_type_id", "normalized_form"], unique=True,
        postgresql_where=sa.text("normalized_form IS NOT NULL AND merged_into_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_entity_normalized_form", table_name="entity")
    op.create_index(
        "ix_entity_normalized_form", "entity",
        ["entity_type_id", "normalized_form"],
        postgresql_where=sa.text("normalized_form IS NOT NULL AND merged_into_id IS NULL"),
    )
