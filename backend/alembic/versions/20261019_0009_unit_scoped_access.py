"""unit-scoped access: units, memberships, patients.unit_id, break-glass grants, unit_scope RLS

ADR-002 Phase 7, item 3. `units` (wards, coded like HL7 PV1-3.1), `user_units` (membership) and `access_grants`
(break-glass) are tenant-scoped with the usual policy. On Postgres, a RESTRICTIVE `unit_scope` policy on patients,
workflows, tasks, discharge_summaries and insurance_claims hides patients outside the user's units. It only applies
when the request sets app.unit_scope = 'on', which the API does only with UNIT_SCOPED_ACCESS on, so this migration
changes nobody's access by itself. `df_patient_exists` lets the API answer 403 (break-glass possible) instead of 404.
New permission `patient:read_all` (admin, interface): never unit-scoped.

Existing patients get no unit (NULL), which every user in the tenant can see.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copies (migrations must not change when app code does).
NEW_TENANT_TABLES = ("units", "user_units", "access_grants")
ROWS = [("admin", "patient:read_all"), ("interface", "patient:read_all")]
SCOPE_OFF = "current_setting('app.unit_scope', true) IS DISTINCT FROM 'on'"
PATIENT_VISIBLE = (
    f"{SCOPE_OFF} OR unit_id IS NULL "
    "OR unit_id = ANY (string_to_array(current_setting('app.unit_ids', true), ',')) "
    "OR EXISTS (SELECT 1 FROM access_grants g WHERE g.patient_id = patients.patient_id "
    "AND g.user_id = current_setting('app.user_id', true) AND g.expires_at > now())"
)
CHILD_TABLES = ("workflows", "tasks", "discharge_summaries", "insurance_claims")
PATIENT_EXISTS_FUNCTION = (
    "CREATE OR REPLACE FUNCTION df_patient_exists(pid text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
    "SET search_path = public AS $$ SELECT EXISTS (SELECT 1 FROM patients WHERE patient_id = pid "
    "AND tenant_id = current_setting('app.tenant_id', true)) $$"
)


def upgrade() -> None:
    op.create_table('units',
    sa.Column('unit_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_units_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('unit_id', name=op.f('pk_units')),
    sa.UniqueConstraint('tenant_id', 'code', name='uq_units_tenant_code')
    )
    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_units_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('user_units',
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('unit_id', sa.String(length=40), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_user_units_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['unit_id'], ['units.unit_id'], name=op.f('fk_user_units_unit_id_units'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], name=op.f('fk_user_units_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'unit_id', name=op.f('pk_user_units'))
    )
    with op.batch_alter_table('user_units', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_units_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_units_unit_id'), ['unit_id'], unique=False)

    op.create_table('access_grants',
    sa.Column('grant_id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('patient_id', sa.String(length=40), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'], name=op.f('fk_access_grants_patient_id_patients'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_access_grants_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], name=op.f('fk_access_grants_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('grant_id', name=op.f('pk_access_grants'))
    )
    with op.batch_alter_table('access_grants', schema=None) as batch_op:
        batch_op.create_index('ix_access_grants_patient_user', ['patient_id', 'user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_access_grants_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_access_grants_user_id'), ['user_id'], unique=False)

    # Not batch mode: on SQLite that would rebuild `patients`, and dropping the old table cascade-deletes workflows
    # and summaries. SQLite can add a nullable column with an inline REFERENCES clause in place instead.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE patients ADD COLUMN unit_id VARCHAR(40) "
                   "CONSTRAINT fk_patients_unit_id_units REFERENCES units (unit_id) ON DELETE SET NULL")
    else:
        op.add_column('patients', sa.Column('unit_id', sa.String(length=40), nullable=True))
        op.create_foreign_key(op.f('fk_patients_unit_id_units'), 'patients', 'units', ['unit_id'], ['unit_id'],
                              ondelete='SET NULL')
    op.create_index(op.f('ix_patients_unit_id'), 'patients', ['unit_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_TENANT_TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       f"USING (tenant_id = current_setting('app.tenant_id', true)) "
                       f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
        op.execute(f"CREATE POLICY unit_scope ON patients AS RESTRICTIVE USING ({PATIENT_VISIBLE}) WITH CHECK (true)")
        for table in CHILD_TABLES:
            op.execute(f"CREATE POLICY unit_scope ON {table} AS RESTRICTIVE USING ({SCOPE_OFF} OR EXISTS "
                       f"(SELECT 1 FROM patients p WHERE p.patient_id = {table}.patient_id)) WITH CHECK (true)")
        op.execute(PATIENT_EXISTS_FUNCTION)
        op.execute("REVOKE ALL ON FUNCTION df_patient_exists(text) FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION df_patient_exists(text) TO {APP_ROLE}")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS df_patient_exists(text)")
        for table in ("patients",) + CHILD_TABLES:
            op.execute(f"DROP POLICY IF EXISTS unit_scope ON {table}")
        for table in NEW_TENANT_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")

    op.drop_index(op.f('ix_patients_unit_id'), table_name='patients')
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(op.f('fk_patients_unit_id_units'), 'patients', type_='foreignkey')
    # SQLite 3.35+ drops a column in place; its inline foreign key goes with it.
    op.drop_column('patients', 'unit_id')

    with op.batch_alter_table('access_grants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_access_grants_user_id'))
        batch_op.drop_index(batch_op.f('ix_access_grants_tenant_id'))
        batch_op.drop_index('ix_access_grants_patient_user')
    op.drop_table('access_grants')

    with op.batch_alter_table('user_units', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_units_unit_id'))
        batch_op.drop_index(batch_op.f('ix_user_units_tenant_id'))
    op.drop_table('user_units')

    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_units_tenant_id'))
    op.drop_table('units')
