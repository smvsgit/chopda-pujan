"""Store user-generated media on the NAS-backed media bind.

Revision ID: d1e6f2a4c8b0
Revises: b5f92c1d7e44
Create Date: 2026-09-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e6f2a4c8b0'
down_revision = 'b5f92c1d7e44'
branch_labels = None
depends_on = None


def _columns(table):
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    manual_cols = _columns('manuals')
    with op.batch_alter_table('manuals') as batch:
        if 'file_path' not in manual_cols:
            batch.add_column(sa.Column('file_path', sa.String(length=500), nullable=True,
                                       server_default=''))
        if 'file_path_gu' not in manual_cols:
            batch.add_column(sa.Column('file_path_gu', sa.String(length=500), nullable=True,
                                       server_default=''))

    event_cols = _columns('event_config')
    with op.batch_alter_table('event_config') as batch:
        if 'poster_path' not in event_cols:
            batch.add_column(sa.Column('poster_path', sa.String(length=500), nullable=True,
                                       server_default=''))


def downgrade():
    event_cols = _columns('event_config')
    if 'poster_path' in event_cols:
        with op.batch_alter_table('event_config') as batch:
            batch.drop_column('poster_path')

    manual_cols = _columns('manuals')
    with op.batch_alter_table('manuals') as batch:
        if 'file_path_gu' in manual_cols:
            batch.drop_column('file_path_gu')
        if 'file_path' in manual_cols:
            batch.drop_column('file_path')
