"""A property and an annotation say what they mean to the engine.

The engine used to read meaning off a name: a property whose name contained
"date" was the source's date, one matching "jurisdiction|country|member_state"
was its jurisdiction, a citation's number was the first of four guessed keys
that happened to exist, the tier was the annotation literally called
"authority_tier" and the issuer the one called "issuing_body". A deployment
naming its own fields anything else got no currency note, no jurisdiction
note, no identifier in its citations and no tier — silently, because a guess
that matches nothing is indistinguishable from a corpus that holds nothing.

Both columns are nullable and carry no default: a role is the exception, not
the rule, and most fields are the deployment's own business. The import writes
them from the class's own declaration, so the meaning lives next to the field
it describes and two classes may use entirely different names for the same
role.

Revision ID: 0044
Revises: 0043
"""
import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class_property",
        sa.Column("engine_role", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_document_class_property_engine_role",
        "document_class_property",
        ["engine_role"],
    )
    op.add_column(
        "annotation_definition",
        sa.Column("engine_role", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_annotation_definition_engine_role",
        "annotation_definition",
        ["engine_role"],
    )


def downgrade() -> None:
    op.drop_index("ix_annotation_definition_engine_role",
                  table_name="annotation_definition")
    op.drop_column("annotation_definition", "engine_role")
    op.drop_index("ix_document_class_property_engine_role",
                  table_name="document_class_property")
    op.drop_column("document_class_property", "engine_role")
