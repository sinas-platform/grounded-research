"""Entity mentions: grounding status.

The grounding gate (services/grounding_gate) marks extractor names the
document does not support as rejected_ungrounded instead of deleting
them: the row stays for audit, every consumer filters on
status = 'active'. Composite index because the hot consumers read
mentions per document.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "entity_mention",
        sa.Column(
            "status", sa.String(length=30), nullable=False,
            server_default="active",
        ),
    )
    op.create_index(
        "ix_entity_mention_document_status",
        "entity_mention",
        ["document_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_entity_mention_document_status", table_name="entity_mention"
    )
    op.drop_column("entity_mention", "status")
