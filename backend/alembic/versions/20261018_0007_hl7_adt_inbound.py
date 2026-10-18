"""hl7 adt inbound: patient MRN with blind index, integration clients, message log, adt:ingest

ADR-001 Phase 6, item 24. Patients gain an encrypted MRN (`mrn`), its assigning authority and `mrn_index`, a keyed
HMAC that is the only way to look a patient up by MRN (unique per tenant). `integration_clients` holds API-key
credentials for hospital systems, which act as the role `interface`; `hl7_messages` records each inbound message's
control id and ACK (never its content) so retries are idempotent. Both new tables are tenant-scoped with the usual
RLS policy. The new permission `adt:ingest` goes to `interface` and, as every permission does, to `admin`.

Existing patients get no MRN: the column stays NULL until an A01 for them arrives (as a new patient; there is no
way to link an existing row to an MRN yet).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 23:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copies (migrations must not change when app code does).
NEW_TENANT_TABLES = ("integration_clients", "hl7_messages")
ROWS = [("admin", "adt:ingest"), ("interface", "adt:ingest")]


def upgrade() -> None:
    with op.batch_alter_table('patients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mrn', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('mrn_authority', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('mrn_index', sa.String(length=64), nullable=True))
    # A unique index rather than a constraint: on SQLite a constraint means rebuilding `patients`, and dropping the
    # old table there would cascade-delete workflows and summaries.
    op.create_index('uq_patients_tenant_mrn_index', 'patients', ['tenant_id', 'mrn_index'], unique=True)

    op.create_table('integration_clients',
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('secret_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_integration_clients_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('client_id', name=op.f('pk_integration_clients'))
    )
    with op.batch_alter_table('integration_clients', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_integration_clients_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('hl7_messages',
    sa.Column('message_id', sa.String(length=40), nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('control_id', sa.String(length=199), nullable=False),
    sa.Column('message_type', sa.String(length=20), nullable=False),
    sa.Column('ack_code', sa.String(length=2), nullable=False),
    sa.Column('error_code', sa.String(length=4), nullable=True),
    sa.Column('ack_text', sa.String(length=500), nullable=True),
    sa.Column('patient_id', sa.String(length=40), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['integration_clients.client_id'], name=op.f('fk_hl7_messages_client_id_integration_clients'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_hl7_messages_patient_id_patients'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_hl7_messages_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('message_id', name=op.f('pk_hl7_messages')),
    sa.UniqueConstraint('tenant_id', 'client_id', 'control_id', name='uq_hl7_messages_client_control')
    )
    with op.batch_alter_table('hl7_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_hl7_messages_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_hl7_messages_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_hl7_messages_tenant_id'), ['tenant_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_TENANT_TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       f"USING (tenant_id = current_setting('app.tenant_id', true)) "
                       f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_TENANT_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")

    with op.batch_alter_table('hl7_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_hl7_messages_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_hl7_messages_patient_id'))
        batch_op.drop_index(batch_op.f('ix_hl7_messages_client_id'))
    op.drop_table('hl7_messages')

    with op.batch_alter_table('integration_clients', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_integration_clients_tenant_id'))
    op.drop_table('integration_clients')

    op.drop_index('uq_patients_tenant_mrn_index', table_name='patients')
    with op.batch_alter_table('patients', schema=None) as batch_op:
        batch_op.drop_column('mrn_index')
        batch_op.drop_column('mrn_authority')
        batch_op.drop_column('mrn')
