"""document_class.name_property

A claim can name its source two ways: by an identifier, which the class
already declares through identifier_property and identifier_pattern, or in
prose, by the document's name. The naming check only understood the first, so
a claim writing "in Ferriere Nord v Commission the Court held" was reported as
naming nothing, and that is the largest group of wrong findings the check
produces.

Which property holds the name is deployment knowledge, so it is declared
rather than assumed. `key_replay.backfill_from_properties` already takes a
name_property as a parameter for the same reason: the caller names the
property, the platform does not guess it.

Nullable and independent of the identifier columns. A class may be identified
by number and never named in words, or named and never numbered.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("name_property", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "name_property")
