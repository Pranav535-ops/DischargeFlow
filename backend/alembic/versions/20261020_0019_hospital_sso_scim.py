"""hospital SSO per tenant and SCIM provisioning

ADR-002 Phase 7, item 2. `identity_providers` (shared, like `tenants`: the app role may only read it; operators add
rows with `python -m app.cli sso-provider`) maps an OIDC issuer and client id to a tenant. `idp_group_mappings`
(tenant-scoped, usual policy) maps a directory group to a role and/or a unit. `users` gains `external_id` (SCIM
externalId) and `deactivated_at` (SCIM active=false). New permission `user:provision` for the `interface` role (the
directory's SCIM connector as an integration client); admin holds every permission.

Additive: nothing changes for existing rows, and the previous release ignores the new columns and tables.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20 09:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "dischargeflow_app"
# Frozen copy (migrations must not change when app code does).
ROWS = [("admin", "user:provision"), ("interface", "user:provision")]


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('external_id', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table('identity_providers',
    sa.Column('provider_id', sa.String(length=40), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('issuer', sa.String(length=500), nullable=False),
    sa.Column('client_id', sa.String(length=200), nullable=False),
    sa.Column('authorize_url', sa.String(length=500), nullable=False),
    sa.Column('token_url', sa.String(length=500), nullable=False),
    sa.Column('jwks_url', sa.String(length=500), nullable=True),
    sa.Column('scopes', sa.String(length=200), nullable=False),
    sa.Column('groups_claim', sa.String(length=100), nullable=False),
    sa.Column('require_mfa', sa.Boolean(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_identity_providers_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('provider_id', name=op.f('pk_identity_providers')),
    sa.UniqueConstraint('issuer', 'client_id', name='uq_identity_providers_issuer_client')
    )
    with op.batch_alter_table('identity_providers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_identity_providers_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('idp_group_mappings',
    sa.Column('mapping_id', sa.String(length=40), nullable=False),
    sa.Column('provider_id', sa.String(length=40), nullable=False),
    sa.Column('group_name', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=True),
    sa.Column('unit_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_idp_group_mappings_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['unit_id'], ['units.unit_id'], name=op.f('fk_idp_group_mappings_unit_id_units'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('mapping_id', name=op.f('pk_idp_group_mappings')),
    sa.UniqueConstraint('tenant_id', 'provider_id', 'group_name', name='uq_idp_group_mappings_group')
    )
    with op.batch_alter_table('idp_group_mappings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_idp_group_mappings_provider_id'), ['provider_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_idp_group_mappings_tenant_id'), ['tenant_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"GRANT SELECT ON identity_providers TO {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON idp_group_mappings TO {APP_ROLE}")
        op.execute("ALTER TABLE idp_group_mappings ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON idp_group_mappings "
                   "USING (tenant_id = current_setting('app.tenant_id', true)) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")

    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROWS])


def downgrade() -> None:
    for role, permission in ROWS:
        op.execute(sa.text("DELETE FROM role_permissions WHERE role = :r AND permission = :p")
                   .bindparams(r=role, p=permission))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON idp_group_mappings")
        op.execute(f"REVOKE ALL ON idp_group_mappings FROM {APP_ROLE}")
        op.execute(f"REVOKE ALL ON identity_providers FROM {APP_ROLE}")
    with op.batch_alter_table('idp_group_mappings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_idp_group_mappings_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_idp_group_mappings_provider_id'))
    op.drop_table('idp_group_mappings')
    with op.batch_alter_table('identity_providers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_identity_providers_tenant_id'))
    op.drop_table('identity_providers')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('deactivated_at')
        batch_op.drop_column('external_id')
