from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    grove_database_url: str = Field(
        default="postgresql+asyncpg://grove:grove@localhost:5432/grove",
        validation_alias="GROVE_DATABASE_URL",
    )

    # Server
    grove_port: int = Field(default=8080, validation_alias="GROVE_PORT")
    grove_log_level: str = Field(default="INFO", validation_alias="GROVE_LOG_LEVEL")
    grove_cors_origins: str = Field(default="", validation_alias="GROVE_CORS_ORIGINS")

    # Auth
    grove_auth_mode: Literal["sinas", "simplified"] = Field(
        default="sinas", validation_alias="GROVE_AUTH_MODE"
    )

    # Sinas integration
    sinas_url: str = Field(default="http://localhost:8000", validation_alias="SINAS_URL")

    # Required when GROVE_AUTH_MODE=simplified — the Sinas API key Grove uses
    # for ALL Sinas callbacks (skills, files, etc) and to derive the single
    # admin identity via /auth/me. Unused in `sinas` mode (per-user tokens
    # carry their own auth).
    sinas_api_key: str = Field(default="", validation_alias="SINAS_API_KEY")

    # Bulk-rerun worker concurrency cap. Caps simultaneous agent invocations
    # the runner makes to Sinas. Higher = faster but burns LLM budget faster.
    grove_rerun_concurrency: int = Field(
        default=5, validation_alias="GROVE_RERUN_CONCURRENCY"
    )

    # ── question runs (see docs/operations-bulk-and-runs.md) ──
    # How answers are drafted: an argument plan, verbatim passage extraction
    # verified against the source text, then one drafting call.
    #
    # The older agent-chat drafting loop can no longer be selected. It handed
    # the drafter the document manifest — summaries, classes, annotations —
    # which are interpretation generated at ingestion and verified against
    # nothing, so a claim could assert what a summary said with no passage
    # behind it. Grounding is on raw source text only. The loop's code is
    # still present and is now unreachable; it should be deleted.
    grove_draft_mode: Literal["extract"] = Field(
        default="extract", validation_alias="GROVE_DRAFT_MODE"
    )
    # Hard per-run spend ceiling in USD, summed over the run's LLM usage and
    # checked at every supervision poll. A run that crosses it ends "partial"
    # with whatever it has verified, never mid-write.
    grove_run_cost_cap_usd: float = Field(
        default=10.0, validation_alias="GROVE_RUN_COST_CAP_USD"
    )
    # Directory holding benchmark fixtures for the retrieval engine.
    # Empty = ~/grove-benchmark.
    grove_bench_dir: str = Field(default="", validation_alias="GROVE_BENCH_DIR")

    # ── what this deployment holds (prompt framing only) ──
    # Grove is domain-neutral; these are the only place a deployment tells
    # the models what kind of corpus and reader they are serving. No
    # behaviour branches on them — they change wording, not logic.
    #
    # Adjective for the corpus domain, e.g. "legal", "clinical", "regulatory".
    # Empty keeps the planner's framing generic.
    grove_domain: str = Field(default="", validation_alias="GROVE_DOMAIN")
    # Who a client-facing note is written for, e.g. "a legal researcher".
    grove_audience: str = Field(
        default="a researcher", validation_alias="GROVE_AUDIENCE"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.grove_cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
