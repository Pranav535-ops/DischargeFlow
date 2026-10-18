"""downtime procedure: downtime_periods, backfilled_at on tasks and workflows, downtime:manage

ADR-002 Phase 7, item 6. `downtime_periods` (tenant-scoped, usual policy) holds read-only periods an admin opens and
closes, and unplanned outages recorded afterwards; a partial unique index allows one open period per tenant. While
one is open the API refuses writes. `tasks.backfilled_at` and `workflows.backfilled_at` mark work done on paper and
entered afterwards (completed_at keeps the real time). New permission `downtime:manage` (admin).

Nothing changes for existing rows: no period is open, and backfilled_at is NULL.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19 15:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copy (migrations must not change when app code does).
ROWS = [("admin", "downtime:manage")]
OPEN_SQL = sa.text("ended_at IS NULL")


def upgrade() -> None:
    op.create_table('downtime_periods',
    sa.Column('downtime_id', sa.String(length=40), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_by', sa.String(length=40), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_by', sa.String(length=40), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_downtime_periods_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('downtime_id', name=op.f('pk_downtime_periods'))
    )
    with op.batch_alter_table('downtime_periods', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_downtime_periods_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index('uq_downtime_periods_one_open', ['tenant_id'], unique=True,
                              postgresql_where=OPEN_SQL, sqlite_where=OPEN_SQL)

    # Plain ADD COLUMN, not batch mode: on SQLite batch mode rebuilds the table, and dropping the old `workflows`
    # cascade-deletes its tasks. Nullable columns without constraints are added in place.
    op.add_column('tasks', sa.Column('backfilled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('workflows', sa.Column('backfilled_at', sa.DateTime(timezone=True), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON downtime_periods TO {APP_ROLE}")
        op.execute("ALTER TABLE downtime_periods ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON downtime_periods "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON downtime_periods")
        op.execute(f"REVOKE ALL ON downtime_periods FROM {APP_ROLE}")
    # SQLite 3.35+ drops a column in place.
    op.drop_column('workflows', 'backfilled_at')
    op.drop_column('tasks', 'backfilled_at')
    with op.batch_alter_table('downtime_periods', schema=None) as batch_op:
        batch_op.drop_index('uq_downtime_periods_one_open', sqlite_where=OPEN_SQL, postgresql_where=OPEN_SQL)
        batch_op.drop_index(batch_op.f('ix_downtime_periods_tenant_id'))
    op.drop_table('downtime_periods')
