"""Change note on query_run — what is different about this version of a run.

`title` names the question and is the same on every rerun of it. This says
what changed in THIS one: "Round 3 — citation and coverage fixes".

A reviewer opening version three of a question otherwise has to work out for
themselves why it is in front of them again, and the reasons live in commit
messages and PR descriptions they cannot see. The review platform already
shows a line like this per version, and until now the text came from
whatever the import invented rather than from the run.

Free text and deliberately not derived from tags: "round-3" says which batch
a run belongs to, not what was done to it, and inferring the second from the
first would be inventing.

Nullable and editable after the fact, like `reference`, `tags` and `title`,
so a version already published can be given its note.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "query_run",
        sa.Column("change_note", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("query_run", "change_note")
