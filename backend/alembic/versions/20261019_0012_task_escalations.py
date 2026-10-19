"""task escalations: escalations table, escalation:run

ADR-002 Phase 8, item 12. `escalations` (tenant-scoped, usual policy) holds one row per escalation step of an overdue
task, unique per (task, level), so repeated or concurrent runs escalate each step once. It holds ids, category,
priority, unit and delivery state only, no PHI. New permission `escalation:run` for the `interface` role (a hospital
scheduler calling POST /api/escalations/run with its API key); admin holds every permission.

Nothing changes for existing rows. Escalations only start once something calls the run endpoint (CRON_SECRET set on
Vercel, an integration client, an admin, or `python -m app.cli escalate`).

Revision ID: 0012
Revises: 0010
Create Date: 2026-10-19 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copy (migrations must not change when app code does).
ROWS = [("admin", "escalation:run"), ("interface", "escalation:run")]


def upgrade() -> None:
    op.create_table('escalations',
    sa.Column('escalation_id', sa.String(length=40), nullable=False),
    sa.Column('task_id', sa.String(length=40), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('category', sa.String(length=40), nullable=False),
    sa.Column('unit_id', sa.String(length=40), nullable=True),
    sa.Column('target_role', sa.String(length=20), nullable=False),
    sa.Column('target_user_id', sa.String(length=40), nullable=True),
    sa.Column('recipients', sa.Integer(), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('page_status', sa.String(length=20), nullable=False),
    sa.Column('page_attempts', sa.Integer(), nullable=False),
    sa.Column('paged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.task_id'], name=op.f('fk_escalations_task_id_tasks'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_escalations_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('escalation_id', name=op.f('pk_escalations')),
    sa.UniqueConstraint('task_id', 'level', name='uq_escalations_task_level')
    )
    with op.batch_alter_table('escalations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_escalations_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_escalations_tenant_id'), ['tenant_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON escalations TO {APP_ROLE}")
        op.execute("ALTER TABLE escalations ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON escalations "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON escalations")
        op.execute(f"REVOKE ALL ON escalations FROM {APP_ROLE}")
    with op.batch_alter_table('escalations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_escalations_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_escalations_task_id'))
    op.drop_table('escalations')
