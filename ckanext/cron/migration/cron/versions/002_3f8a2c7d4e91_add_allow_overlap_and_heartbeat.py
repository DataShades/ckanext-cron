"""Add cron_job.allow_overlap and cron_run.heartbeat

Revision ID: 3f8a2c7d4e91
Revises: 6d1c3f0a9b2e
Create Date: 2026-10-17 22:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "3f8a2c7d4e91"
down_revision = "6d1c3f0a9b2e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cron_job", sa.Column("allow_overlap", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("cron_run", sa.Column("heartbeat", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("cron_run", "heartbeat")
    op.drop_column("cron_job", "allow_overlap")
