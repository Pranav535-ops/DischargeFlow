"""summary delivery: delivery_endpoints, discharge_recipients, summary_deliveries, delivery:* permissions

ADR-002 Phase 9, item 16. Outbound delivery of the signed discharge summary (FHIR R4 document Bundle or C-CDA) to
the primary care physician or receiving facility, tracked per delivery.

- `delivery_endpoints` (tenant-scoped): the admin-managed directory of receivers. No PHI.
- `discharge_recipients` (tenant- and unit-scoped through the patient): which receivers get one stay's summary.
- `summary_deliveries` (tenant- and unit-scoped): the outbox. Status, attempts, next attempt, error codes; no PHI.
- Permissions: `delivery:configure` (admin), `delivery:manage` (nurse, admin), `delivery:run` and
  `delivery:acknowledge` (interface, admin).

Nothing changes for existing rows: nothing is sent until an admin adds an endpoint and a stay gets a recipient.

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
SCOPE_OFF = "current_setting('app.unit_scope', true) IS DISTINCT FROM 'on'"
TABLES = ('delivery_endpoints', 'discharge_recipients', 'summary_deliveries')
UNIT_SCOPED = ('discharge_recipients', 'summary_deliveries')
# Frozen copy (migrations must not change when app code does).
ROWS = [
    ("admin", "delivery:configure"), ("admin", "delivery:manage"), ("nurse", "delivery:manage"),
    ("admin", "delivery:run"), ("interface", "delivery:run"),
    ("admin", "delivery:acknowledge"), ("interface", "delivery:acknowledge"),
]


def upgrade() -> None:
    op.create_table('delivery_endpoints',
    sa.Column('endpoint_id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('document_format', sa.String(length=20), nullable=False),
    sa.Column('transport', sa.String(length=20), nullable=False),
    sa.Column('url', sa.String(length=2048), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_delivery_endpoints_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('endpoint_id', name=op.f('pk_delivery_endpoints'))
    )
    with op.batch_alter_table('delivery_endpoints', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_delivery_endpoints_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('discharge_recipients',
    sa.Column('recipient_id', sa.String(length=40), nullable=False),
    sa.Column('encounter_id', sa.String(length=40), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('endpoint_id', sa.String(length=40), nullable=False),
    sa.Column('added_by', sa.String(length=40), nullable=False),
    sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['encounter_id'], ['encounters.encounter_id'], name=op.f('fk_discharge_recipients_encounter_id_encounters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['endpoint_id'], ['delivery_endpoints.endpoint_id'], name=op.f('fk_discharge_recipients_endpoint_id_delivery_endpoints'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_discharge_recipients_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_discharge_recipients_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('recipient_id', name=op.f('pk_discharge_recipients')),
    sa.UniqueConstraint('encounter_id', 'endpoint_id', name='uq_discharge_recipients_encounter_endpoint')
    )
    with op.batch_alter_table('discharge_recipients', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discharge_recipients_encounter_id'), ['encounter_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_discharge_recipients_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_discharge_recipients_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('summary_deliveries',
    sa.Column('delivery_id', sa.String(length=40), nullable=False),
    sa.Column('summary_id', sa.String(length=40), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('workflow_id', sa.String(length=40), nullable=False),
    sa.Column('encounter_id', sa.String(length=40), nullable=True),
    sa.Column('endpoint_id', sa.String(length=40), nullable=False),
    sa.Column('trigger_id', sa.String(length=40), nullable=False),
    sa.Column('document_format', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=80), nullable=True),
    sa.Column('last_http_status', sa.Integer(), nullable=True),
    sa.Column('document_sha256', sa.String(length=64), nullable=True),
    sa.Column('remote_id', sa.String(length=300), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['endpoint_id'], ['delivery_endpoints.endpoint_id'], name=op.f('fk_summary_deliveries_endpoint_id_delivery_endpoints'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_summary_deliveries_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['summary_id'], ['discharge_summaries.summary_id'], name=op.f('fk_summary_deliveries_summary_id_discharge_summaries'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_summary_deliveries_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('delivery_id', name=op.f('pk_summary_deliveries')),
    sa.UniqueConstraint('summary_id', 'endpoint_id', 'trigger_id', name='uq_summary_deliveries_trigger')
    )
    with op.batch_alter_table('summary_deliveries', schema=None) as batch_op:
        batch_op.create_index('ix_summary_deliveries_due', ['status', 'next_attempt_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_summary_deliveries_endpoint_id'), ['endpoint_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_summary_deliveries_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_summary_deliveries_summary_id'), ['summary_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_summary_deliveries_tenant_id'), ['tenant_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       "USING (tenant_id = current_setting('app.tenant_id', true)) "
                       "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
        for table in UNIT_SCOPED:
            op.execute(f"CREATE POLICY unit_scope ON {table} AS RESTRICTIVE USING ({SCOPE_OFF} OR EXISTS "
                       f"(SELECT 1 FROM patients p WHERE p.patient_id = {table}.patient_id)) WITH CHECK (true)")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        for table in UNIT_SCOPED:
            op.execute(f"DROP POLICY IF EXISTS unit_scope ON {table}")
        for table in TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    with op.batch_alter_table('summary_deliveries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_summary_deliveries_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_summary_deliveries_summary_id'))
        batch_op.drop_index(batch_op.f('ix_summary_deliveries_patient_id'))
        batch_op.drop_index(batch_op.f('ix_summary_deliveries_endpoint_id'))
        batch_op.drop_index('ix_summary_deliveries_due')

    op.drop_table('summary_deliveries')
    with op.batch_alter_table('discharge_recipients', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discharge_recipients_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_discharge_recipients_patient_id'))
        batch_op.drop_index(batch_op.f('ix_discharge_recipients_encounter_id'))

    op.drop_table('discharge_recipients')
    with op.batch_alter_table('delivery_endpoints', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_delivery_endpoints_tenant_id'))

    op.drop_table('delivery_endpoints')
