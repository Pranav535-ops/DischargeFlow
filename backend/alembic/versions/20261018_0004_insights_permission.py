"""insights:read permission

ADR-001 Phase 5, item 20: the admin-only AI insights panel. Admin holds every permission, so only its row is added.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy (migrations must not change when app code does).
ROWS = [("admin", "insights:read")]


def upgrade() -> None:
    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
