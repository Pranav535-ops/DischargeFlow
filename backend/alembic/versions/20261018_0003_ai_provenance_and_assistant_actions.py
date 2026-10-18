"""ai provenance and assistant actions

ADR-001 Phase 3. `ai_generations` records provenance for every LLM call (model, prompt version, input hash, tokens,
latency, grounding warnings; never prompt or output text). `assistant_actions` holds writes the assistant proposed
until the user confirms them. Discharge summaries gain the structured draft, a link to their generation and the
physician's edit diff. Both new tables are tenant-scoped with the same RLS policy as every other tenant table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18 15:15:55.239061
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

APP_ROLE = "dischargeflow_app"
# Frozen copy (migrations must not change when app code does).
NEW_TENANT_TABLES = ("ai_generations", "assistant_actions")

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('ai_generations',
    sa.Column('generation_id', sa.String(length=40), nullable=False),
    sa.Column('purpose', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('workflow_id', sa.String(length=40), nullable=True),
    sa.Column('patient_id', sa.String(length=40), nullable=True),
    sa.Column('provider', sa.String(length=20), nullable=False),
    sa.Column('model_id', sa.String(length=100), nullable=False),
    sa.Column('prompt_version', sa.String(length=60), nullable=False),
    sa.Column('prompt_sha256', sa.String(length=64), nullable=False),
    sa.Column('input_hash', sa.String(length=64), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('grounding_warnings', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_ai_generations_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('generation_id', name=op.f('pk_ai_generations'))
    )
    with op.batch_alter_table('ai_generations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_generations_prompt_version'), ['prompt_version'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_generations_purpose'), ['purpose'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_generations_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_generations_workflow_id'), ['workflow_id'], unique=False)

    op.create_table('assistant_actions',
    sa.Column('action_id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('tool', sa.String(length=60), nullable=False),
    sa.Column('arguments', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tenant_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], name=op.f('fk_assistant_actions_tenant_id_tenants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('action_id', name=op.f('pk_assistant_actions'))
    )
    with op.batch_alter_table('assistant_actions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assistant_actions_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_assistant_actions_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('discharge_summaries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('structured', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))
        batch_op.add_column(sa.Column('generation_id', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('original_content', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('edit_diff', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('edit_similarity', sa.Float(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_discharge_summaries_generation_id_ai_generations'), 'ai_generations', ['generation_id'], ['generation_id'], ondelete='SET NULL')

    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_TENANT_TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                       f"USING (tenant_id = current_setting('app.tenant_id', true)) "
                       f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_TENANT_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    with op.batch_alter_table('discharge_summaries', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_discharge_summaries_generation_id_ai_generations'), type_='foreignkey')
        batch_op.drop_column('edit_similarity')
        batch_op.drop_column('edit_diff')
        batch_op.drop_column('original_content')
        batch_op.drop_column('generation_id')
        batch_op.drop_column('structured')

    with op.batch_alter_table('assistant_actions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_assistant_actions_user_id'))
        batch_op.drop_index(batch_op.f('ix_assistant_actions_tenant_id'))

    op.drop_table('assistant_actions')
    with op.batch_alter_table('ai_generations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_generations_workflow_id'))
        batch_op.drop_index(batch_op.f('ix_ai_generations_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_ai_generations_purpose'))
        batch_op.drop_index(batch_op.f('ix_ai_generations_prompt_version'))

    op.drop_table('ai_generations')
