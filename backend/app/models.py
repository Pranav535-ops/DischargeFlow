"""Import every module's models so Base.metadata is complete (used by Alembic and tests)."""
from app.ai.models import DischargeSummary
from app.audit.models import AuditChainHead, AuditLog
from app.core.db import Base
from app.discharge.models import Task, Workflow
from app.identity.models import AuthSession, RolePermission, Tenant, User
from app.insurance.models import InsuranceClaim
from app.notifications.models import Notification
from app.patients.models import Patient

__all__ = [
    "Base",
    "AuditChainHead",
    "AuditLog",
    "AuthSession",
    "DischargeSummary",
    "InsuranceClaim",
    "Notification",
    "Patient",
    "RolePermission",
    "Task",
    "Tenant",
    "User",
    "Workflow",
]
