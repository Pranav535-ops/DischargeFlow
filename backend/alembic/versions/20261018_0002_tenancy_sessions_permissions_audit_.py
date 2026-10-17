"""tenancy, sessions, permissions, audit chain, PHI columns

ADR-001 Phase 2. Existing rows join the default tenant, existing audit entries are chained in timestamp order, and
role permissions are seeded. Patient identifiers are encrypted afterwards by `python -m app.cli migrate` (needs
PHI_KEK, which migrations deliberately don't). On PostgreSQL this also creates the `dischargeflow_app` role,
row-level-security policies and an append-only trigger on audit_logs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18 01:41:02.532548
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT = os.environ.get("DEFAULT_TENANT_ID", "default").strip() or "default"
GENESIS_HASH = "0" * 64
APP_ROLE = "dischargeflow_app"

# Frozen copies (migrations must not change when app code does).
TENANT_SCOPED = ("users", "patients", "workflows", "tasks", "discharge_summaries", "notifications", "insurance_claims")
RLS_TABLES = TENANT_SCOPED + ("auth_sessions", "audit_logs", "audit_chain_heads")
READ_ONLY_TABLES = ("tenants", "role_permissions")
ROLE_PERMISSIONS = [
    ("admin", "audit:read"), ("admin", "audit:read_patient"), ("admin", "patient:erase"), ("admin", "patient:write"),
    ("admin", "summary:manage"), ("admin", "task:create"), ("admin", "user:manage"), ("admin", "workflow:manage"),
    ("nurse", "patient:write"), ("nurse", "task:create"),
    ("physician", "audit:read_patient"), ("physician", "patient:write"), ("physician", "summary:manage"),
    ("physician", "task:create"), ("physician", "workflow:manage"),
]
AUDIT_HASHED_FIELDS = (
    "tenant_id", "seq", "log_id", "user_id", "user_name", "action", "resource_type", "resource_id", "details",
    "ip_address", "timestamp", "prev_hash",
)


def _audit_hash(row: dict) -> str:
    payload = {}
    for name in AUDIT_HASHED_FIELDS:
        value = row[name]
        if hasattr(value, "isoformat"):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.isoformat()
        payload[name] = value
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _add_tenant_column(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=40), nullable=False, server_default=DEFAULT_TENANT))
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.alter_column('tenant_id', server_default=None, existing_type=sa.String(length=40), existing_nullable=False)
        batch_op.create_index(batch_op.f(f'ix_{table}_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f(f'fk_{table}_tenant_id_tenants'), 'tenants', ['tenant_id'], ['tenant_id'], ondelete='RESTRICT')


def _chain_existing_audit_entries(bind) -> tuple[int, str]:
    audit = sa.table(
        'audit_logs', *(sa.column(c) for c in AUDIT_HASHED_FIELDS if c not in ('timestamp', 'seq', 'prev_hash', 'tenant_id')),
        sa.column('timestamp', sa.DateTime(timezone=True)),
    )
    rows = bind.execute(sa.select(audit).order_by(audit.c.timestamp, audit.c.log_id)).mappings().all()
    prev, seq = GENESIS_HASH, 0
    for row in rows:
        seq += 1
        entry = {**row, "tenant_id": DEFAULT_TENANT, "seq": seq, "prev_hash": prev}
        digest = _audit_hash(entry)
        bind.execute(
            sa.text("UPDATE audit_logs SET tenant_id = :t, seq = :s, prev_hash = :p, hash = :h WHERE log_id = :id"),
            {"t": DEFAULT_TENANT, "s": seq, "p": prev, "h": digest, "id": row["log_id"]},
        )
        prev = digest
    return seq, prev


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.create_table('tenants',
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', name=op.f('pk_tenants'))
    )
    op.bulk_insert(sa.table('tenants', sa.column('tenant_id'), sa.column('name'), sa.column('created_at', sa.DateTime(timezone=True))),
                   [{"tenant_id": DEFAULT_TENANT, "name": "Default Hospital", "created_at": datetime.now(timezone.utc)}])

    op.create_table('role_permissions',
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('permission', sa.String(length=60), nullable=False),
    sa.PrimaryKeyConstraint('role', 'permission', name=op.f('pk_role_permissions'))
    )
    op.bulk_insert(sa.table('role_permissions', sa.column('role'), sa.column('permission')),
                   [{"role": r, "permission": p} for r, p in ROLE_PERMISSIONS])

    for table in TENANT_SCOPED:
        _add_tenant_column(table)

    op.create_table('auth_sessions',
    sa.Column('session_id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('current_hash', sa.String(length=64), nullable=False),
    sa.Column('previous_hash', sa.String(length=64), nullable=True),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_reason', sa.String(length=40), nullable=True),
    sa.Column('user_agent', sa.String(length=300), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_auth_sessions_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], name=op.f('fk_auth_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', name=op.f('pk_auth_sessions'))
    )
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_auth_sessions_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_auth_sessions_user_id'), ['user_id'], unique=False)

    # Audit chain: add columns nullable, chain existing entries, then tighten.
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('seq', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('prev_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('hash', sa.String(length=64), nullable=True))
    last_seq, last_hash = _chain_existing_audit_entries(bind)
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        for col, type_ in (('tenant_id', sa.String(length=40)), ('seq', sa.Integer()),
                           ('prev_hash', sa.String(length=64)), ('hash', sa.String(length=64))):
            batch_op.alter_column(col, existing_type=type_, nullable=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_unique_constraint('uq_audit_logs_tenant_seq', ['tenant_id', 'seq'])
        batch_op.create_foreign_key(batch_op.f('fk_audit_logs_tenant_id_tenants'), 'tenants', ['tenant_id'], ['tenant_id'], ondelete='RESTRICT')

    op.create_table('audit_chain_heads',
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.Column('last_seq', sa.Integer(), nullable=False),
    sa.Column('last_hash', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_audit_chain_heads_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('tenant_id', name=op.f('pk_audit_chain_heads'))
    )
    op.bulk_insert(sa.table('audit_chain_heads', sa.column('tenant_id'), sa.column('last_seq'), sa.column('last_hash')),
                   [{"tenant_id": DEFAULT_TENANT, "last_seq": last_seq, "last_hash": last_hash}])

    # Patient identifiers become ciphertext: wider columns, a per-patient wrapped key, no plaintext index.
    with op.batch_alter_table('patients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('phi_dek', sa.LargeBinary(), nullable=True))
        batch_op.alter_column('name', existing_type=sa.VARCHAR(length=200), type_=sa.Text(), existing_nullable=False)
        batch_op.alter_column('date_of_birth', existing_type=sa.VARCHAR(length=40), type_=sa.Text(), existing_nullable=False)
        batch_op.alter_column('diagnosis', existing_type=sa.VARCHAR(length=1000), type_=sa.Text(), existing_nullable=False)
        batch_op.drop_index(batch_op.f('ix_patients_name'))

    if is_pg:
        op.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') "
                   f"THEN CREATE ROLE {APP_ROLE} NOLOGIN; END IF; END $$")
        # Lets the owner SET ROLE to the app role. Skipped when already possible (e.g. superusers, re-runs).
        op.execute(f"DO $$ BEGIN IF NOT pg_has_role(CURRENT_USER, '{APP_ROLE}', 'SET') "
                   f"THEN GRANT {APP_ROLE} TO CURRENT_USER; END IF; END $$")
        op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        for table in READ_ONLY_TABLES:
            op.execute(f"GRANT SELECT ON {table} TO {APP_ROLE}")
        for table in RLS_TABLES:
            privileges = "SELECT, INSERT" if table == "audit_logs" else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       f"USING (tenant_id = current_setting('app.tenant_id', true)) "
                       f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")
        op.execute("""CREATE OR REPLACE FUNCTION audit_logs_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only (% blocked)', TG_OP;
