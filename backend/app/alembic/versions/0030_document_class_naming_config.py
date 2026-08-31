"""Two configuration fields on document_class, for the source-naming check.

The check asks whether the first claim that attributes something to a source
also names it. It cannot answer either half on its own: what identifies a
document, and what counts as attributing, are facts about a domain that a
deployment configures, not facts about the platform.

identifier_property names one of the class's own properties, by name. The
value of that property on a document is what a claim has to carry.

attribution_cues is a list of words. A claim carrying one is treated as
attributing rather than merely describing.

Both nullable, and a class that declares neither is not checked, so every
existing deployment keeps its current behaviour until it opts in.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("identifier_property", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "document_class",
        sa.Column("attribution_cues", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "attribution_cues")
    op.drop_column("document_class", "identifier_property")
