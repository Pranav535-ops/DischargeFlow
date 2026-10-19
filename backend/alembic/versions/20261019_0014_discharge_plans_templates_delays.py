"""discharge plans, workflow templates, delay reasons

ADR-002 Phase 8, items 8-10.
- Item 8: the discharge plan on the encounter. `expected_discharge_date` exists since 0011; this adds
  `planned_disposition` and the daily review (`plan_reviewed_at`, `plan_reviewed_by`). New permission `plan:manage`
  (physician, nurse, admin).
- Item 9: `workflow_templates` (tenant-scoped, usual policy), one per disposition per tenant, with tasks,
  prerequisites and due offsets in JSON. `workflows.template_id`/`template_version` record which one a discharge used;
  `tasks.due_at`, `due_offset_hours` and `depends_on` carry the due time and prerequisites. The six default templates
  (home, home with services, SNF, rehab, hospice, AMA) are inserted for every existing tenant. New permission
  `template:manage` (admin).
- Item 10: `delay_reasons` (tenant- and unit-scoped like the other tables about a patient): a code when the expected
  date slips or a task is completed after its due time.

Existing rows: plans, due times and prerequisites are NULL, so running discharges keep the standard checklist and its
SLA targets. Columns go onto `tasks` and `workflows` with a plain ADD COLUMN, not batch mode: on SQLite batch mode
rebuilds the table, and dropping the old `workflows` cascade-deletes its tasks.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19 19:00:00
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copies (migrations must not change when app code does).
SCOPE_OFF = "current_setting('app.unit_scope', true) IS DISTINCT FROM 'on'"
ROWS = [("physician", "plan:manage"), ("nurse", "plan:manage"), ("admin", "plan:manage"),
        ("admin", "template:manage")]
# (disposition, name, [(key, title, description, category, role, priority, due offset hours, prerequisites)])
TEMPLATES = [
    ('home', 'Home (self-care)', [
        ('summary', 'Complete Discharge Summary', 'Generate and approve AI discharge summary', 'documentation', 'physician', 1, 10, ()),
        ('med_review', 'Final Medication Review', 'Review and reconcile all medications', 'medication', 'pharmacist', 1, 9, ()),
        ('meds', 'Prepare Discharge Medications', 'Prepare and package medications for patient', 'medication', 'pharmacist', 2, 11, ('med_review',)),
        ('education', 'Patient Education', 'Provide discharge instructions and education materials', 'education', 'nurse', 2, 11, ('med_review',)),
        ('follow_up', 'Schedule Follow-up Appointments', 'Schedule necessary follow-up appointments', 'follow_up', 'nurse', 2, 10, ()),
        ('billing', 'Billing Clearance', 'Verify insurance and billing clearance', 'billing', 'nurse', 3, 12, ()),
        ('vitals', 'Final Vitals Check', 'Record final vital signs before discharge', 'documentation', 'nurse', 1, 11, ()),
        ('iv', 'Remove IV and Equipment', 'Remove IV lines and return equipment', 'documentation', 'nurse', 2, 12, ('vitals',)),
    ]),
    ('home_services', 'Home with services', [
        ('home_health', 'Arrange Home Health Services', 'Agency accepted the referral and set a start of care', 'placement', 'nurse', 1, -24, ()),
        ('equipment', 'Order Durable Medical Equipment', 'Equipment ordered and delivery confirmed', 'equipment', 'nurse', 2, -24, ()),
        ('summary', 'Complete Discharge Summary', 'Generate and approve AI discharge summary', 'documentation', 'physician', 1, 10, ()),
        ('med_review', 'Final Medication Review', 'Review and reconcile all medications', 'medication', 'pharmacist', 1, 9, ()),
        ('meds', 'Prepare Discharge Medications', 'Prepare and package medications for patient', 'medication', 'pharmacist', 2, 11, ('med_review',)),
        ('education', 'Patient and Caregiver Education', 'Teach-back on medications, equipment and home care', 'education', 'nurse', 2, 11, ('med_review', 'equipment')),
        ('follow_up', 'Schedule Follow-up Appointments', 'Schedule necessary follow-up appointments', 'follow_up', 'nurse', 2, 10, ()),
        ('vitals', 'Final Vitals Check', 'Record final vital signs before discharge', 'documentation', 'nurse', 1, 11, ()),
        ('iv', 'Remove IV and Equipment', 'Remove IV lines and return equipment', 'documentation', 'nurse', 2, 12, ('vitals',)),
    ]),
    ('snf', 'Skilled nursing facility', [
        ('placement', 'Secure SNF Placement', 'Referral sent and acceptance confirmed by the facility', 'placement', 'nurse', 1, -48, ()),
        ('prior_auth', 'Insurance Prior Authorization', 'Obtain payer authorization for the facility stay', 'insurance', 'nurse', 1, -24, ('placement',)),
        ('summary', 'Complete Discharge Summary', 'Generate and approve AI discharge summary', 'documentation', 'physician', 1, 10, ()),
        ('med_review', 'Final Medication Review', 'Review and reconcile all medications', 'medication', 'pharmacist', 1, 9, ()),
        ('meds', 'Prepare Discharge Medications', 'Prepare and package medications for patient', 'medication', 'pharmacist', 2, 11, ('med_review',)),
        ('vitals', 'Final Vitals Check', 'Record final vital signs before discharge', 'documentation', 'nurse', 1, 11, ()),
        ('iv', 'Remove IV and Equipment', 'Remove IV lines and return equipment', 'documentation', 'nurse', 2, 12, ('vitals',)),
        ('transfer_packet', 'Send Transfer Packet', 'Summary, medication list and orders sent to the facility', 'documentation', 'nurse', 1, 11, ('summary', 'med_review', 'placement')),
        ('transport', 'Arrange Transport', 'Book transport to the facility for the discharge time', 'transport', 'nurse', 2, 10, ('prior_auth',)),
    ]),
    ('rehab', 'Inpatient rehabilitation', [
        ('placement', 'Secure Rehab Bed Acceptance', 'Referral sent and acceptance confirmed by the rehabilitation unit', 'placement', 'nurse', 1, -48, ()),
        ('prior_auth', 'Insurance Prior Authorization', 'Obtain payer authorization for the rehabilitation unit stay', 'insurance', 'nurse', 1, -24, ('placement',)),
        ('summary', 'Complete Discharge Summary', 'Generate and approve AI discharge summary', 'documentation', 'physician', 1, 10, ()),
        ('med_review', 'Final Medication Review', 'Review and reconcile all medications', 'medication', 'pharmacist', 1, 9, ()),
        ('meds', 'Prepare Discharge Medications', 'Prepare and package medications for patient', 'medication', 'pharmacist', 2, 11, ('med_review',)),
        ('vitals', 'Final Vitals Check', 'Record final vital signs before discharge', 'documentation', 'nurse', 1, 11, ()),
        ('iv', 'Remove IV and Equipment', 'Remove IV lines and return equipment', 'documentation', 'nurse', 2, 12, ('vitals',)),
        ('transfer_packet', 'Send Transfer Packet', 'Summary, medication list and orders sent to the facility', 'documentation', 'nurse', 1, 11, ('summary', 'med_review', 'placement')),
        ('transport', 'Arrange Transport', 'Book transport to the facility for the discharge time', 'transport', 'nurse', 2, 10, ('prior_auth',)),
    ]),
    ('hospice', 'Hospice', [
        ('hospice_referral', 'Hospice Referral and Consent', 'Hospice accepted; goals of care and consent recorded', 'placement', 'nurse', 1, -24, ()),
        ('summary', 'Complete Discharge Summary', 'Generate and approve AI discharge summary', 'documentation', 'physician', 1, 10, ()),
        ('med_review', 'Comfort Medication Plan', 'Reconcile medications for comfort care', 'medication', 'pharmacist', 1, 9, ()),
        ('education', 'Family Education', 'Teach the family the care and symptom plan', 'education', 'nurse', 2, 11, ('med_review', 'hospice_referral')),
        ('transport', 'Arrange Transport', 'Book transport to home or the hospice', 'transport', 'nurse', 2, 10, ('hospice_referral',)),
    ]),
    ('ama', 'Left against medical advice', [
        ('ama_form', 'Document AMA Discussion', 'Risks explained, capacity assessed, AMA form signed or refused', 'documentation', 'physician', 1, 0, ()),
        ('summary', 'Complete Discharge Summary', 'Generate and approve AI discharge summary', 'documentation', 'physician', 1, 10, ()),
        ('med_review', 'Final Medication Review', 'Prescriptions and medication risks for the patient leaving', 'medication', 'pharmacist', 1, 0, ()),
        ('education', 'Return Precautions', 'When to come back; follow-up offered', 'education', 'nurse', 1, 0, ()),
    ]),
]


def upgrade() -> None:
    op.create_table('workflow_templates',
    sa.Column('template_id', sa.String(length=40), nullable=False),
    sa.Column('disposition', sa.String(length=30), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('tasks', sa.JSON(), nullable=False),
    sa.Column('updated_by', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_workflow_templates_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('template_id', name=op.f('pk_workflow_templates')),
    sa.UniqueConstraint('tenant_id', 'disposition', name='uq_workflow_templates_tenant_disposition')
    )
    with op.batch_alter_table('workflow_templates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_workflow_templates_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('delay_reasons',
    sa.Column('delay_id', sa.String(length=40), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('encounter_id', sa.String(length=40), nullable=True),
    sa.Column('task_id', sa.String(length=40), nullable=True),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('code', sa.String(length=30), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('slip_days', sa.Integer(), nullable=True),
    sa.Column('recorded_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['encounter_id'], ['encounters.encounter_id'], name=op.f('fk_delay_reasons_encounter_id_encounters'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_delay_reasons_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.task_id'], name=op.f('fk_delay_reasons_task_id_tasks'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_delay_reasons_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('delay_id', name=op.f('pk_delay_reasons'))
    )
    with op.batch_alter_table('delay_reasons', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_delay_reasons_encounter_id'), ['encounter_id'], unique=False)
        batch_op.create_index('ix_delay_reasons_kind_created', ['kind', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_delay_reasons_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delay_reasons_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delay_reasons_tenant_id'), ['tenant_id'], unique=False)

    for table, column in (
        ('encounters', sa.Column('planned_disposition', sa.String(length=30), nullable=True)),
        ('encounters', sa.Column('plan_reviewed_at', sa.DateTime(timezone=True), nullable=True)),
        ('encounters', sa.Column('plan_reviewed_by', sa.String(length=40), nullable=True)),
        ('tasks', sa.Column('due_at', sa.DateTime(timezone=True), nullable=True)),
        ('tasks', sa.Column('due_offset_hours', sa.Integer(), nullable=True)),
        ('tasks', sa.Column('depends_on', sa.JSON(), nullable=True)),
        ('workflows', sa.Column('template_id', sa.String(length=40), nullable=True)),
        ('workflows', sa.Column('template_version', sa.Integer(), nullable=True)),
    ):
        op.add_column(table, column)

    if op.get_bind().dialect.name == "postgresql":
        for table in ('workflow_templates', 'delay_reasons'):
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       "USING (tenant_id = current_setting('app.tenant_id', true)) "
                       "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
        op.execute(f"CREATE POLICY unit_scope ON delay_reasons AS RESTRICTIVE USING ({SCOPE_OFF} OR EXISTS "
                   f"(SELECT 1 FROM patients p WHERE p.patient_id = delay_reasons.patient_id)) WITH CHECK (true)")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])
    _seed_templates()


def _seed_templates() -> None:
    bind = op.get_bind()
    tenants = [row[0] for row in bind.execute(sa.text("SELECT tenant_id FROM tenants"))]
    templates = sa.table('workflow_templates', sa.column('template_id', sa.String), sa.column('tenant_id', sa.String),
                         sa.column('disposition', sa.String), sa.column('name', sa.String),
                         sa.column('version', sa.Integer), sa.column('active', sa.Boolean),
                         sa.column('tasks', sa.JSON), sa.column('created_at', sa.DateTime(timezone=True)),
                         sa.column('updated_at', sa.DateTime(timezone=True)))
    now = datetime.now(timezone.utc)
    rows = []
    for tenant_id in tenants:
        for disposition, name, tasks in TEMPLATES:
            rows.append({
                'template_id': f"wft_{uuid.uuid4().hex[:12]}", 'tenant_id': tenant_id, 'disposition': disposition,
                'name': name, 'version': 1, 'active': True, 'created_at': now, 'updated_at': now,
                'tasks': [{"key": k, "title": t, "description": d, "category": c, "assigned_role": r, "priority": p,
                           "due_offset_hours": o, "depends_on": list(deps)} for k, t, d, c, r, p, o, deps in tasks],
            })
    if rows:
        op.bulk_insert(templates, rows)


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS unit_scope ON delay_reasons")
        for table in ('delay_reasons', 'workflow_templates'):
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")

    # SQLite 3.35+ drops a column in place (no table rebuild, so no cascade).
    for table, column in (('workflows', 'template_version'), ('workflows', 'template_id'),
                          ('tasks', 'depends_on'), ('tasks', 'due_offset_hours'), ('tasks', 'due_at'),
                          ('encounters', 'plan_reviewed_by'), ('encounters', 'plan_reviewed_at'),
                          ('encounters', 'planned_disposition')):
        op.drop_column(table, column)

    with op.batch_alter_table('delay_reasons', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_delay_reasons_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_delay_reasons_task_id'))
        batch_op.drop_index(batch_op.f('ix_delay_reasons_patient_id'))
        batch_op.drop_index('ix_delay_reasons_kind_created')
        batch_op.drop_index(batch_op.f('ix_delay_reasons_encounter_id'))
    op.drop_table('delay_reasons')
    with op.batch_alter_table('workflow_templates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workflow_templates_tenant_id'))
    op.drop_table('workflow_templates')
