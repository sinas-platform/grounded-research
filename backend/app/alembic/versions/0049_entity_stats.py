"""What the corpus holds of one entity, computed out of band.

Two facts about an entity are read on the path of every question: in how
many documents it is mentioned, and whether anything other than a blind
string match ever recognised it. The first ranks the matches the planner is
shown and weights every anchor in retrieval; the second keeps an entity
nothing ever recognised from being force-picked as an anchor. Both were
computed per question, per entity, out of the mention table — in four
places — and on 22.7 million mentions that put nine to twelve minutes of
database time between two planner calls of 18 seconds each, growing with
every document ingested. A seed the planner writes as "Kestrel Holdings v
Northmoor Authority (T-123/45 P)" is split on its punctuation into fragments
like `45 P)`, each fragment matches thousands of entities, and each of those
was counted before the cut to six.

This table is those two facts, per entity, refreshed by the maintenance
pass in one statement over the mention table and read as a lookup — the
per-entity sibling of `corpus_profile`, which does the same per type, for
the same reason.

`documents` is a count as of `refreshed_at`, not a live figure; an entity
created since the last refresh has no row, reads as zero documents and
unrecognised, and sorts last until the next pass. That is the corpus
profile's staleness, accepted for the same reason.

Keyed on the entity, so a dropped entity takes its row with it.

Revision ID: 0049
Revises: 0048
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_stats",
        sa.Column("entity_id", UUID(as_uuid=True),
                  sa.ForeignKey("entity.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("documents", sa.Integer(), nullable=False,
                  server_default="0"),
        # Documents in which something other than a blind string match found
        # the entity. `documents` counts the gazetteer's hits too, which for a
        # word that became an entity is most of the corpus; this is the count
        # that says how much the entity is actually written about.
        sa.Column("recognised_documents", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("recognised", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("refreshed_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("entity_stats")
