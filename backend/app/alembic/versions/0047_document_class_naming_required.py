"""A class says whether a claim resting on it must name it.

A review of published answers asked for the rule structurally rather than lexically:
every proposition carries a source, and where that source is one the field
treats as authority it must be named in the claim — the parties and the
reference — so a reader can look it up and weigh it. An empty field sends the
claim back.

Which classes those are is the deployment's to declare. The column defaults
to false, so a deployment that declares nothing holds nothing to the rule and
behaves exactly as before.

Revision ID: 0047
Revises: 0046
"""
import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("naming_required", sa.Boolean(), nullable=False,
                  server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("document_class", "naming_required")
