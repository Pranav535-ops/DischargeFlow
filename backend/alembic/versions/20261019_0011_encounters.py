"""encounters: one row per hospital stay, workflows.encounter_id

ADR-002 Phase 8, item 7 (design change 1): patients -> encounters -> workflows. `encounters` is tenant-scoped (usual
policy) and unit-scoped like the other tables about a patient (RESTRICTIVE `unit_scope`, visible when the patient
is). A partial unique index allows one active encounter per patient, and a second one on workflows allows one active
workflow per encounter.

Data: each patient gets a first encounter copied from its current admission fields (admission_date, room_number,
unit_id). It is `discharged` when the patient is, ending at the patient's last completed discharge, else `active`.
Every workflow of the patient points to it: earlier stays were overwritten before this migration, so they can't be
told apart. The patient columns stay (the previous release still reads and writes them during the rollout), and
`workflows.encounter_id` is nullable so that release can still insert workflows.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-19 18:00:00
"""
import uuid
from datetime import date, datetime, time, timezone
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copies (migrations must not change when app code does).
SCOPE_OFF = "current_setting('app.unit_scope', true) IS DISTINCT FROM 'on'"
ACTIVE_ENCOUNTER_SQL = sa.text("status = 'active'")
ACTIVE_WORKFLOW_SQL = sa.text("status IN ('in_progress', 'pending_approval')")


def _admitted_at(value: Optional[str]) -> Optional[datetime]:
    raw = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw[:10]), time())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def upgrade() -> None:
    op.create_table('encounters',
    sa.Column('encounter_id', sa.String(length=40), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('visit_number', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('admitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('discharged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('unit_id', sa.String(length=40), nullable=True),
    sa.Column('room_number', sa.String(length=40), nullable=True),
    sa.Column('disposition', sa.String(length=40), nullable=True),
    sa.Column('expected_discharge_date', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_encounters_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_encounters_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['unit_id'], ['units.unit_id'], name=op.f('fk_encounters_unit_id_units'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('encounter_id', name=op.f('pk_encounters'))
    )
    with op.batch_alter_table('encounters', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_encounters_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_encounters_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_encounters_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_encounters_unit_id'), ['unit_id'], unique=False)
        batch_op.create_index('uq_encounters_one_active_per_patient', ['patient_id'], unique=True,
                              postgresql_where=ACTIVE_ENCOUNTER_SQL, sqlite_where=ACTIVE_ENCOUNTER_SQL)

    # Not batch mode: on SQLite that would rebuild `workflows`, and dropping the old table cascade-deletes tasks and
    # summaries. SQLite adds a nullable column with an inline REFERENCES clause in place instead.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE workflows ADD COLUMN encounter_id VARCHAR(40) CONSTRAINT "
                   "fk_workflows_encounter_id_encounters REFERENCES encounters (encounter_id) ON DELETE CASCADE")
    else:
        op.add_column('workflows', sa.Column('encounter_id', sa.String(length=40), nullable=True))
        op.create_foreign_key(op.f('fk_workflows_encounter_id_encounters'), 'workflows', 'encounters',
                              ['encounter_id'], ['encounter_id'], ondelete='CASCADE')
    op.create_index(op.f('ix_workflows_encounter_id'), 'workflows', ['encounter_id'], unique=False)
    op.create_index('uq_workflows_one_active_per_encounter', 'workflows', ['encounter_id'], unique=True,
                    postgresql_where=ACTIVE_WORKFLOW_SQL, sqlite_where=ACTIVE_WORKFLOW_SQL)

    _copy_admissions()

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON encounters TO {APP_ROLE}")
        op.execute("ALTER TABLE encounters ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON encounters "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
        op.execute(f"CREATE POLICY unit_scope ON encounters AS RESTRICTIVE USING ({SCOPE_OFF} OR EXISTS "
                   f"(SELECT 1 FROM patients p WHERE p.patient_id = encounters.patient_id)) WITH CHECK (true)")


def _copy_admissions() -> None:
    """One encounter per existing patient from its current admission fields; its workflows point to it."""
    bind = op.get_bind()
    patients = sa.table('patients', sa.column('patient_id', sa.String), sa.column('tenant_id', sa.String),
                        sa.column('admission_date', sa.String), sa.column('room_number', sa.String),
                        sa.column('unit_id', sa.String), sa.column('status', sa.String),
                        sa.column('created_at', sa.DateTime(timezone=True)))
    workflows = sa.table('workflows', sa.column('workflow_id', sa.String), sa.column('patient_id', sa.String),
                         sa.column('encounter_id', sa.String), sa.column('status', sa.String),
                         sa.column('completed_at', sa.DateTime(timezone=True)))
    encounters = sa.table('encounters', *(sa.column(c, sa.String) for c in (
        'encounter_id', 'tenant_id', 'patient_id', 'status', 'unit_id', 'room_number')),
        *(sa.column(c, sa.DateTime(timezone=True)) for c in (
            'admitted_at', 'discharged_at', 'created_at', 'updated_at')))

    last_discharge = dict(bind.execute(
        sa.select(workflows.c.patient_id, sa.func.max(workflows.c.completed_at))
        .where(workflows.c.status == 'completed').group_by(workflows.c.patient_id)).all())
    now = datetime.now(timezone.utc)
    rows = []
    for p in bind.execute(sa.select(patients)).mappings():
        discharged = p['status'] == 'discharged'
        rows.append({
            'encounter_id': f"enc_{uuid.uuid4().hex[:12]}",
            'tenant_id': p['tenant_id'],
            'patient_id': p['patient_id'],
            'status': 'discharged' if discharged else 'active',
            'admitted_at': _admitted_at(p['admission_date']),
            'discharged_at': _aware(last_discharge.get(p['patient_id'])) if discharged else None,
            'unit_id': p['unit_id'],
            'room_number': p['room_number'],
            'created_at': _aware(p['created_at']) or now,
            'updated_at': now,
        })
    if rows:
        op.bulk_insert(encounters, rows)
        # Exactly one encounter per patient at this point.
        bind.execute(workflows.update().values(encounter_id=sa.select(encounters.c.encounter_id).where(
            encounters.c.patient_id == workflows.c.patient_id).scalar_subquery()))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS unit_scope ON encounters")
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON encounters")
        op.execute(f"REVOKE ALL ON encounters FROM {APP_ROLE}")

    op.drop_index('uq_workflows_one_active_per_encounter', table_name='workflows',
                  postgresql_where=ACTIVE_WORKFLOW_SQL, sqlite_where=ACTIVE_WORKFLOW_SQL)
    op.drop_index(op.f('ix_workflows_encounter_id'), table_name='workflows')
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(op.f('fk_workflows_encounter_id_encounters'), 'workflows', type_='foreignkey')
    # SQLite 3.35+ drops a column in place; its inline foreign key goes with it.
    op.drop_column('workflows', 'encounter_id')

    with op.batch_alter_table('encounters', schema=None) as batch_op:
        batch_op.drop_index('uq_encounters_one_active_per_patient',
                            postgresql_where=ACTIVE_ENCOUNTER_SQL, sqlite_where=ACTIVE_ENCOUNTER_SQL)
        batch_op.drop_index(batch_op.f('ix_encounters_unit_id'))
        batch_op.drop_index(batch_op.f('ix_encounters_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_encounters_status'))
        batch_op.drop_index(batch_op.f('ix_encounters_patient_id'))
    op.drop_table('encounters')
