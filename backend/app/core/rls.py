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
    "units",
    "user_units",
    "access_grants",
    "downtime_periods",
    "encounters",
    "escalations",
    "discharge_summary_versions",
    "discharge_summary_addenda",
    "workflow_templates",
    "delay_reasons",
    "hl7_parked_messages",
    "delivery_endpoints",
    "discharge_recipients",
    "summary_deliveries",
    "bed_turnovers",
    "smart_issuers",
    "ehr_user_links",
    "smart_launches",
    "patient_instructions",
)
# Shared reference data the app may read but never change.
READ_ONLY_TABLES = ("tenants", "role_permissions")
# Append-only: the app may insert and read, never update or delete.
APPEND_ONLY_TABLES = ("audit_logs",)

POLICY_NAME = "tenant_isolation"

# Unit scope (ADR-002 item 3). A RESTRICTIVE policy, so it narrows what tenant_isolation allows. Inactive unless the
# request set app.unit_scope = 'on' (core/tenancy.use_access_scope): migrations, the CLI, login, the ADT feed and
# unscoped users see the whole tenant. It limits reads, updates and deletes; inserts are checked by tenant only.
UNIT_SCOPE_POLICY = "unit_scope"
_SCOPE_OFF = "current_setting('app.unit_scope', true) IS DISTINCT FROM 'on'"
PATIENT_VISIBLE = (
    f"{_SCOPE_OFF} OR unit_id IS NULL "
    "OR unit_id = ANY (string_to_array(current_setting('app.unit_ids', true), ',')) "
    "OR EXISTS (SELECT 1 FROM access_grants g WHERE g.patient_id = patients.patient_id "
    "AND g.user_id = current_setting('app.user_id', true) AND g.expires_at > now())"
)
# Rows about one patient are visible when the patient is: the subquery itself runs under the patients policies.
UNIT_SCOPED_CHILD_TABLES = ("workflows", "tasks", "discharge_summaries", "insurance_claims", "encounters",
                            "discharge_summary_versions", "discharge_summary_addenda",
                            "delay_reasons", "patient_instructions", "discharge_recipients",
                            "summary_deliveries")
# Rows about a place rather than a patient (ADR-002 item 17: beds being cleaned) are visible by their own unit_id.
UNIT_SCOPED_LOCATION_TABLES = ("bed_turnovers",)
UNIT_SCOPED_TABLES = ("patients",) + UNIT_SCOPED_CHILD_TABLES + UNIT_SCOPED_LOCATION_TABLES


LOCATION_VISIBLE = (
    f"{_SCOPE_OFF} OR unit_id IS NULL "
    "OR unit_id = ANY (string_to_array(current_setting('app.unit_ids', true), ','))"
)


def _child_visible(table: str) -> str:
    return f"{_SCOPE_OFF} OR EXISTS (SELECT 1 FROM patients p WHERE p.patient_id = {table}.patient_id)"


# Lets the API tell "outside your units" (403, offer break-glass) from "no such patient" (404): RLS hides both.
# SECURITY DEFINER runs as the owner, who bypasses RLS, so it checks the tenant itself. Returns a boolean only.
PATIENT_EXISTS_FUNCTION = (
    "CREATE OR REPLACE FUNCTION df_patient_exists(pid text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
    "SET search_path = public AS $$ SELECT EXISTS (SELECT 1 FROM patients WHERE patient_id = pid "
    "AND tenant_id = current_setting('app.tenant_id', true)) $$"
)


def unit_scope_statements() -> list[str]:
    stmts = []
    for table in UNIT_SCOPED_TABLES:
        using = (PATIENT_VISIBLE if table == "patients" else
                 LOCATION_VISIBLE if table in UNIT_SCOPED_LOCATION_TABLES else _child_visible(table))
        stmts += [
            f"DROP POLICY IF EXISTS {UNIT_SCOPE_POLICY} ON {table}",
            f"CREATE POLICY {UNIT_SCOPE_POLICY} ON {table} AS RESTRICTIVE USING ({using}) WITH CHECK (true)",
        ]
    return stmts + [
        PATIENT_EXISTS_FUNCTION,
        "REVOKE ALL ON FUNCTION df_patient_exists(text) FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION df_patient_exists(text) TO {APP_ROLE}",
    ]


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
    return stmts + unit_scope_statements()


def drop_unit_scope_statements() -> list[str]:
    """Drop the unit_scope policies wherever their tables exist. The patients policy references access_grants, so
    it must go before tables are dropped (tests rebuild the schema with drop_all)."""
    return [
        f"DO $$ BEGIN IF to_regclass('{table}') IS NOT NULL THEN "
        f"DROP POLICY IF EXISTS {UNIT_SCOPE_POLICY} ON {table}; END IF; END $$"
        for table in UNIT_SCOPED_TABLES
    ]


def uninstall_statements() -> list[str]:
    stmts = [f"DROP POLICY IF EXISTS {UNIT_SCOPE_POLICY} ON {table}" for table in UNIT_SCOPED_TABLES]
    stmts.append("DROP FUNCTION IF EXISTS df_patient_exists(text)")
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
# ADR-002 item 21: announce each new notification on the `notifications` channel, as "<tenant>:<user>:<id>" (ids only,
# never text). Postgres delivers a NOTIFY only when the inserting transaction commits. Frozen copy in migration 0016.
NOTIFY_TRIGGER_PG = [
    """CREATE OR REPLACE FUNCTION notifications_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('notifications', NEW.tenant_id || ':' || NEW.user_id || ':' || NEW.notification_id);
    RETURN NULL;
END $$""",
    "DROP TRIGGER IF EXISTS notifications_notify ON notifications",
    "CREATE TRIGGER notifications_notify AFTER INSERT ON notifications "
    "FOR EACH ROW EXECUTE FUNCTION notifications_notify()",
]
APPEND_ONLY_TRIGGER_SQLITE = [
    "CREATE TRIGGER IF NOT EXISTS audit_logs_no_update BEFORE UPDATE ON audit_logs "
    "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only (UPDATE blocked)'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete BEFORE DELETE ON audit_logs "
    "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only (DELETE blocked)'); END",
]
