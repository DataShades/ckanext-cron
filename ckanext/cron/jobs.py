"""Cron jobs together with their pg_cron schedule and their latest run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from ckan import model

from ckanext.cron import config, pg_cron
from ckanext.cron.model import LOST_AFTER, NOTIFY_CHANNEL, PG_CRON_PREFIX, CronJob, CronRun, CronWorker

# queued runs older than this mean that no worker is processing the queue
QUEUE_DELAY_WARNING = timedelta(minutes=5)


def dictize_jobs(jobs: list[CronJob]) -> list[dict[str, Any]]:
    """Dictize jobs, with the schedule from pg_cron and the latest run of each.

    `schedule` and `active` are None for a job that has no pg_cron job, and
    `trigger_error` holds the error of the latest pg_cron run if it failed to
    enqueue a CKAN run.
    """
    if not jobs:
        return []

    scheduled = pg_cron.find_jobs([job.pg_cron_name for job in jobs])
    trigger_errors = pg_cron.failed_triggers([pg_job["jobid"] for pg_job in scheduled.values()])
    last_runs = _last_runs([job.id for job in jobs])

    result = []

    for job in jobs:
        pg_job = scheduled.get(job.pg_cron_name)
        last_run = last_runs.get(job.id)

        result.append(
            {
                "id": job.id,
                "name": job.name,
                "command": job.command,
                "allow_overlap": job.allow_overlap,
                "schedule": pg_job["schedule"] if pg_job else None,
                "active": pg_job["active"] if pg_job else None,
                "pg_cron_jobid": pg_job["jobid"] if pg_job else None,
                "trigger_error": trigger_errors.get(pg_job["jobid"]) if pg_job else None,
                "last_run": last_run.dictize() if last_run else None,
                "created": job.created.isoformat(),
                "created_by": job.created_by,
                "modified": job.modified.isoformat(),
                "modified_by": job.modified_by,
            }
        )

    return result


def dictize_job(job: CronJob) -> dict[str, Any]:
    return dictize_jobs([job])[0]


def _last_runs(job_ids: list[str]) -> dict[str, CronRun]:
    stmt = (
        sa.select(CronRun)
        .where(CronRun.job_id.in_(job_ids))
        .distinct(CronRun.job_id)
        .order_by(CronRun.job_id, CronRun.id.desc())
    )
    return {run.job_id: run for run in model.Session.scalars(stmt)}


def enqueue_run(job: CronJob, trigger: str = CronRun.Trigger.manual, user_id: str | None = None) -> CronRun:
    """Queue a run of the job right away and wake up the workers."""
    run = CronRun(job_id=job.id, trigger=trigger, triggered_by=user_id)
    model.Session.add(run)
    model.Session.execute(sa.text(f"NOTIFY {NOTIFY_CHANNEL}"))
    model.Session.commit()

    return run


def pg_cron_jobs() -> list[pg_cron.PgCronJob]:
    """All pg_cron jobs that enqueue runs in this CKAN database, including leftovers of deleted jobs."""
    return pg_cron.find_jobs_by_prefix(PG_CRON_PREFIX, config.ckan_database())


def orphaned_pg_cron_jobs() -> list[pg_cron.PgCronJob]:
    """pg_cron jobs left behind by CKAN jobs that no longer exist."""
    names = {job.pg_cron_name for job in model.Session.scalars(sa.select(CronJob))}
    return [pg_job for pg_job in pg_cron_jobs() if pg_job["jobname"] not in names]


def has_unfinished_run(job: CronJob) -> bool:
    stmt = sa.select(CronRun.id).where(
        CronRun.job_id == job.id, CronRun.status.in_([CronRun.Status.queued, CronRun.Status.running])
    )
    return model.Session.scalar(stmt.limit(1)) is not None


def live_workers() -> list[CronWorker]:
    """Workers that have sent a heartbeat recently, oldest first."""
    stmt = (
        sa.select(CronWorker)
        .where(CronWorker.last_seen >= datetime.now(timezone.utc) - LOST_AFTER)
        .order_by(CronWorker.started, CronWorker.name)
    )
    return list(model.Session.scalars(stmt))


def delayed_runs() -> int:
    """Count queued runs that have waited longer than a worker would let them."""
    threshold = datetime.now(timezone.utc) - QUEUE_DELAY_WARNING
    stmt = sa.select(sa.func.count()).where(CronRun.status == CronRun.Status.queued, CronRun.created < threshold)

    return model.Session.scalar(stmt) or 0
