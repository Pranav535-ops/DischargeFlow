"""track time workflows spend in pending_approval

ADR-001 Phase 6, item 22: operational metrics report how long discharges wait for physician approval.
`pending_approval_since` is set on entering pending_approval; `pending_approval_seconds` accumulates each stay.

Existing rows keep `pending_approval_seconds` NULL ("not tracked"), so the metric never reports an invented zero
for discharges that happened before this migration. No backfill: the transitions were never recorded.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18 21:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflows', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pending_approval_since', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('pending_approval_seconds', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workflows', schema=None) as batch_op:
        batch_op.drop_column('pending_approval_seconds')
        batch_op.drop_column('pending_approval_since')
