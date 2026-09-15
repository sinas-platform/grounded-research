"""document_class.declared_properties

Which front-matter keys a class reads directly, and whether the header is the
source of truth for each. Front matter is parsed at upload and never consulted
again: ingestion asks a model to transcribe it instead, and on one corpus that
cost 693 disagreements on the decision date out of 20,688, against 0 in 14,318
on language and 0 in 5,215 on CELEX. The field a model has to decide about is
the one it gets wrong, and it is the field recency is decided on.

JSONB and nullable, holding a list of {key, property, on_conflict}. A class
that declares nothing behaves exactly as it does today.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("declared_properties", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "declared_properties")
