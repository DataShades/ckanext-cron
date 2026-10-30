"""Add cron_worker, and who created, changed and triggered jobs

Revision ID: c2d7e5f1a8b3
Revises: 9b4e1d6a2c58
Create Date: 2026-10-18 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c2d7e5f1a8b3"
down_revision = "9b4e1d6a2c58"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cron_worker",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("started", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # no foreign key: the run may be deleted while it runs, and heartbeats must not fail then
        sa.Column("run_id", sa.BigInteger),
    )
    op.add_column("cron_job", sa.Column("created_by", sa.Text))
    op.add_column("cron_job", sa.Column("modified_by", sa.Text))
    op.add_column("cron_run", sa.Column("triggered_by", sa.Text))


def downgrade():
    op.drop_column("cron_run", "triggered_by")
    op.drop_column("cron_job", "modified_by")
    op.drop_column("cron_job", "created_by")
    op.drop_table("cron_worker")
