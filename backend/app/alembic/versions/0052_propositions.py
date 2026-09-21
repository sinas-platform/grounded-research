"""A class says it carries propositions, and a table holds them.

A proposition is a statement a document establishes, applies or decides —
a judgment's holdings, a guideline's rules, a paper's findings — one
sentence, in the collection's working language whatever the document's,
with the line span it rests on. Retrieval matches a question's hypotheses
against these rather than against whole documents or their summaries.

Measured before this existed, on one collection of 144,200 documents: a
summary of a long judgment reads like the summary of every other judgment
on the subject, and the words that distinguish the document sit in its
paragraphs, so neither a summary index nor summary vectors reached the
documents an expert review asked for; full-text words reached them in the
document's own language only. Propositions extracted per document reached
them from a rule written in the working language, and from a French, a
Dutch or a Hungarian document alike.

Which classes carry propositions is the deployment's to say, exactly as
`authority_label`, `naming_required` and `standing` are: the engine holds
no list. `document_class.propositions` is that declaration, false by
default, so a class nobody declared is left alone.

The table keys on the document VERSION, because a line span is a fact
about one text: the maintenance pass writes a new version when it
normalises a wall of text, and propositions extracted from the old one
would then point at the wrong lines. A generated tsvector column serves
the word index; the vector index is a later migration, once the extension
that serves it is part of every deployment's database.

Revision ID: 0052
Revises: 0051
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_class",
        sa.Column("propositions", sa.Boolean(), nullable=False,
                  server_default="false"),
    )
    op.create_table(
        "proposition",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_version.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("line_from", sa.Integer(), nullable=True),
        sa.Column("line_to", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.execute("""
        ALTER TABLE proposition
        ADD COLUMN text_tsvector tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
    """)
    op.create_index("ix_proposition_document_id", "proposition", ["document_id"])
    op.create_index("ix_proposition_version_id", "proposition",
                    ["document_version_id"])
    op.create_index("ix_proposition_tsv", "proposition", ["text_tsvector"],
                    postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("proposition")
    op.drop_column("document_class", "propositions")
