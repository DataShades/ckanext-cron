from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

import ckan.plugins.toolkit as tk
from ckan.model.types import make_uuid

NOTIFY_CHANNEL = "ckanext_cron"
PG_CRON_PREFIX = "ckanext-cron-"
LOST_AFTER = timedelta(seconds=60)


class CronJob(tk.BaseModel):
    """A `ckan` CLI command scheduled with pg_cron.

    Its schedule and active flag live in the pg_cron job named `pg_cron_name`.
    """

    __tablename__ = "cron_job"

    id = sa.Column(sa.Text, primary_key=True, default=make_uuid)
    name = sa.Column(sa.Text, nullable=False, unique=True)
    # arguments of the `ckan` CLI, such as "harvester run"
    command = sa.Column(sa.Text, nullable=False)
    # whether a new run is queued while the previous one is still queued or running
    allow_overlap = sa.Column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    created = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    modified = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
    # ids of the users who created and last changed the job
    created_by = sa.Column(sa.Text)
    modified_by = sa.Column(sa.Text)

    @property
    def pg_cron_name(self) -> str:
        return f"{PG_CRON_PREFIX}{self.id}"

    def trigger_command(self) -> str:
        """SQL that pg_cron runs on schedule: enqueue a run and wake up the workers.

        The run is skipped while the previous one is unfinished, unless the job
        allows overlapping runs, and when the job no longer exists.
        """
        job_id = str(uuid.UUID(str(self.id)))
        unfinished = f"'{CronRun.Status.queued}', '{CronRun.Status.running}'"

        return (
            f"INSERT INTO {CronRun.__tablename__} (job_id, trigger)"
            f" SELECT id, '{CronRun.Trigger.schedule}' FROM {CronJob.__tablename__} WHERE id = '{job_id}'"
            f" AND (allow_overlap OR NOT EXISTS (SELECT 1 FROM {CronRun.__tablename__}"
            f" WHERE job_id = '{job_id}' AND status IN ({unfinished}))); "
            f"NOTIFY {NOTIFY_CHANNEL};"
        )


class CronRun(tk.BaseModel):
    """One execution of a job, from the moment it is queued until it finishes."""

    __tablename__ = "cron_run"

    class Status:
        queued = "queued"
        running = "running"
        succeeded = "succeeded"
        failed = "failed"

    class Trigger:
        schedule = "schedule"
        manual = "manual"

    id = sa.Column(sa.BigInteger, sa.Identity(), primary_key=True)
    job_id = sa.Column(sa.Text, sa.ForeignKey("cron_job.id", ondelete="CASCADE"), nullable=False)
    trigger = sa.Column(sa.Text, nullable=False, server_default=Trigger.schedule)
    # id of the user who queued a manual run
    triggered_by = sa.Column(sa.Text)
    status = sa.Column(sa.Text, nullable=False, server_default=Status.queued)
    created = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    started = sa.Column(sa.DateTime(timezone=True))
    finished = sa.Column(sa.DateTime(timezone=True))
    worker = sa.Column(sa.Text)
    # refreshed by the worker while the command runs, so that runs of a dead worker can be failed
    heartbeat = sa.Column(sa.DateTime(timezone=True))
    # None if the command was killed or could not start
    exit_code = sa.Column(sa.Integer)
    stdout = sa.Column(sa.Text)
    stderr = sa.Column(sa.Text)

    def dictize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "trigger": self.trigger,
            "triggered_by": self.triggered_by,
            "status": self.status,
            "created": _isoformat(self.created),
            "started": _isoformat(self.started),
            "finished": _isoformat(self.finished),
            "worker": self.worker,
            "heartbeat": _isoformat(self.heartbeat),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class CronWorker(tk.BaseModel):
    """A running `ckan cron worker`, which refreshes `last_seen` while it is alive."""

    __tablename__ = "cron_worker"

    name = sa.Column(sa.Text, primary_key=True)
    started = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    last_seen = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    # the run the worker is executing, None while it waits
    run_id = sa.Column(sa.BigInteger)

    def dictize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started": _isoformat(self.started),
            "last_seen": _isoformat(self.last_seen),
            "run_id": self.run_id,
        }


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value else None
