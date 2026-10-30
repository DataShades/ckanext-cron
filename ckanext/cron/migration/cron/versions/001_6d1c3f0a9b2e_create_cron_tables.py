"""Create cron_job and cron_run tables

Revision ID: 6d1c3f0a9b2e
Revises:
Create Date: 2026-10-17 12:00:00.000000

"""

import logging

import sqlalchemy as sa
from alembic import op

log = logging.getLogger(__name__)

revision = "6d1c3f0a9b2e"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cron_job",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("modified", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "cron_run",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.Text, sa.ForeignKey("cron_job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger", sa.Text, nullable=False, server_default="schedule"),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started", sa.DateTime(timezone=True)),
        sa.Column("finished", sa.DateTime(timezone=True)),
        sa.Column("worker", sa.Text),
        sa.Column("exit_code", sa.Integer),
        sa.Column("stdout", sa.Text),
        sa.Column("stderr", sa.Text),
    )
    op.create_index("idx_cron_run_job_id", "cron_run", ["job_id"])
    op.create_index("idx_cron_run_status", "cron_run", ["status"])


def downgrade():
    _unschedule_pg_cron_jobs()
    op.drop_table("cron_run")
    op.drop_table("cron_job")


def _unschedule_pg_cron_jobs():
    """Remove the pg_cron jobs of this CKAN database, which would fail on every run without the tables."""
    if op.get_context().as_sql:
        return

    from ckanext.cron import config, pg_cron
    from ckanext.cron.model import PG_CRON_PREFIX

    try:
        for pg_job in pg_cron.find_jobs_by_prefix(PG_CRON_PREFIX, config.ckan_database()):
            pg_cron.unschedule_job(pg_job["jobid"])
    except pg_cron.PgCronError as e:
        log.warning("Cannot unschedule the pg_cron jobs of ckanext-cron, remove them by hand: %s", e)
