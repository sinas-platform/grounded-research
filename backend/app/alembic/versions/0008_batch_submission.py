"""switch run workers to Sinas batch submission

Previously, SGR's discovery and ingestion workers invoked Sinas agents one
unit at a time using `get_admin_client()` (which requires SINAS_API_KEY). The
new pattern (Sinas 0.2.0+ batch endpoints):

  - At API create-run time we have `caller.sinas_token`; persist it on the
    run row so the worker / polling code can call Sinas as the originating
    user.
  - The worker submits one batch per stage (ingestion) or per kind (discovery)
    via `client.agents.submit_batch(...)` instead of N per-unit invocations.
  - Sinas owns dispatch and mints per-execution tokens; SGR just tracks
    `batch_id`s and polls aggregate status (or receives callbacks).

Two columns per run table:
  - `submitting_user_token` (text, nullable) — the bearer token captured at
    API time. Nullable so existing rows still parse; new rows will populate.
  - `sinas_batch_ids` (jsonb, defaults to `{}`) — mapping of stage/phase
    name to Sinas `batch_id`. For discovery: `{"scan": "...", "consolidate":
    "..."}`. For ingestion: `{"classifier": "...", "summarizer": "...", ...}`.

See ADR `sinas/backend/docs/adrs/2026-05-14-external-app-bulk-enqueue.md`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("discovery_run", "ingestion_run"):
        op.add_column(
            table,
            sa.Column("submitting_user_token", sa.Text(), nullable=True),
        )
        op.add_column(
            table,
            sa.Column(
                "sinas_batch_ids",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    # Per-unit Sinas execution id, so we can reconcile each unit against the
    # batch's child executions.
    for table in ("discovery_run_unit", "ingestion_run_unit"):
        op.add_column(
            table,
            sa.Column("sinas_execution_id", sa.String(length=64), nullable=True),
        )
        op.create_index(
            f"ix_{table}_sinas_exec",
            table,
            ["sinas_execution_id"],
            unique=False,
            postgresql_where=sa.text("sinas_execution_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in ("discovery_run_unit", "ingestion_run_unit"):
        op.drop_index(f"ix_{table}_sinas_exec", table_name=table)
        op.drop_column(table, "sinas_execution_id")
    for table in ("discovery_run", "ingestion_run"):
        op.drop_column(table, "sinas_batch_ids")
        op.drop_column(table, "submitting_user_token")
