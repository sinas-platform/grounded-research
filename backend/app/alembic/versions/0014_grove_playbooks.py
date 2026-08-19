"""move playbook content into SGR (drop Sinas dual-write)

A playbook was previously:
  - content stored as a Sinas skill in `sgr_retrieval_playbooks` /
    `sgr_synthesis_playbooks`
  - scope (which doc/dossier classes it applies to) stored in SGR

That split made multi-SGR on one Sinas impossible (namespace collision),
required a Sinas token for every playbook write, and forced the package
importer to do a brittle dual-write. Sinas wasn't actually injecting the
content into agents either — agents are wired to fetch via the SGR
connector.

This migration moves content into a new SGR `playbook` table and rewrites
`playbook_scope` to reference it by id.

The project is unused — destructive migration is safe.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. New playbook table.
    op.create_table(
        "playbook",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.String(20), nullable=False),  # retrieval | synthesis
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("managed_by", sa.String(128), index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("kind", "name", name="uq_playbook_kind_name"),
        sa.CheckConstraint("kind IN ('retrieval', 'synthesis')", name="ck_playbook_kind"),
    )

    # 2. Drop the old scope columns and add a typed FK.
    #    (Project unused — no data preservation needed.)
    op.drop_constraint("uq_playbook_scope", "playbook_scope", type_="unique")
    op.drop_index("ix_ps_ns", table_name="playbook_scope")
    op.drop_index("ix_ps_name", table_name="playbook_scope")
    op.drop_column("playbook_scope", "skill_namespace")
    op.drop_column("playbook_scope", "skill_name")
    op.add_column(
        "playbook_scope",
        sa.Column(
            "playbook_id",
            UUID(as_uuid=True),
            sa.ForeignKey("playbook.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    op.create_unique_constraint(
        "uq_playbook_scope",
        "playbook_scope",
        ["playbook_id", "document_class_id", "dossier_class_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_playbook_scope", "playbook_scope", type_="unique")
    op.drop_column("playbook_scope", "playbook_id")
    op.add_column(
        "playbook_scope",
        sa.Column("skill_namespace", sa.String(200), nullable=False, server_default=""),
    )
    op.add_column(
        "playbook_scope",
        sa.Column("skill_name", sa.String(200), nullable=False, server_default=""),
    )
    op.create_index("ix_ps_ns", "playbook_scope", ["skill_namespace"])
    op.create_index("ix_ps_name", "playbook_scope", ["skill_name"])
    op.create_unique_constraint(
        "uq_playbook_scope",
        "playbook_scope",
        ["skill_namespace", "skill_name", "document_class_id", "dossier_class_id"],
    )
    op.drop_table("playbook")
