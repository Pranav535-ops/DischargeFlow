"""Idempotent demo data. Replaces the old POST /api/seed and the lazy re-seeding inside read endpoints."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.patients.models import Patient

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


async def seed_demo_data(db: AsyncSession) -> int:
    """Insert demo patients into an empty database. Returns how many were inserted (0 if data already exists)."""
    if await db.scalar(select(func.count()).select_from(Patient)):
        return 0
    db.add_all(Patient(**p) for p in DEMO_PATIENTS)
    await db.flush()
    return len(DEMO_PATIENTS)
