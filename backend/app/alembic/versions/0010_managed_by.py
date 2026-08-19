"""add managed_by to package-managed config tables

`managed_by` marks a row as installed by a SgrPackage so the importer can
prune resources that were removed from the manifest on re-import. NULL means
the row was created manually (UI / API) and is left alone by the importer.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = (
    "document_class",
    "entity_type",
    "relationship_definition",
    "dossier_class",
    "playbook_scope",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("managed_by", sa.String(128), nullable=True))
        op.create_index(f"ix_{table}_managed_by", table, ["managed_by"])


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f"ix_{table}_managed_by", table_name=table)
        op.drop_column(table, "managed_by")
