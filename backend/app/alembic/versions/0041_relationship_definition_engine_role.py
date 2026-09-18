"""relationship_definition.engine_role

Which engine-meaningful role a relationship definition carries, written from
the package's `spec.relationship_roles` block.

Engine features that need an edge of a particular MEANING were finding it by
the name a deployment happened to choose: a literal name list in the
superseded-authority check, a walk over names sharing a prefix for the
authority tier. A deployment naming its relations anything else got no check
and no tier, with no error and no log — the query matched nothing, which reads
downstream exactly like a corpus that records no supersession.

NULL on every existing row and on any definition a deployment gives no role,
which is the default and means "this definition is not one the engine looks
for".

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "relationship_definition",
        sa.Column("engine_role", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_relationship_definition_engine_role",
        "relationship_definition",
        ["engine_role"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_relationship_definition_engine_role",
        table_name="relationship_definition",
    )
    op.drop_column("relationship_definition", "engine_role")
