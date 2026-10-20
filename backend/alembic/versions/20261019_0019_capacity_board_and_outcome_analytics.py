"""Capacity board and outcome analytics

ADR-002 Phase 9, items 17 and 19.

- `bed_turnovers`: one row per bed turnover (dirty -> cleaning -> available), opened when a stay ends in the bed.
  Tenant-scoped (usual policy) and unit-scoped by its own `unit_id` (RESTRICTIVE `unit_scope` policy, frozen copy of
  rls.LOCATION_VISIBLE). At most one open turnover per bed (partial unique index).
- `encounters.first_expected_discharge_date`: the first expected date a plan was given, for expected-date accuracy.
  Not backfilled: a date moved earlier records no delay reason, so the first date of existing plans is unknown.
- Permissions `capacity:read` (physician, nurse, admin) and `bed:manage` (nurse, interface, admin).

Every change is additive, so the previous release keeps working while the deploy rolls out.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-19 23:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copies (migrations must not change when app code does).
LOCATION_VISIBLE = ("current_setting('app.unit_scope', true) IS DISTINCT FROM 'on' OR unit_id IS NULL "
                    "OR unit_id = ANY (string_to_array(current_setting('app.unit_ids', true), ','))")
ROWS = [("physician", "capacity:read"), ("nurse", "capacity:read"), ("admin", "capacity:read"),
        ("nurse", "bed:manage"), ("interface", "bed:manage"), ("admin", "bed:manage")]


def upgrade() -> None:
    op.create_table('bed_turnovers',
    sa.Column('turnover_id', sa.String(length=40), nullable=False),
    sa.Column('unit_id', sa.String(length=40), nullable=True),
    sa.Column('room_number', sa.String(length=40), nullable=False),
    sa.Column('encounter_id', sa.String(length=40), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('dirty_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('cleaning_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cleaning_started_by', sa.String(length=40), nullable=True),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('available_by', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['encounter_id'], ['encounters.encounter_id'], name=op.f('fk_bed_turnovers_encounter_id_encounters'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_bed_turnovers_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['unit_id'], ['units.unit_id'], name=op.f('fk_bed_turnovers_unit_id_units'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('turnover_id', name=op.f('pk_bed_turnovers'))
    )
    with op.batch_alter_table('bed_turnovers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bed_turnovers_encounter_id'), ['encounter_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bed_turnovers_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_bed_turnovers_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bed_turnovers_unit_id'), ['unit_id'], unique=False)
        batch_op.create_index('uq_bed_turnovers_one_open_per_bed', ['tenant_id', 'unit_id', 'room_number'], unique=True, postgresql_where=sa.text("status IN ('dirty', 'cleaning')"), sqlite_where=sa.text("status IN ('dirty', 'cleaning')"))

    op.add_column('encounters', sa.Column('first_expected_discharge_date', sa.Date(), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON bed_turnovers TO {APP_ROLE}")
        op.execute("ALTER TABLE bed_turnovers ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON bed_turnovers "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
        op.execute(f"CREATE POLICY unit_scope ON bed_turnovers AS RESTRICTIVE USING ({LOCATION_VISIBLE}) "
                   "WITH CHECK (true)")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS unit_scope ON bed_turnovers")
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON bed_turnovers")
        op.execute(f"REVOKE ALL ON bed_turnovers FROM {APP_ROLE}")

    with op.batch_alter_table('encounters', schema=None) as batch_op:
        batch_op.drop_column('first_expected_discharge_date')

    with op.batch_alter_table('bed_turnovers', schema=None) as batch_op:
        batch_op.drop_index('uq_bed_turnovers_one_open_per_bed', postgresql_where=sa.text("status IN ('dirty', 'cleaning')"), sqlite_where=sa.text("status IN ('dirty', 'cleaning')"))
        batch_op.drop_index(batch_op.f('ix_bed_turnovers_unit_id'))
        batch_op.drop_index(batch_op.f('ix_bed_turnovers_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_bed_turnovers_status'))
        batch_op.drop_index(batch_op.f('ix_bed_turnovers_encounter_id'))

    op.drop_table('bed_turnovers')
