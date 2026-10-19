"""SMART on FHIR EHR launch: issuer allow-list, EHR user links, launches holding the EHR token

ADR-002 Phase 9, items 14 and 15.

- `smart_issuers`: the per-tenant allow-list of EHR FHIR servers (`iss`) that may launch DischargeFlow, with the
  client id and redirect URI the EHR registered and how its identifiers map to our MRN and visit number.
- `ehr_user_links`: which DischargeFlow user an EHR `fhirUser` is.
- `smart_launches`: one launch, pending until the code exchange, then holding the EHR access token as ciphertext
  under its own data key until it expires. Not unit-scoped: a launch is read only by the session that made it, and
  the clinical-context route first loads the patient under the caller's unit scope.

All three are tenant-scoped (usual policy). New tables only, so the previous release keeps working.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-19 23:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
TABLES = ("smart_issuers", "ehr_user_links", "smart_launches")


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f(f'fk_{table}_tenant_id_tenants'),
                                   ondelete='RESTRICT')


def upgrade() -> None:
    op.create_table('smart_issuers',
    sa.Column('issuer_id', sa.String(length=40), nullable=False),
    sa.Column('iss', sa.String(length=500), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('client_id', sa.String(length=200), nullable=False),
    sa.Column('redirect_uri', sa.String(length=500), nullable=False),
    sa.Column('mrn_system', sa.String(length=500), nullable=True),
    sa.Column('mrn_authority', sa.String(length=60), nullable=True),
    sa.Column('visit_system', sa.String(length=500), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    _tenant_fk('smart_issuers'),
    sa.PrimaryKeyConstraint('issuer_id', name=op.f('pk_smart_issuers')),
    sa.UniqueConstraint('tenant_id', 'iss', name='uq_smart_issuers_tenant_iss')
    )
    with op.batch_alter_table('smart_issuers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_smart_issuers_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('ehr_user_links',
    sa.Column('link_id', sa.String(length=40), nullable=False),
    sa.Column('issuer_id', sa.String(length=40), nullable=False),
    sa.Column('fhir_user', sa.String(length=500), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['issuer_id'], ['smart_issuers.issuer_id'],
                            name=op.f('fk_ehr_user_links_issuer_id_smart_issuers'), ondelete='CASCADE'),
    _tenant_fk('ehr_user_links'),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], name=op.f('fk_ehr_user_links_user_id_users'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('link_id', name=op.f('pk_ehr_user_links')),
    sa.UniqueConstraint('tenant_id', 'issuer_id', 'fhir_user', name='uq_ehr_user_links_fhir_user')
    )
    with op.batch_alter_table('ehr_user_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ehr_user_links_issuer_id'), ['issuer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ehr_user_links_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ehr_user_links_user_id'), ['user_id'], unique=False)

    op.create_table('smart_launches',
    sa.Column('launch_id', sa.String(length=40), nullable=False),
    sa.Column('issuer_id', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('state_hash', sa.String(length=64), nullable=False),
    sa.Column('code_challenge', sa.String(length=128), nullable=False),
    sa.Column('nonce', sa.String(length=64), nullable=False),
    sa.Column('authorize_endpoint', sa.String(length=500), nullable=False),
    sa.Column('token_endpoint', sa.String(length=500), nullable=False),
    sa.Column('jwks_uri', sa.String(length=500), nullable=True),
    sa.Column('oidc_issuer', sa.String(length=500), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('session_id', sa.String(length=40), nullable=True),
    sa.Column('user_id', sa.String(length=40), nullable=True),
    sa.Column('fhir_user', sa.String(length=500), nullable=True),
    sa.Column('scope', sa.String(length=1000), nullable=True),
    sa.Column('dek', sa.LargeBinary(), nullable=True),
    sa.Column('access_token', sa.Text(), nullable=True),
    sa.Column('ehr_patient', sa.Text(), nullable=True),
    sa.Column('ehr_encounter', sa.Text(), nullable=True),
    sa.Column('patient_id', sa.String(length=40), nullable=True),
    sa.Column('encounter_id', sa.String(length=40), nullable=True),
    sa.Column('context_note', sa.String(length=200), nullable=True),
    sa.Column('failure', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['encounter_id'], ['encounters.encounter_id'],
                            name=op.f('fk_smart_launches_encounter_id_encounters'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['issuer_id'], ['smart_issuers.issuer_id'],
                            name=op.f('fk_smart_launches_issuer_id_smart_issuers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.patient_id'],
                            name=op.f('fk_smart_launches_patient_id_patients'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['auth_sessions.session_id'],
                            name=op.f('fk_smart_launches_session_id_auth_sessions'), ondelete='CASCADE'),
    _tenant_fk('smart_launches'),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], name=op.f('fk_smart_launches_user_id_users'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('launch_id', name=op.f('pk_smart_launches')),
    sa.UniqueConstraint('state_hash', name=op.f('uq_smart_launches_state_hash'))
    )
    with op.batch_alter_table('smart_launches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_smart_launches_issuer_id'), ['issuer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_smart_launches_patient_id'), ['patient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_smart_launches_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_smart_launches_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_smart_launches_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_smart_launches_user_id'), ['user_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       "USING (tenant_id = current_setting('app.tenant_id', true)) "
                       "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.drop_table('smart_launches')
    op.drop_table('ehr_user_links')
    op.drop_table('smart_issuers')
