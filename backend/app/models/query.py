"""Query runs — server-supervised question pipelines.

A QueryRun is the query-side sibling of IngestionRun: one row per question,
driven end-to-end by `services/query_runner.py` with the choreography in
SGR and only judgment delegated to agents. Sub-search chats, the parent
result, the answer, and per-stage timings all hang off this row, so a run is
resumable and auditable from the database alone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import OwnedMixin, TimestampMixin, uuid_pk

# status lifecycle:
#   pending → retrieving → synthesizing → validating
#           → published | partial | failed
# "partial" is a terminal outcome with a client-facing note instead of a full
# answer (budget ceiling, thin coverage); "failed" is infra and resumable.
# decomposing/searching/merging belong to the retired agent-driven retrieval
# path and only appear on historical runs.
QUERY_RUN_STATUSES = (
    "pending",
    "retrieving",
    "decomposing",
    "searching",
    "merging",
    "synthesizing",
    "validating",
    "published",
    "partial",
    "failed",
)


class QueryRun(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "query_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # "full" (question → answer), "retrieval" (stop at published parent
    # result), "synthesis" (existing parent_result_id → published answer)
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="full", server_default="full"
    )
    # How expansive the run may be; the runner translates this into hard
    # bounds (sub-query fan-out today; read depth is a candidate later).
    # low=1 sub-query, medium=2, high=3.
    effort: Mapped[str] = mapped_column(
        String(10), nullable=False, default="medium", server_default="medium"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    # ["sub-query", ...] — set by the decompose stage (or by the caller).
    subqueries: Mapped[list | None] = mapped_column(JSONB)
    # per-subquery supervision state:
    # {sub: {chat_id, started, nudges, redispatched, result_id}}
    searches: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    parent_result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    answer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    synthesis_chat_id: Mapped[str | None] = mapped_column(String(64))
    # fire relationship discovery asynchronously after the parent publishes
    run_discovery: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    error: Mapped[str | None] = mapped_column(Text)
    # {stage: {started, completed, detail…}} — the run's own flight recorder
    telemetry: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
