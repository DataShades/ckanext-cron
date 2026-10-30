from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

import ckan.plugins.toolkit as tk
from ckan import model, types
from ckan.logic import validate

from ckanext.cron import config, jobs, pg_cron
from ckanext.cron.logic import schema
from ckanext.cron.model import CronJob, CronRun

log = logging.getLogger(__name__)

MAX_RUN_LIST_LIMIT = 1000


def _user_id(context: types.Context) -> str | None:
    """Id of the user who calls the action, None for internal calls without a user."""
    user = model.User.get(context["user"]) if context.get("user") else None
    return user.id if user else None


def _validation_error(err: pg_cron.PgCronError) -> tk.ValidationError:
    if err.message.startswith("permission denied for function"):
        username = pg_cron.get_info()["username"] or "<role>"
        message = tk._("{error}. A database superuser has to run: {grant}").format(
            error=err.message, grant=pg_cron.grant_statement(username)
        )
        return tk.ValidationError({"message": [message]})

    field = "schedule" if re.match(r"^invalid schedule", err.message) else "message"
    return tk.ValidationError({field: [str(err)]})


@tk.side_effect_free
def cron_info(context: types.Context, data_dict: types.DataDict) -> dict[str, Any]:
    """Report whether pg_cron is usable, plus its version, timezone and connected role."""
    tk.check_access("cron_info", context, data_dict)

    return dict(pg_cron.get_info())


@tk.side_effect_free
def cron_job_list(context: types.Context, data_dict: types.DataDict) -> list[dict[str, Any]]:
    """List all jobs with their schedule and latest run."""
    tk.check_access("cron_job_list", context, data_dict)

    try:
        return jobs.dictize_jobs(list(model.Session.scalars(sa.select(CronJob).order_by(CronJob.name))))
    except pg_cron.PgCronError as e:
        raise _validation_error(e) from e


@tk.side_effect_free
@validate(schema.cron_job_id)
def cron_job_show(context: types.Context, data_dict: types.DataDict) -> dict[str, Any]:
    """Show a job with its schedule and latest run.

    Args:
        id (str): the job id
    """
    tk.check_access("cron_job_show", context, data_dict)

    try:
        return jobs.dictize_job(model.Session.get(CronJob, data_dict["id"]))
    except pg_cron.PgCronError as e:
        raise _validation_error(e) from e


@validate(schema.cron_job_create)
def cron_job_create(context: types.Context, data_dict: types.DataDict) -> dict[str, Any]:
    """Create a job that runs a ``ckan`` command on a schedule.

    Args:
        name (str): unique name
        command (str): arguments of the ``ckan`` CLI, such as ``harvester run``.
            A leading ``ckan`` is accepted, a config file option is not.
        schedule (str): a cron expression (``*/30 * * * *``), an interval
            (``30 seconds``) or a macro such as ``@daily``
        active (bool, optional): whether pg_cron runs the job, defaults to true
        allow_overlap (bool, optional): queue a new run even if the previous
            one is still queued or running, defaults to false
    """
    tk.check_access("cron_job_create", context, data_dict)

    user_id = _user_id(context)
    job = CronJob(
        name=data_dict["name"],
        command=data_dict["command"],
        allow_overlap=data_dict["allow_overlap"],
        created_by=user_id,
        modified_by=user_id,
    )
    model.Session.add(job)
    model.Session.flush()

    try:
        pg_cron.schedule_job(
            job.pg_cron_name,
            data_dict["schedule"],
            job.trigger_command(),
            config.ckan_database(),
            active=data_dict["active"],
        )
    except pg_cron.PgCronError as e:
        model.Session.rollback()
        raise _validation_error(e) from e

    model.Session.commit()
    log.info("Cron job %s (ckan %s) has been created by %s", job.name, job.command, context.get("user"))

    return jobs.dictize_job(job)


@validate(schema.cron_job_update)
def cron_job_update(context: types.Context, data_dict: types.DataDict) -> dict[str, Any]:
    """Change a job. Fields that are left out stay as they are.

    If the job has lost its pg_cron job, providing ``schedule`` creates it again.

    Args:
        id (str): the job id
        name (str, optional)
        command (str, optional)
        schedule (str, optional)
        active (bool, optional)
        allow_overlap (bool, optional)
    """
    tk.check_access("cron_job_update", context, data_dict)

    job = model.Session.get(CronJob, data_dict["id"])

    for field in ("name", "command", "allow_overlap"):
        if field in data_dict:
            setattr(job, field, data_dict[field])

    job.modified_by = _user_id(context)
    # `modified` changes even if only the pg_cron schedule or active flag did
    job.modified = sa.func.now()

    model.Session.flush()

    try:
        _update_schedule(job, data_dict.get("schedule"), data_dict.get("active"))
    except tk.ValidationError:
        model.Session.rollback()
        raise
    except pg_cron.PgCronError as e:
        model.Session.rollback()
        raise _validation_error(e) from e

    model.Session.commit()
    log.info("Cron job %s has been updated by %s", job.name, context.get("user"))

    return jobs.dictize_job(job)


def _update_schedule(job: CronJob, schedule: str | None, active: bool | None) -> None:
    pg_job = pg_cron.find_job(job.pg_cron_name)

    if pg_job:
        # jobs scheduled by older versions of the extension get the current trigger command
        command = job.trigger_command() if pg_job["command"] != job.trigger_command() else None

        if schedule is not None or active is not None or command is not None:
            pg_cron.alter_job(pg_job["jobid"], schedule=schedule, command=command, active=active)
        return

    if schedule is None:
        raise tk.ValidationError({"schedule": [tk._("The job is not scheduled in pg_cron, provide a schedule")]})

    pg_cron.schedule_job(
        job.pg_cron_name,
        schedule,
        job.trigger_command(),
        config.ckan_database(),
        active=True if active is None else active,
    )


