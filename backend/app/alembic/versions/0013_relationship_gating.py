"""relationship creation gating: creation_mode on relationship_definition

Per-RelationshipDefinition policy for what happens when an ingestion agent
calls POST /ingest/relationships:
  - open    : insert the Relationship directly (current behavior; default)
  - review  : insert as a RelationshipProposal regardless of which endpoint
              the agent called
  - closed  : reject (HTTP 409). Curated edges only — populated via the
              SgrPackage YAML or the admin UI; runtime extraction blocked.

Mirrors entity_type.creation_mode added in 0012.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "relationship_definition",
        sa.Column(
            "creation_mode",
            sa.String(10),
            nullable=False,
            server_default="open",
        ),
    )
    op.create_check_constraint(
        "ck_relationship_definition_creation_mode",
        "relationship_definition",
        "creation_mode IN ('open', 'review', 'closed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_relationship_definition_creation_mode", "relationship_definition"
    )
    op.drop_column("relationship_definition", "creation_mode")
