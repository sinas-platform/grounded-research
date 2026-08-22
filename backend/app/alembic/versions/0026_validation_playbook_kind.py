"""Allow playbook kind `validation`.

Deployment configs can now ship corpus-specific guidance for the evidence
validator (what voice and modality checking look like in their documents),
the same way they ship retrieval and synthesis playbooks. The generic rules
stay in code; the domain examples arrive through this kind.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_CK = "ck_playbook_kind"


def upgrade() -> None:
    op.drop_constraint(_CK, "playbook", type_="check")
    op.create_check_constraint(
        _CK, "playbook", "kind IN ('retrieval', 'synthesis', 'validation')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM playbook WHERE kind = 'validation'")
    op.drop_constraint(_CK, "playbook", type_="check")
    op.create_check_constraint(
        _CK, "playbook", "kind IN ('retrieval', 'synthesis')"
    )
