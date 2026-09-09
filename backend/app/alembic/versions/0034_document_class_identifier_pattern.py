"""document_class.identifier_pattern

The naming checks already read two columns of deployment knowledge off the
class: identifier_property names the property that identifies a document, and
attribution_cues holds the words that mark a claim as attributing to it. Both
arrive from the package rather than from code, because SGR is the platform and
the vocabulary belongs to the deployment.

The mismatch check needs one more piece of the same kind, and for the same
reason. To tell that a claim names one document and cites another it has to
find identifiers in prose, and the SHAPE of an identifier is deployment
knowledge: a court case number, a merger reference and an invoice number are
all identifiers and none of them can be recognised without knowing the scheme.
Hardcoding one would put a deployment's vocabulary in a repo that must not
name a deployment.

So the shape is declared: a regular expression whose capture groups are the
comparison key. Nullable, and a class that declares none is not judged by the
check, which is the same opt-in the other two columns give.

500 characters: a pattern is a line, not a document, and a bound keeps a
runaway value out of a column that is compiled on every answer.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("identifier_pattern", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_class", "identifier_pattern")