@validate(schema.cron_job_id)
def cron_job_delete(context: types.Context, data_dict: types.DataDict) -> bool:
    """Delete a job, its pg_cron job and its run history.

    Args:
        id (str): the job id
    """
    tk.check_access("cron_job_delete", context, data_dict)

    job = model.Session.get(CronJob, data_dict["id"])

    try:
        if pg_job := pg_cron.find_job(job.pg_cron_name):
            pg_cron.unschedule_job(pg_job["jobid"])
    except pg_cron.PgCronError as e:
        raise _validation_error(e) from e

    model.Session.delete(job)
    model.Session.commit()
    log.info("Cron job %s has been deleted by %s", job.name, context.get("user"))

    return True


@validate(schema.cron_job_id)
def cron_job_trigger(context: types.Context, data_dict: types.DataDict) -> dict[str, Any]:
    """Queue a run of the job now, regardless of its schedule.

    Unless the job allows overlapping runs, it fails while a run of the job is
    still queued or running.

    Args:
        id (str): the job id

    Returns:
        the queued run
    """
    tk.check_access("cron_job_trigger", context, data_dict)

    job = model.Session.get(CronJob, data_dict["id"])

    if not job.allow_overlap and jobs.has_unfinished_run(job):
        raise tk.ValidationError({"id": [tk._("A run of this job is already queued or running")]})

    run = jobs.enqueue_run(job, user_id=_user_id(context))
    log.info("Cron run %s has been queued by %s", run.id, context.get("user"))

    return run.dictize()


@tk.side_effect_free
def cron_worker_list(context: types.Context, data_dict: types.DataDict) -> list[dict[str, Any]]:
    """List the running workers: those that sent a heartbeat in the last minute.

    Each one has its ``name`` (host:pid), ``started``, ``last_seen`` and the
    ``run_id`` it is executing, which is None while it waits.
    """
    tk.check_access("cron_worker_list", context, data_dict)

    return [worker.dictize() for worker in jobs.live_workers()]


@tk.side_effect_free
@validate(schema.cron_run_list)
def cron_run_list(context: types.Context, data_dict: types.DataDict) -> list[dict[str, Any]]:
    """List runs, newest first.

    Args:
        job_id (str, optional): only the runs of this job
        status (str, optional): ``queued``, ``running``, ``succeeded`` or ``failed``
        limit (int, optional): defaults to 100, at most 1000
        offset (int, optional): defaults to 0
    """
    tk.check_access("cron_run_list", context, data_dict)

    stmt = (
        sa.select(CronRun)
        .order_by(CronRun.id.desc())
        .limit(min(data_dict["limit"], MAX_RUN_LIST_LIMIT))
        .offset(data_dict["offset"])
    )

    if job_id := data_dict.get("job_id"):
        stmt = stmt.where(CronRun.job_id == job_id)

    if status := data_dict.get("status"):
        stmt = stmt.where(CronRun.status == status)

    return [run.dictize() for run in model.Session.scalars(stmt)]


@tk.side_effect_free
@validate(schema.cron_run_show)
def cron_run_show(context: types.Context, data_dict: types.DataDict) -> dict[str, Any]:
    """Show a run.

    Args:
        id (int): the run id
    """
    tk.check_access("cron_run_show", context, data_dict)

    run = model.Session.get(CronRun, data_dict["id"])

    if not run:
        raise tk.ObjectNotFound(tk._("Cron run not found"))

    return run.dictize()


@validate(schema.cron_run_purge)
def cron_run_purge(context: types.Context, data_dict: types.DataDict) -> dict[str, int]:
    """Delete finished runs that were queued more than the given number of days ago.

    pg_cron's own log of the runs (``cron.job_run_details``) is purged too,
    since pg_cron never deletes it.

    Args:
        days (int, optional): defaults to ``ckanext.cron.history_retention_days``
        job_id (str, optional): only purge the runs of this job

    Returns:
        ``{"deleted": <number of deleted runs>, "pg_cron_deleted": <number of
        deleted pg_cron log rows, or None if pg_cron could not be purged>}``
    """
    tk.check_access("cron_run_purge", context, data_dict)

    older_than = datetime.now(timezone.utc) - timedelta(days=data_dict["days"])
    stmt = sa.delete(CronRun).where(
        CronRun.created < older_than,
        CronRun.status.in_([CronRun.Status.succeeded, CronRun.Status.failed]),
    )

    if job_id := data_dict.get("job_id"):
        stmt = stmt.where(CronRun.job_id == job_id)

    deleted = model.Session.execute(stmt).rowcount
    model.Session.commit()
    log.info("%s cron runs older than %s have been purged by %s", deleted, older_than, context.get("user"))

    return {"deleted": deleted, "pg_cron_deleted": _purge_pg_cron_log(data_dict.get("job_id"), older_than)}


def _purge_pg_cron_log(job_id: str | None, older_than: datetime) -> int | None:
    try:
        if job_id:
            pg_job = pg_cron.find_job(model.Session.get(CronJob, job_id).pg_cron_name)
            pg_jobs = [pg_job] if pg_job else []
        else:
            pg_jobs = jobs.pg_cron_jobs()

        return pg_cron.purge_run_details([pg_job["jobid"] for pg_job in pg_jobs], older_than)
    except pg_cron.PgCronError as e:
        log.warning("Cannot purge the run log of pg_cron: %s", e)
        return None
