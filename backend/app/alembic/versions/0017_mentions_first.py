"""Mentions-first entity model — identity becomes a re-computable link.

entity_mention becomes ground truth: the surface text as written, the
document-local resolved form, and a type — with entity_id now NULLABLE.
Linking a mention to a canonical entity is an annotation performed by the
resolver (services/entity_resolver), carrying method + confidence +
evidence, and can be re-run at any time.

entity gains natural_key (package-declared identity patterns, e.g. case
numbers) and merged_into_id (merge tombstone — merges become reversible).

Legacy rows are backfilled: existing mentions keep their entity link,
marked link_method='legacy' with the entity's canonical form as both
surface and resolved form (the old pipeline never stored the actual text).

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── entity_mention: ground-truth columns ────────────────────────────
    op.add_column("entity_mention", sa.Column("surface_form", sa.String(500)))
    op.add_column("entity_mention", sa.Column("resolved_form", sa.String(500)))
    op.add_column(
        "entity_mention",
        sa.Column(
            "entity_type_id",
            UUID(as_uuid=True),
            sa.ForeignKey("entity_type.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("entity_mention", sa.Column("link_method", sa.String(20)))
    op.add_column("entity_mention", sa.Column("link_confidence", sa.Float))
    op.add_column("entity_mention", sa.Column("link_evidence", JSONB))
    op.alter_column("entity_mention", "entity_id", nullable=True)
    op.create_index(
        "ix_entity_mention_unlinked",
        "entity_mention",
        ["document_id"],
        postgresql_where=sa.text("entity_id IS NULL"),
    )

    # ── entity: identity key + merge tombstone ──────────────────────────
    op.add_column("entity", sa.Column("natural_key", sa.String(300)))
    op.add_column(
        "entity",
        sa.Column(
            "merged_into_id",
            UUID(as_uuid=True),
            sa.ForeignKey("entity.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_entity_natural_key",
        "entity",
        ["entity_type_id", "natural_key"],
        unique=True,
        postgresql_where=sa.text("natural_key IS NOT NULL AND merged_into_id IS NULL"),
    )

    # ── backfill legacy mentions ────────────────────────────────────────
    op.execute(
        """
        UPDATE entity_mention em
        SET surface_form = e.canonical_form,
            resolved_form = e.canonical_form,
            entity_type_id = e.entity_type_id,
            link_method = 'legacy',
            link_confidence = coalesce(em.confidence, 0.7)
        FROM entity e
        WHERE em.entity_id = e.id AND em.surface_form IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_entity_natural_key", table_name="entity")
    op.drop_column("entity", "merged_into_id")
    op.drop_column("entity", "natural_key")
    op.drop_index("ix_entity_mention_unlinked", table_name="entity_mention")
    op.execute("DELETE FROM entity_mention WHERE entity_id IS NULL")
    op.alter_column("entity_mention", "entity_id", nullable=False)
    op.drop_column("entity_mention", "link_evidence")
    op.drop_column("entity_mention", "link_confidence")
    op.drop_column("entity_mention", "link_method")
    op.drop_column("entity_mention", "entity_type_id")
    op.drop_column("entity_mention", "resolved_form")
    op.drop_column("entity_mention", "surface_form")
