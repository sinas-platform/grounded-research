"""Tie every model call a run makes back to the run.

Spend used to be measured on the synthesis chat: one answer, one chat, sum
its usage rows. Stateless drafting opens a throwaway chat per call — 515
extraction calls produced 515 chat ids — so nothing tied a usage row to the
run that caused it, and the per-run ceiling silently measured zero.

The invoke response returns that chat id. Recording it against the run is
enough: llm_usage is keyed by chat_id, so a run's cost is a join. No change
to Sinas is needed.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_llm_call",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", UUID(as_uuid=True),
                  sa.ForeignKey("query_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_run_llm_call_run", "run_llm_call", ["run_id"])
    op.create_index("ix_run_llm_call_chat", "run_llm_call", ["chat_id"])


def downgrade() -> None:
    op.drop_index("ix_run_llm_call_chat", table_name="run_llm_call")
    op.drop_index("ix_run_llm_call_run", table_name="run_llm_call")
    op.drop_table("run_llm_call")
