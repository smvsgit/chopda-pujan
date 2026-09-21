"""Whether a corrected email address is applied straight away

Split out of a4e81b2c9f30 rather than added to it. That revision had already
been applied, so amending the file changed nothing: Alembic had the revision
stamped and never ran it again, and the column stayed missing until every
query touching event_config failed.

Added conditionally, so this is safe whether the column arrived through the
amended file, through LEGACY_ALTERS on boot, or not at all.

Revision ID: b5f92c1d7e44
Revises: a4e81b2c9f30
Create Date: 2026-09-09

"""
from alembic import op
import sqlalchemy as sa


revision = 'b5f92c1d7e44'
down_revision = 'a4e81b2c9f30'
branch_labels = None
depends_on = None

COLUMN = 'selfserve_auto_email'


def _has_column():
    bind = op.get_bind()
    return COLUMN in {c['name'] for c in
                      sa.inspect(bind).get_columns('event_config')}


def upgrade():
    if _has_column():
        return
    with op.batch_alter_table('event_config') as batch:
        batch.add_column(sa.Column(COLUMN, sa.Boolean(), nullable=False,
                                   server_default=sa.true()))


def downgrade():
    if not _has_column():
        return
    with op.batch_alter_table('event_config') as batch:
        batch.drop_column(COLUMN)
