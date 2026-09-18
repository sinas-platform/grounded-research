"""document_class.authority_label

What a claim citing a document of this class says about the source, in the
words a reader needs. Until now the drafter was asked for it: the prompt
listed eight values and told the model to pick one per claim. Those eight
words were a vocabulary in engine code, which is knowledge about one
collection, and asking for them cost output the drafter never had to spend —
the label follows from the document's class, which is already stored.

Null is the default and means the class needs no label: an unlabelled source
carries a rule on its own, and a labelled one does not.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("authority_label", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "authority_label")
