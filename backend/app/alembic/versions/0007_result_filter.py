"""add persistent filter state to result

The deep-search agent loop mutates a SgrFilter iteratively. Previously the
filter only lived in the agent's chat context, which made the trace
agent-discipline-dependent and forced the full filter object on the wire
every introspect call.

This migration moves the filter onto the Result row itself:
  - `filter` jsonb: the persisted current filter state
  - `filter_version` int: optimistic concurrency token, incremented on every
    mutation. Mutation requests carry the expected version and 409 on mismatch.

See `docs/adrs/2026-05-14-stateful-filter-on-result.md`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "result",
        sa.Column(
            "filter",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "result",
        sa.Column(
            "filter_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("result", "filter_version")
    op.drop_column("result", "filter")
