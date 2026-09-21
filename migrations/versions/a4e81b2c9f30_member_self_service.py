"""What a member may change about themselves

A signed link in a message opens a page of their own where they can correct
their firm names or add one. Nothing else: not amounts, not seats, not passes.

Every submission is recorded in member_requests, including the ones applied
straight away, so there is one place to look afterwards - and turning the auto
settings off later is then a change of policy rather than a change of history.

Revision ID: a4e81b2c9f30
Revises: 9c3f7a10d485
Create Date: 2026-09-09

"""
from alembic import op
import sqlalchemy as sa


revision = 'a4e81b2c9f30'
down_revision = '9c3f7a10d485'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'member_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('member_id', sa.Integer(), nullable=False),
        sa.Column('pujan_year', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('entity_type', sa.String(length=20), nullable=True),
        sa.Column('old_name', sa.String(length=200), nullable=True),
        sa.Column('new_name', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('auto', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('from_ip', sa.String(length=60), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('decided_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['member_id'], ['members.id']),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id']),
        sa.ForeignKeyConstraint(['decided_by'], ['users.id']),
    )
    op.create_index('ix_member_requests_status', 'member_requests',
                    ['status', 'created_at'])
    with op.batch_alter_table('event_config') as batch:
        batch.add_column(sa.Column('selfserve_open', sa.Boolean(),
                                   nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column('selfserve_until', sa.Date(), nullable=True))
        batch.add_column(sa.Column('selfserve_auto_add', sa.Boolean(),
                                   nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column('selfserve_auto_edit', sa.Boolean(),
                                   nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table('event_config') as batch:
        batch.drop_column('selfserve_auto_edit')
        batch.drop_column('selfserve_auto_add')
        batch.drop_column('selfserve_until')
        batch.drop_column('selfserve_open')
    op.drop_index('ix_member_requests_status', table_name='member_requests')
    op.drop_table('member_requests')
