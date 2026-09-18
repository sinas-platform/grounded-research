"""How many documents actually write about an entity.

`entity_stats.documents` counts every document a mention of the entity sits
in, and for a word that became an entity that is most of the corpus: the
gazetteer matches the word everywhere. Ranked by that count, the corpus
profile's examples were the gazetteer's junk — the planner was told the
collection's companies were "Case, Parties, Only, Lang, This".

`recognised_documents` counts only the documents in which something other
than a blind string match found the entity: an extractor that read the
document, an identifier, a curated alias. Ranked by this, the same list
reads "EDF, SFR, IBM, BMW, TF1". Filled by the same refresh that fills the
other two columns (`generic_entities.refresh_entity_stats`), in the same
grouped scan, so it costs nothing extra to keep.

Its own migration rather than a change to 0049: that one is merged, and a
merged migration is never edited — an environment that has run it would
silently lack the column.

Revision ID: 0051
Revises: 0050
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entity_stats",
        sa.Column("recognised_documents", sa.Integer(), nullable=False,
                  server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("entity_stats", "recognised_documents")
