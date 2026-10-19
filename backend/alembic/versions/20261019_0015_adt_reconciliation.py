"""ADT reconciliation: externally discharged workflows, patient merges, parked HL7 messages

ADR-002 Phase 8, item 11 (design change 2; findings 6, 7 and 10).

- `workflows.departed_at`, `workflows.documentation_due_at`: an A03 the state machine can't complete moves the
  workflow to the new status `externally_discharged` (a string; no constraint to change) with the time the patient
  left and the policy deadline for the tasks still open.
- `patients.merged_into`: set on the non-surviving record of an A40 merge. No foreign key, so the column can be added
  in place on SQLite.
- `hl7_parked_messages`: out-of-order messages, kept as ciphertext only, tenant-scoped (usual policy). Not unit-scoped:
  a parked message has no patient yet.

Every change is additive, so the previous release keeps working while the deploy rolls out.

Revision ID: 0015
Revises: 0011
Create Date: 2026-10-19 21:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"


def upgrade() -> None:
    op.add_column('workflows', sa.Column('departed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('workflows', sa.Column('documentation_due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('patients', sa.Column('merged_into', sa.String(length=40), nullable=True))
    op.create_index(op.f('ix_patients_merged_into'), 'patients', ['merged_into'], unique=False)

    op.create_table('hl7_parked_messages',
    sa.Column('parked_id', sa.String(length=40), nullable=False),
    sa.Column('message_id', sa.String(length=40), nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('event', sa.String(length=20), nullable=False),
    sa.Column('mrn_index', sa.String(length=64), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('dek', sa.LargeBinary(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result_ack', sa.String(length=2), nullable=True),
    sa.Column('result_text', sa.String(length=500), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['integration_clients.client_id'], name=op.f('fk_hl7_parked_messages_client_id_integration_clients'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['message_id'], ['hl7_messages.message_id'], name=op.f('fk_hl7_parked_messages_message_id_hl7_messages'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_hl7_parked_messages_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('parked_id', name=op.f('pk_hl7_parked_messages'))
    )
    with op.batch_alter_table('hl7_parked_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_hl7_parked_messages_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_hl7_parked_messages_message_id'), ['message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_hl7_parked_messages_mrn_index'), ['mrn_index'], unique=False)
        batch_op.create_index(batch_op.f('ix_hl7_parked_messages_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_hl7_parked_messages_tenant_id'), ['tenant_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON hl7_parked_messages TO {APP_ROLE}")
        op.execute("ALTER TABLE hl7_parked_messages ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON hl7_parked_messages "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON hl7_parked_messages")
        op.execute(f"REVOKE ALL ON hl7_parked_messages FROM {APP_ROLE}")

    with op.batch_alter_table('hl7_parked_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_hl7_parked_messages_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_hl7_parked_messages_status'))
        batch_op.drop_index(batch_op.f('ix_hl7_parked_messages_mrn_index'))
        batch_op.drop_index(batch_op.f('ix_hl7_parked_messages_message_id'))
        batch_op.drop_index(batch_op.f('ix_hl7_parked_messages_client_id'))
    op.drop_table('hl7_parked_messages')

    op.drop_index(op.f('ix_patients_merged_into'), table_name='patients')
    # SQLite 3.35+ drops these columns in place (no table rebuild, which would cascade-delete child rows).
    op.drop_column('patients', 'merged_into')
    op.drop_column('workflows', 'documentation_due_at')
    op.drop_column('workflows', 'departed_at')
