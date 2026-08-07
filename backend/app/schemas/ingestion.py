from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

# The pipeline's parts, in execution order. A run may select a subset —
# e.g. only "relationships" to re-extract edges after a config change
# without re-paying metadata extraction. "extract" is the classify+
# metadata+mentions LLM pass and the only part that wipes previous
# artifacts before writing. "dossiers" is a free no-op when no dossier
# classes are configured.
Part = Literal["extract", "ground", "resolve", "relationships", "dossiers"]
ALL_PARTS: tuple[str, ...] = ("extract", "ground", "resolve", "relationships", "dossiers")


class RunFilter(BaseModel):
    """How to select documents for the run.

    Empty filter = all documents. Combine fields = AND.
    Unknown fields are REJECTED: a typo'd filter must fail loudly, not
    silently select the whole corpus (which is what an ignored field
    plus the empty-filter default would do).
    """

    model_config = {"extra": "forbid"}

    document_ids: list[uuid.UUID] | None = None
    # Cap the selection (applied after all other clauses; ordered by created_at).
    limit: int | None = None
    document_class_ids: list[uuid.UUID] | None = None
    include_unclassified: bool = False
    # Upper bound on classification_confidence — used to target the "doubtful"
    # docs for reclassification. Combines additively with the class clauses
    # (same OR group): "class A OR unclassified OR confidence ≤ 0.6".
    max_classification_confidence: float | None = None
    # Staged docs are uploaded but skipped the auto-pipeline. Two knobs:
    #   include_staged=true → staged docs are added alongside the rest.
    #   staged_only=true    → only staged docs (short-circuits the class group).
    # Default behavior depends on the runner: ingestion excludes staged,
    # discovery/FM-suggest includes staged.
    include_staged: bool = False
    staged_only: bool = False
    created_since: datetime | None = None
    created_until: datetime | None = None


class RunCreateIn(BaseModel):
    """The filter selects documents; `parts` selects work (None = the full
    pipeline). There is no `stages` field anymore — the former stage
    architecture is gone.

    Unknown fields are REJECTED. In particular a caller still sending the
    old `stages` key must get a 422, not a silent full-pipeline run at
    ~5× the cost of the parts it thought it asked for (bitten in the
    wild on 7 Aug: grove_sink sent stages=["oneshot"] and paid for
    everything). Same fail-loudly rule as RunFilter."""

    model_config = {"extra": "forbid"}

    parts: list[Part] | None = None
    filter: RunFilter = Field(default_factory=RunFilter)
    dry_run: bool = False  # if true, returns the count without creating work


class RunCreateOut(BaseModel):
    run_id: uuid.UUID | None = None
    document_count: int
    unit_count: int  # docs × stages
    status: str  # "started" | "would_start" (dry-run)


class RunOut(ORMModel):
    id: uuid.UUID
    status: str
    # The selected pipeline parts (stored in the run row's legacy `stages`
    # column; exposed under the name that matches what it now holds).
    parts: list[str] = Field(validation_alias="stages")
    filter: dict
    total_units: int
    done_units: int
    failed_units: int
    error: str | None = None
    started_by: uuid.UUID
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class RunUnitOut(ORMModel):
    id: uuid.UUID
    run_id: uuid.UUID
    document_id: uuid.UUID
    stage: str
    status: str
    error: str | None = None
    attempts: int
    chat_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
