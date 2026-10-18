"""encrypt discharge summary text

Discharge summaries carry names and clinical details, so `content`, `original_content`, `edit_diff` and
`structured` are now AES-256-GCM ciphertext under the owning patient's data key (app/ai/models.py). The text columns
keep their type; `structured` changes from JSON(B) to text because it now holds the ciphertext of its JSON.

The schema change only reinterprets existing values (JSON as its text form). Encrypting existing rows is the
backfill in `app.core.migrate.encrypt_legacy_phi`, which `python -m app.cli migrate` (and AUTO_MIGRATE) runs after
`upgrade head`. Downgrade does not decrypt: ciphertext in `structured` is kept as a JSON string.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('discharge_summaries', schema=None) as batch_op:
        batch_op.alter_column('structured', existing_type=JSON_DOC, type_=sa.Text(), existing_nullable=True,
                              postgresql_using='structured::text')


def downgrade() -> None:
    with op.batch_alter_table('discharge_summaries', schema=None) as batch_op:
        batch_op.alter_column('structured', existing_type=sa.Text(), type_=JSON_DOC, existing_nullable=True,
                              postgresql_using="CASE WHEN structured LIKE 'enc:v1:%' OR structured = '[REDACTED]' "
                                               "THEN to_jsonb(structured) ELSE structured::jsonb END")
