"""discharge summary lifecycle: signed versions and addenda

ADR-002 Phase 8, item 13. `discharge_summary_versions` holds each signed (frozen) version of a summary and
`discharge_summary_addenda` the text added after signature, each signed by its author. Both are encrypted with the
patient's data key like the summary, tenant-scoped with the usual policy, and unit-scoped like discharge_summaries
(visible when the patient is).

Summaries approved before this migration get their version 1 from `app.cli migrate` (core/migrate.py), which can
encrypt; this migration only creates the tables.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copy of core/rls.py (migrations must not change when app code does).
SCOPE_OFF = "current_setting('app.unit_scope', true) IS DISTINCT FROM 'on'"
TABLES = ("discharge_summary_versions", "discharge_summary_addenda")


def upgrade() -> None:
    op.create_table('discharge_summary_versions',
    sa.Column('version_id', sa.String(length=40), nullable=False),
    sa.Column('summary_id', sa.String(length=40), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('signed_by', sa.String(length=40), nullable=False),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_discharge_summary_versions_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['summary_id'], ['discharge_summaries.summary_id'], name=op.f('fk_discharge_summary_versions_summary_id_discharge_summaries'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_discharge_summary_versions_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('version_id', name=op.f('pk_discharge_summary_versions')),
    sa.UniqueConstraint('summary_id', 'version', name=op.f('uq_discharge_summary_versions_summary_id'))
    )
    with op.batch_alter_table('discharge_summary_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discharge_summary_versions_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_discharge_summary_versions_summary_id'), ['summary_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_discharge_summary_versions_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('discharge_summary_addenda',
    sa.Column('addendum_id', sa.String(length=40), nullable=False),
    sa.Column('summary_id', sa.String(length=40), nullable=False),
    sa.Column('version_id', sa.String(length=40), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('signed_by', sa.String(length=40), nullable=False),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_discharge_summary_addenda_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['summary_id'], ['discharge_summaries.summary_id'], name=op.f('fk_discharge_summary_addenda_summary_id_discharge_summaries'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_discharge_summary_addenda_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['version_id'], ['discharge_summary_versions.version_id'], name=op.f('fk_discharge_summary_addenda_version_id_discharge_summary_versions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('addendum_id', name=op.f('pk_discharge_summary_addenda'))
    )
    with op.batch_alter_table('discharge_summary_addenda', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discharge_summary_addenda_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_discharge_summary_addenda_summary_id'), ['summary_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_discharge_summary_addenda_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_discharge_summary_addenda_version_id'), ['version_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       "USING (tenant_id = current_setting('app.tenant_id', true)) "
                       "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
            op.execute(f"CREATE POLICY unit_scope ON {table} AS RESTRICTIVE USING ({SCOPE_OFF} OR EXISTS "
                       f"(SELECT 1 FROM patients p WHERE p.patient_id = {table}.patient_id)) WITH CHECK (true)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"DROP POLICY IF EXISTS unit_scope ON {table}")
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    with op.batch_alter_table('discharge_summary_addenda', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discharge_summary_addenda_version_id'))
        batch_op.drop_index(batch_op.f('ix_discharge_summary_addenda_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_discharge_summary_addenda_summary_id'))
        batch_op.drop_index(batch_op.f('ix_discharge_summary_addenda_patient_id'))
    op.drop_table('discharge_summary_addenda')
    with op.batch_alter_table('discharge_summary_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discharge_summary_versions_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_discharge_summary_versions_summary_id'))
        batch_op.drop_index(batch_op.f('ix_discharge_summary_versions_patient_id'))
    op.drop_table('discharge_summary_versions')
