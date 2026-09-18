"""document_class.filename_rules

How a document of this class can be recognised from its filename alone, as a
list of {pattern, confidence, reason}, written from the package's
`filename_rules` on the class.

The ingestion one-shot carried this as `CLASS_RULES`: five filename patterns
mapped to four document-class names one deployment chose. Free and instant is
the right first rung of the classification ladder, but which filenames mean
what is knowledge about a collection — a regulator's register scheme, a
feed's numeric ids — and the engine cannot have it without naming a
deployment. Every other deployment got no rule hits and paid for a model call
on every document, with nothing saying why.

NULL on every existing row. A class with no rules is not recognisable by
filename, which is what most classes are and what every class was before this
column existed.

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("filename_rules", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "filename_rules")
