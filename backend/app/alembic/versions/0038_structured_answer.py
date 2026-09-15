"""Structured answer: sections, question parts, tests, source labels.

An answer was a flat list of independent claim rows, and every reader printed
one paragraph per row. Expert review of six answers found the same defects
in each: the conclusion missing or last, later parts of a multi-part question
thin, a test that a source states as ordered conditions flattened into one
sentence, every source at one level, superseded law and other-jurisdiction
sources presented as current. The rows carried nothing a renderer could use
to do better.

Each claim now says where it belongs and what it is: the section it renders
in (conclusion, analysis, authority), the part of the question it answers,
its position within that section and part, and a kind that extends the old
claim_type with `test` and `label`. A test claim carries its conditions as
JSON, in the order the source states them. Four columns say how far a claim's
source can be trusted for the proposition: how the drafter classified it, its
tier in the deployment's authority hierarchy, whether its jurisdiction differs
from the question's, and whether the instrument is still current. A claim
that is a reasoning step rather than a reading of a passage — an inference,
or a conclusion — says which claims it follows from.

The answer carries the decomposition the drafter worked from and the date the
law is stated as at. An evidence span carries the source paragraph it pins.

All nullable and additive: rows written before this keep working and render as
before, and nothing downstream requires any of them.

Revision ID: 0037
Revises: 0036
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("answer_claim", sa.Column("section", sa.String(length=20), nullable=True))
    op.add_column("answer_claim", sa.Column("part_index", sa.Integer(), nullable=True))
    op.add_column("answer_claim", sa.Column("part_label", sa.String(length=300), nullable=True))
    op.add_column("answer_claim", sa.Column("position", sa.Integer(), nullable=True))
    op.add_column("answer_claim", sa.Column("claim_kind", sa.String(length=50), nullable=True))
    op.add_column("answer_claim", sa.Column("test", JSONB(), nullable=True))
    op.add_column(
        "answer_claim", sa.Column("authority_label", sa.String(length=40), nullable=True)
    )
    op.add_column("answer_claim", sa.Column("authority_tier", sa.Integer(), nullable=True))
    op.add_column(
        "answer_claim", sa.Column("jurisdiction_note", sa.String(length=300), nullable=True)
    )
    op.add_column(
        "answer_claim", sa.Column("currency_note", sa.String(length=500), nullable=True)
    )
    # The claims this one reasons from: a JSON list of claim ids. An
    # inference claim ("because X and Y, Z follows") carries no span of its
    # own and rests on these instead; a conclusion names what it concludes
    # from. Null on every row from before, and on claims that rest on
    # passages alone.
    op.add_column("answer_claim", sa.Column("follows_from", JSONB(), nullable=True))
    op.add_column("answer", sa.Column("law_stated_as_at", sa.Date(), nullable=True))
    op.add_column("answer", sa.Column("question_parts", JSONB(), nullable=True))
    # The answer as prose, assembled from the rows at publish by the generic
    # renderer. Stored so a reader gets one document; regenerable from the
    # rows at any time, so nothing depends on it.
    op.add_column("answer", sa.Column("rendered_markdown", sa.Text(), nullable=True))
    op.add_column(
        "claim_evidence", sa.Column("paragraph_ref", sa.String(length=50), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("claim_evidence", "paragraph_ref")
    op.drop_column("answer", "rendered_markdown")
    op.drop_column("answer", "question_parts")
    op.drop_column("answer", "law_stated_as_at")
    for col in ("follows_from", "currency_note", "jurisdiction_note", "authority_tier",
                "authority_label",
                "test", "claim_kind", "position", "part_label", "part_index", "section"):
        op.drop_column("answer_claim", col)
