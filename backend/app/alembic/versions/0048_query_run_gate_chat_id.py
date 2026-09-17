"""The review's conversation, alongside the drafter's.

Judging an answer was a fresh call per cycle, each one carrying the question,
the question's parts, the whole retrieved document set and every rule for
judging it — measured at 40,000 tokens a call, of which 39,266 were written to
the prompt cache and 732 read back. A freshly assembled body each cycle gives
the provider's rolling cache breakpoint nothing to roll onto, which is the
same defect the drafting conversation was built to fix and the same column
shape that fixed it.

Nullable, like `synthesis_chat_id`: a run that has not reached judging has no
conversation, and one that predates this has none either.

Revision ID: 0048
Revises: 0047
"""
from alembic import op
import sqlalchemy as sa

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("query_run",
                  sa.Column("gate_chat_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("query_run", "gate_chat_id")
