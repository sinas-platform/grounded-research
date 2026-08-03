"""Annotation framework: definitions + materialized values.

annotation_definition holds config-declared derived fields (relationship
path + reducer + materialize flag), imported from the GrovePackage and
validated loudly at import. annotation_value holds materialized results,
one row per (definition, subject).

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "annotation_definition",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.Text),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("reduce", JSONB, nullable=False),
        sa.Column("materialize", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("subject_ref_type", sa.String(40), nullable=False),
        sa.Column("managed_by", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_annotation_definition_managed_by", "annotation_definition", ["managed_by"])

    op.create_table(
        "annotation_value",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "annotation_definition_id",
            UUID(as_uuid=True),
            sa.ForeignKey("annotation_definition.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_id", UUID(as_uuid=True), nullable=False),
        sa.Column("value", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("annotation_definition_id", "subject_id", name="uq_annotation_subject"),
    )
    op.create_index(
        "ix_annotation_value_definition", "annotation_value", ["annotation_definition_id"]
    )
    op.create_index("ix_annotation_value_subject", "annotation_value", ["subject_id"])


def downgrade() -> None:
    op.drop_table("annotation_value")
    op.drop_table("annotation_definition")
