"""Property values: rewrap the one-shot's {"value": x} rows to {"_": x}.

The one-shot ingestion path wrote PropertyValue.value as {"value": x}
while the API unwrapper and the SQL filter layer read {"_": x}, so every
property it extracted was invisible to filtering and malformed through
the API. The write path is fixed alongside this migration; this heals
the rows any deployment wrote between the one-shot's merge (28 July)
and the fix.

Downgrade is a documented no-op: after the upgrade, rows this migration
rewrapped are indistinguishable from rows that were always correct, and
re-wrapping correct rows as {"value": x} would corrupt them. Nothing
reads the old shape, so there is nothing to restore.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-04
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE property_value
        SET value = jsonb_build_object('_', value->'value')
        WHERE value ? 'value' AND NOT value ? '_'
        """
    )


def downgrade() -> None:
    # Intentionally empty: see the module docstring. The old {"value": x}
    # shape was a bug no reader consumes; reverting would require knowing
    # which rows this migration touched, which is not recorded.
    pass
