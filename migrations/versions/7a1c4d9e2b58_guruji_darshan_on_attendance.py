"""Guruji Darshan stamp on the attendance row

Darshan is recorded on the existing attendance row rather than in a table of
its own: it happens after arrival, belongs to the same person on the same day,
and is read against the seat number that row already carries. One row per
person per year also keeps "present but no darshan" a single comparison
instead of an outer join.

Revision ID: 7a1c4d9e2b58
Revises: 66b672165732
Create Date: 2026-09-08

"""
from alembic import op
import sqlalchemy as sa


revision = '7a1c4d9e2b58'
down_revision = '66b672165732'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('attendance') as batch:
        batch.add_column(sa.Column('darshan_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('darshan_by', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_attendance_darshan_by', 'users',
                                 ['darshan_by'], ['id'])


def downgrade():
    with op.batch_alter_table('attendance') as batch:
        batch.drop_constraint('fk_attendance_darshan_by', type_='foreignkey')
        batch.drop_column('darshan_by')
        batch.drop_column('darshan_at')
