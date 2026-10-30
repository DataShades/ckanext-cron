"""Index cron_run for the queries the worker and the pages run

Revision ID: 9b4e1d6a2c58
Revises: 3f8a2c7d4e91
Create Date: 2026-10-17 23:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "9b4e1d6a2c58"
down_revision = "3f8a2c7d4e91"
branch_labels = None
depends_on = None


def upgrade():
    # most runs are finished, so a status index barely narrows anything down
    op.drop_index("idx_cron_run_status", "cron_run")
    op.drop_index("idx_cron_run_job_id", "cron_run")

    # the latest runs of a job: the jobs page and the run history of one job
    op.create_index("idx_cron_run_job_id_id", "cron_run", ["job_id", sa.text("id DESC")])
    # the oldest queued run, claimed by workers
    op.create_index("idx_cron_run_queued", "cron_run", ["id"], postgresql_where=sa.text("status = 'queued'"))
    # unfinished runs of a job, checked before queuing a new one, and runs of lost workers
    op.create_index(
        "idx_cron_run_unfinished", "cron_run", ["job_id"], postgresql_where=sa.text("status IN ('queued', 'running')")
    )
    # purging old runs
    op.create_index("idx_cron_run_created", "cron_run", ["created"])


def downgrade():
    op.drop_index("idx_cron_run_created", "cron_run")
    op.drop_index("idx_cron_run_unfinished", "cron_run")
    op.drop_index("idx_cron_run_queued", "cron_run")
    op.drop_index("idx_cron_run_job_id_id", "cron_run")
    op.create_index("idx_cron_run_job_id", "cron_run", ["job_id"])
    op.create_index("idx_cron_run_status", "cron_run", ["status"])
