"""Readmission risk score (LACE) on the encounter

ADR-002 Phase 9, item 20. The stay records the score's inputs (PV1-4 admission type, PV1-2 patient class, the
Charlson comorbidity flags as ciphertext under the patient's data key, and an ED-visit count staff may record) and
the score as last computed (total, version, per-component detail with no condition names, time).

Additive and nullable, so the previous release is unaffected. Existing stays have no score until their plan is next
read, when app.risk.service.ensure_current computes it (the ED-visit count needs the patient's other encounters, so
it is not done in SQL here).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-19 23:30:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = [
    ('admission_type', sa.String(length=2)),
    ('patient_class', sa.String(length=2)),
    ('comorbidities', sa.Text()),
    ('ed_visits_recorded', sa.Integer()),
    ('readmission_score', sa.Integer()),
    ('readmission_score_version', sa.String(length=20)),
    ('readmission_score_detail', sa.JSON()),
    ('readmission_scored_at', sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column('encounters', sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column('encounters', name)
