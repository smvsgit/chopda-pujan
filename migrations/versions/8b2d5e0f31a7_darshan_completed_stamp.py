"""Guruji Darshan, stage two: completed at Guruji's desk

darshan_at already records the attendance desk sending someone to darshan -
the single green tick, seat number unchanged. This adds the second stage:
darshan_done_at, stamped when Guruji's desk presses Save & Next, which is the
double green tick.

Two columns rather than one status word, because each stage is stamped by a
different desk at a different moment, and the counts either side of it -
waiting to be seen, and seen - are what both screens are built on.

Revision ID: 8b2d5e0f31a7
Revises: 7a1c4d9e2b58
Create Date: 2026-09-08

"""
from alembic import op
import sqlalchemy as sa


revision = '8b2d5e0f31a7'
down_revision = '7a1c4d9e2b58'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('attendance') as batch:
        batch.add_column(sa.Column('darshan_done_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('darshan_done_by', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_attendance_darshan_done_by', 'users',
                                 ['darshan_done_by'], ['id'])


def downgrade():
    with op.batch_alter_table('attendance') as batch:
        batch.drop_constraint('fk_attendance_darshan_done_by', type_='foreignkey')
        batch.drop_column('darshan_done_by')
        batch.drop_column('darshan_done_at')
