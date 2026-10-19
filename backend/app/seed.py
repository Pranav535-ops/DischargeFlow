"""Idempotent demo data. Replaces the old POST /api/seed and the lazy re-seeding inside read endpoints."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.tenancy import use_tenant
from app.identity.models import Tenant
from app.patients.models import Patient
from app.patients.service import open_encounter

DEMO_PATIENTS = [
    {"name": "John Smith", "date_of_birth": "1958-03-15", "admission_date": "2026-01-10", "room_number": "301A",
     "diagnosis": "Pneumonia with respiratory complications", "attending_physician": "Dr. Sarah Johnson", "status": "admitted"},
    {"name": "Maria Garcia", "date_of_birth": "1975-08-22", "admission_date": "2026-01-08", "room_number": "215B",
     "diagnosis": "Post-operative recovery - knee replacement", "attending_physician": "Dr. Michael Chen", "status": "admitted"},
    {"name": "Robert Johnson", "date_of_birth": "1990-12-01", "admission_date": "2026-01-11", "room_number": "108",
     "diagnosis": "Acute appendicitis - appendectomy", "attending_physician": "Dr. Sarah Johnson", "status": "admitted"},
    {"name": "Emily Watson", "date_of_birth": "1982-05-30", "admission_date": "2026-01-09", "room_number": "402",
     "diagnosis": "Diabetic ketoacidosis management", "attending_physician": "Dr. James Williams", "status": "admitted"},
]


async def ensure_tenant(db: AsyncSession, tenant_id: str, name: str) -> None:
    if await db.get(Tenant, tenant_id) is None:
        db.add(Tenant(tenant_id=tenant_id, name=name))
        await db.flush()


async def seed_demo_data(db: AsyncSession, tenant_id: str | None = None) -> int:
    """Insert demo patients into a tenant with no patients. Returns how many were inserted."""
    tenant_id = tenant_id or settings.default_tenant_id
    await ensure_tenant(db, tenant_id, "Default Hospital")
    await use_tenant(db, tenant_id)
    if await db.scalar(select(func.count()).select_from(Patient).where(Patient.tenant_id == tenant_id)):
        return 0
    for fields in DEMO_PATIENTS:
        patient = Patient(**fields)
        db.add(patient)
        await db.flush()
        await open_encounter(db, patient)
    return len(DEMO_PATIENTS)
