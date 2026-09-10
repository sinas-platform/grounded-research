"""document.visibility

Documents carried only the owner-and-roles tenancy every table shares. That
is right for an upload, which is someone's, and wrong for the corpus, which
an operator ingests and every user is meant to read: a user without the
documents read-all permission was refused every source their own answer
cited.

`shared` is the corpus and the default; `private` is an upload. Every
existing row becomes shared, since until now every row was corpus. An
operator who has private uploads on a deployment marks them after upgrading.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column(
            "visibility",
            sa.String(length=10),
            nullable=False,
            server_default="shared",
        ),
    )
    op.create_check_constraint(
        "ck_document_visibility",
        "document",
        "visibility IN ('shared', 'private')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_document_visibility", "document", type_="check")
    op.drop_column("document", "visibility")
