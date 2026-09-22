"""document_class.declared_properties

Which front-matter keys a class reads directly, and whether the header is the
source of truth for each. Until this revision front matter was parsed at
upload and never consulted again: ingestion asked a model to transcribe it
instead, so a stored value could disagree with the document's own header. A
field the model has to decide about, such as the date, is where that goes
wrong, and it is the field recency is decided on.

JSONB and nullable, holding a list of {key, property, on_conflict}. A class
that declares nothing behaves exactly as it does today.

Renumbered from 0036 when it met the merged claim_evidence.quote revision of
the same number: two revisions cannot share an id, and this one had not been
applied anywhere yet.

Revision ID: 0037
Revises: 0036
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("declared_properties", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "declared_properties")
