"""PostgreSQL role, grants and row-level-security policies for tenant isolation.

Migration 0002 applies a frozen copy of this SQL; the test suite applies it to databases built with create_all and
checks that the migrated schema has the same policies.
"""
from app.core.tenancy import APP_ROLE

# Tables whose rows belong to a tenant. Every one has a tenant_id column and an isolation policy.
TENANT_TABLES = (
    "users",
    "auth_sessions",
    "patients",
    "workflows",
    "tasks",
    "discharge_summaries",
    "notifications",
    "insurance_claims",
    "audit_logs",
    "audit_chain_heads",
    "ai_generations",
    "assistant_actions",
    "integration_clients",
    "hl7_messages",
)
# Shared reference data the app may read but never change.
READ_ONLY_TABLES = ("tenants", "role_permissions")
# Append-only: the app may insert and read, never update or delete.
APPEND_ONLY_TABLES = ("audit_logs",)

POLICY_NAME = "tenant_isolation"


def install_statements() -> list[str]:
    stmts = [
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') "
        f"THEN CREATE ROLE {APP_ROLE} NOLOGIN; END IF; END $$",
        # Lets the connecting (owner) role SET ROLE to the app role; no password or login involved.
        f"DO $$ BEGIN IF NOT pg_has_role(CURRENT_USER, '{APP_ROLE}', 'SET') "
        f"THEN GRANT {APP_ROLE} TO CURRENT_USER; END IF; END $$",
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
    ]
    for table in READ_ONLY_TABLES:
        stmts.append(f"GRANT SELECT ON {table} TO {APP_ROLE}")
    for table in TENANT_TABLES:
        privileges = "SELECT, INSERT" if table in APPEND_ONLY_TABLES else "SELECT, INSERT, UPDATE, DELETE"
        stmts += [
            f"GRANT {privileges} ON {table} TO {APP_ROLE}",
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}",
            f"CREATE POLICY {POLICY_NAME} ON {table} "
            f"USING (tenant_id = current_setting('app.tenant_id', true)) "
            f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))",
        ]
    return stmts


def uninstall_statements() -> list[str]:
    stmts = []
    for table in TENANT_TABLES:
        stmts += [
            f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}",
            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
            f"REVOKE ALL ON {table} FROM {APP_ROLE}",
        ]
    for table in READ_ONLY_TABLES:
        stmts.append(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    stmts.append(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    return stmts


# Append-only enforcement that applies to every role, including the owner.
APPEND_ONLY_TRIGGER_PG = [
    """CREATE OR REPLACE FUNCTION audit_logs_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only (% blocked)', TG_OP;
END $$""",
    "DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs",
    "CREATE TRIGGER audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs "
    "FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only()",
]
APPEND_ONLY_TRIGGER_SQLITE = [
    "CREATE TRIGGER IF NOT EXISTS audit_logs_no_update BEFORE UPDATE ON audit_logs "
    "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only (UPDATE blocked)'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete BEFORE DELETE ON audit_logs "
    "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only (DELETE blocked)'); END",
]
