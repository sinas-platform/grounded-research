"""A document class says how high a source of it stands.

The engine can already say what a source IS — a class declares an
`authority_label` and the drafter is shown it — and labelling a source was
never the same as ranking it. An expert review of published answers found
general propositions resting on secondary material describing a decision
while the decision itself sat in the same retrieved set: the label made the
defect visible and changed nothing about it.

Ranking needs an ordering, and an ordering is the deployment's knowledge, not
the engine's. So the class carries a rank: 1 stands highest, larger numbers
stand lower, and equal numbers stand equally. NULL is the default and means
UNRANKED — a class nobody ranked is inert, and a source of it neither
satisfies the rule that a general proposition rests on the highest-standing
retrieved source that carries it nor breaches it.

Nullable with no default, like `engine_role` before it: a rank is a statement
a deployment makes, and a default would be the engine making it instead.

Not to be confused with the annotation carrying the `standing_tier` role,
which is derived per DOCUMENT from a walk over the graph. This is per CLASS,
declared rather than derived, and available without any relationship at all.

Revision ID: 0045
Revises: 0044
"""
import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("standing", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "standing")
