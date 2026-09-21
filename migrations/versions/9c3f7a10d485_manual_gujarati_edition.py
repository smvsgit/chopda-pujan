"""A Gujarati edition of each user manual

The Gujarati manual is its own PDF rather than a translated title over the
English one: a volunteer who reads Gujarati needs the pages in Gujarati, and a
document cannot be translated on the fly. Where no Gujarati file has been
uploaded the English one is still served, so a manual link never breaks.

Revision ID: 9c3f7a10d485
Revises: 8b2d5e0f31a7
Create Date: 2026-09-09

"""
from alembic import op
import sqlalchemy as sa


revision = '9c3f7a10d485'
down_revision = '8b2d5e0f31a7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('manuals') as batch:
        batch.add_column(sa.Column('filename_gu', sa.String(length=200),
                                   nullable=True, server_default=''))
        batch.add_column(sa.Column('data_gu', sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column('size_gu', sa.Integer(), nullable=True,
                                   server_default='0'))
        batch.add_column(sa.Column('version_gu', sa.String(length=40),
                                   nullable=True, server_default=''))
        # The manual as plain prose, for reading aloud. A Gujarati PDF stores
        # shaped glyphs in visual order, so text taken back out of it is
        # useless to a speech voice; this is the same words in the order they
        # were written.
        batch.add_column(sa.Column('speech_en', sa.Text(), nullable=True))
        batch.add_column(sa.Column('speech_gu', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('manuals') as batch:
        batch.drop_column('speech_gu')
        batch.drop_column('speech_en')
        batch.drop_column('version_gu')
        batch.drop_column('size_gu')
        batch.drop_column('data_gu')
        batch.drop_column('filename_gu')
