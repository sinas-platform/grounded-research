"""Document identity for deduplication: content hash, source identity,
duplicate marking.

Three identities per document, weakest last:
- content_hash on each version — exact-bytes identity (normalized content,
  sha256); the upload path rejects-as-idempotent on it.
- (source, external_ref) on the document — the ingesting connector's
  natural key (CELEX, ECLI, publication number, …); decides
  create-vs-new-version on re-ingestion.
- filename — the external_ref of last resort, unchanged behavior.

duplicate_of_id marks a document detected as an exact duplicate of an
earlier one; nothing is deleted, the duplicate is staged out of retrieval.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_version",
                  sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index("ix_document_version_content_hash", "document_version",
                    ["content_hash"])
    op.add_column("document", sa.Column("source", sa.String(200), nullable=True))
    op.add_column("document",
                  sa.Column("external_ref", sa.String(500), nullable=True))
    op.create_index(
        "uq_document_source_external_ref", "document",
        ["source", "external_ref"], unique=True,
        postgresql_where=sa.text("source IS NOT NULL AND external_ref IS NOT NULL"))
    op.add_column("document", sa.Column(
        "duplicate_of_id", sa.dialects.postgresql.UUID(as_uuid=True),
        sa.ForeignKey("document.id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.drop_column("document", "duplicate_of_id")
    op.drop_index("uq_document_source_external_ref", table_name="document")
    op.drop_column("document", "external_ref")
    op.drop_column("document", "source")
    op.drop_index("ix_document_version_content_hash",
                  table_name="document_version")
    op.drop_column("document_version", "content_hash")
