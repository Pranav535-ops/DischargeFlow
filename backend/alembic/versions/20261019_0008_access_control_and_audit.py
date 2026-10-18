"""access control: task override/assign and claim permissions, task override columns

ADR-001 Phase 7. Task status changes are limited to the assignee; `task:override` (physician, admin) lets someone
else change one with a reason, stored in the new `tasks.override_reason` / `override_by`. Naming an assignee needs
`task:assign` (physician, admin). Creating claims and changing their status needs `claim:manage` (nurse, admin).

The no-access role `pending` (new, unapproved sign-ins) needs no rows: it holds no permissions. Existing users keep
their roles, so the production admin is unaffected.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 09:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy (migrations must not change when app code does).
ROWS = [
    ("admin", "claim:manage"), ("nurse", "claim:manage"),
    ("admin", "task:assign"), ("physician", "task:assign"),
    ("admin", "task:override"), ("physician", "task:override"),
]


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('override_reason', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('override_by', sa.String(length=40), nullable=True))
    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('override_by')
        batch_op.drop_column('override_reason')
