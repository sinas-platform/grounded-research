"""document_class.name_property

The naming check asks whether a claim writes the identifier of the source it
relies on. House style does not always oblige: a judgment is named by its
parties, and a reader given `Just Eat/Hungryhouse` can follow it perfectly
well without the docket number. The check called those claims unnamed and was
wrong to, on 22 findings of 157 measured over the published answers.

Reading a name needs to know which property carries one, and that is
deployment knowledge of exactly the kind identifier_property and
attribution_cues already carry: a case title, a study title and a product name
are all names, and none of them can be found without being told where to look.

So the class declares it. Nullable, and a class declaring none is judged on
its identifier alone, which is where the check was before this column existed.

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
