"""patient-facing discharge instructions and the patient's preferred language

ADR-002 Phase 9, item 18.

- `patients.preferred_language`: BCP 47 code, nullable, added in place.
- `patient_instructions`: drafts and approved instructions, one per (workflow, language), text encrypted with the
  patient's data key like discharge summaries. Tenant-scoped with the usual policy and unit-scoped like
  discharge_summaries (visible when the patient is).
- Permission `instructions:manage` for physicians and nurses (admins hold every permission).

Additive only, so the previous release keeps working while the deploy rolls out.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-19 22:34:37
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copy of core/rls.py (migrations must not change when app code does).
SCOPE_OFF = "current_setting('app.unit_scope', true) IS DISTINCT FROM 'on'"
# Frozen copy of the new rows in identity/permissions.py:DEFAULT_ROLE_PERMISSIONS (admin holds all).
ROWS = [("admin", "instructions:manage"), ("nurse", "instructions:manage"), ("physician", "instructions:manage")]


def upgrade() -> None:
    op.create_table('patient_instructions',
    sa.Column('instructions_id', sa.String(length=40), nullable=False),
    sa.Column('workflow_id', sa.String(length=40), nullable=False),
    sa.Column('summary_id', sa.String(length=40), nullable=False),
    sa.Column('source_version_id', sa.String(length=40), nullable=True),
    sa.Column('source_sha256', sa.String(length=64), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('language', sa.String(length=35), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('original_content', sa.Text(), nullable=True),
    sa.Column('structured', sa.Text(), nullable=True),
    sa.Column('edit_diff', sa.Text(), nullable=True),
    sa.Column('reading_grade', sa.Float(), nullable=True),
    sa.Column('approved_reading_grade', sa.Float(), nullable=True),
    sa.Column('target_grade', sa.Float(), nullable=False),
    sa.Column('generation_id', sa.String(length=40), nullable=True),
    sa.Column('generated_by', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('approved', sa.Boolean(), nullable=False),
    sa.Column('approved_by', sa.String(length=40), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('edit_similarity', sa.Float(), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['generation_id'], ['ai_generations.generation_id'], name=op.f('fk_patient_instructions_generation_id_ai_generations'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_patient_instructions_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_version_id'], ['discharge_summary_versions.version_id'], name=op.f('fk_patient_instructions_source_version_id_discharge_summary_versions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['summary_id'], ['discharge_summaries.summary_id'], name=op.f('fk_patient_instructions_summary_id_discharge_summaries'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_patient_instructions_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.workflow_id'], name=op.f('fk_patient_instructions_workflow_id_workflows'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('instructions_id', name=op.f('pk_patient_instructions')),
    sa.UniqueConstraint('workflow_id', 'language', name=op.f('uq_patient_instructions_workflow_id'))
    )
    with op.batch_alter_table('patient_instructions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_patient_instructions_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_patient_instructions_summary_id'), ['summary_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_patient_instructions_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_patient_instructions_workflow_id'), ['workflow_id'], unique=False)

    op.add_column('patients', sa.Column('preferred_language', sa.String(length=35), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON patient_instructions TO {APP_ROLE}")
        op.execute("ALTER TABLE patient_instructions ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON patient_instructions "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
        op.execute(f"CREATE POLICY unit_scope ON patient_instructions AS RESTRICTIVE USING ({SCOPE_OFF} OR EXISTS "
                   "(SELECT 1 FROM patients p WHERE p.patient_id = patient_instructions.patient_id)) WITH CHECK (true)")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])



def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS unit_scope ON patient_instructions")
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON patient_instructions")
        op.execute(f"REVOKE ALL ON patient_instructions FROM {APP_ROLE}")
    # SQLite 3.35+ drops a column in place (no table rebuild, so no cascade).
    op.drop_column('patients', 'preferred_language')

    with op.batch_alter_table('patient_instructions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_patient_instructions_workflow_id'))
        batch_op.drop_index(batch_op.f('ix_patient_instructions_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_patient_instructions_summary_id'))
        batch_op.drop_index(batch_op.f('ix_patient_instructions_patient_id'))

    op.drop_table('patient_instructions')