END $$""")
        op.execute("CREATE TRIGGER audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs "
                   "FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only()")
    else:
        op.execute("CREATE TRIGGER audit_logs_no_update BEFORE UPDATE ON audit_logs "
                   "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only (UPDATE blocked)'); END")
        op.execute("CREATE TRIGGER audit_logs_no_delete BEFORE DELETE ON audit_logs "
                   "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only (DELETE blocked)'); END")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS audit_logs_append_only()")
        for table in RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        for table in READ_ONLY_TABLES:
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
        # The role itself is cluster-wide and harmless without grants; it is left in place.
    else:
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update")
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_delete")

    # Encrypted identifiers can't be narrowed back to VARCHAR without decrypting first (PHI_KEK isn't available
    # to migrations), so the columns stay TEXT; only the index and key column are reverted.
    with op.batch_alter_table('patients', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_patients_name'), ['name'], unique=False)
        batch_op.drop_column('phi_dek')

    op.drop_table('audit_chain_heads')
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_audit_logs_tenant_id_tenants'), type_='foreignkey')
        batch_op.drop_constraint('uq_audit_logs_tenant_seq', type_='unique')
        batch_op.drop_index(batch_op.f('ix_audit_logs_tenant_id'))
        batch_op.drop_column('hash')
        batch_op.drop_column('prev_hash')
        batch_op.drop_column('seq')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_auth_sessions_user_id'))
        batch_op.drop_index(batch_op.f('ix_auth_sessions_tenant_id'))
    op.drop_table('auth_sessions')

    for table in reversed(TENANT_SCOPED):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(batch_op.f(f'fk_{table}_tenant_id_tenants'), type_='foreignkey')
            batch_op.drop_index(batch_op.f(f'ix_{table}_tenant_id'))
            batch_op.drop_column('tenant_id')

    op.drop_table('role_permissions')
    op.drop_table('tenants')
