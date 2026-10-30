from __future__ import annotations

import json
from typing import Any

import click

import sqlalchemy as sa

import ckan.plugins.toolkit as tk
from ckan import model

from ckanext.cron import config, jobs, pg_cron
from ckanext.cron.model import CronJob
from ckanext.cron.worker import Worker

__all__ = ["cron"]


@click.group(short_help="ckan commands scheduled with pg_cron")
def cron():
    pass


@cron.command()
@click.option("--once", is_flag=True, help="Execute the queued runs and exit instead of waiting for more.")
@click.option("--poll-interval", type=int, help="Seconds between queue checks when no NOTIFY arrives.")
def worker(once: bool, poll_interval: int | None):
    """Execute the runs that pg_cron enqueues, or that are started from the UI."""
    Worker(
        config_file=tk.config["__file__"],
        poll_interval=poll_interval or config.poll_interval(),
        timeout=config.timeout(),
    ).run(once=once)


@cron.command()
@click.option("--fix", is_flag=True, help="Unschedule leftover pg_cron jobs and refresh outdated ones.")
def check(fix: bool):
    """Check that every job has its pg_cron job, and that pg_cron has no leftovers.

    Exits with status 1 if a problem remains, so it can be used for monitoring.
    """
    try:
        problems, job_count = _check(fix)
    except pg_cron.PgCronError as e:
        raise click.ClickException(str(e)) from None

    if problems:
        if not fix:
            click.echo("Run with --fix to clean up what can be fixed automatically.")
        raise click.exceptions.Exit(1)

    click.secho(f"{job_count} jobs, all scheduled in pg_cron.", fg="green")


def _check(fix: bool) -> tuple[int, int]:
    """Report problems, fixing those that can be fixed if `fix` is set. Return the number left and the number of jobs."""
    info = pg_cron.get_info()

    if not info["available"]:
        raise pg_cron.PgCronError(f"pg_cron is not available: {info['error']}")

    problems = 0

    if info["missing_privileges"]:
        problems += 1
        click.secho(f"{info['username']} cannot manage pg_cron jobs. A superuser has to run:", fg="red")
        click.echo(f"    {pg_cron.grant_statement(info['username'], info['missing_privileges'])}")

    job_list = list(model.Session.scalars(sa.select(CronJob).order_by(CronJob.name)))
    scheduled = pg_cron.find_jobs([job.pg_cron_name for job in job_list])

    for job in job_list:
        pg_job = scheduled.get(job.pg_cron_name)

        if not pg_job:
            problems += 1
            click.secho(f'"{job.name}" has no pg_cron job. Save it with a schedule to create one.', fg="red")
        elif pg_job["command"] != job.trigger_command():
            if fix:
                pg_cron.alter_job(pg_job["jobid"], command=job.trigger_command())
                click.secho(f'"{job.name}": refreshed the outdated command of pg_cron job {pg_job["jobid"]}', fg="green")
            else:
                problems += 1
                click.secho(f'"{job.name}": pg_cron job {pg_job["jobid"]} runs an outdated command', fg="yellow")

    for pg_job in jobs.orphaned_pg_cron_jobs():
        if fix:
            pg_cron.unschedule_job(pg_job["jobid"])
            click.secho(f"Unscheduled leftover pg_cron job {pg_job['jobid']} ({pg_job['jobname']})", fg="green")
        else:
            problems += 1
            click.secho(
                f"pg_cron job {pg_job['jobid']} ({pg_job['jobname']}) belongs to a job that no longer exists", fg="yellow"
            )

    return problems, len(job_list)


@cron.command("call-action")
@click.argument("action")
@click.argument("params", nargs=-1)
@click.pass_context
def call_action(ctx: click.Context, action: str, params: tuple[str, ...]):
    """Call a CKAN action as the site user and print its result as JSON.

    PARAMS are key=value pairs that make up the data dict. Values that are
    valid JSON are decoded, so `limit=10` passes a number and `id=abc` a string.

    \b
    Example:
        ckan cron call-action harvest_source_reindex id=my-source
    """
    data_dict = _parse_params(params)

    with ctx.meta["flask_app"].test_request_context():
        site_user = tk.get_action("get_site_user")({"ignore_auth": True}, {})

        try:
            result = tk.get_action(action)({"user": site_user["name"]}, data_dict)
        except KeyError:
            raise click.ClickException(f"Action {action} does not exist") from None
        except tk.ValidationError as e:
            raise click.ClickException(json.dumps(e.error_dict, default=str)) from None
        except (tk.ObjectNotFound, tk.NotAuthorized) as e:
            raise click.ClickException(f"{type(e).__name__}: {e}") from None

    click.echo(json.dumps(result, indent=2, default=str))


def _parse_params(params: tuple[str, ...]) -> dict[str, Any]:
    data_dict: dict[str, Any] = {}

    for param in params:
        key, sep, value = param.partition("=")

        if not sep or not key:
            raise click.BadParameter(f"{param!r} is not a key=value pair", param_hint="PARAMS")

        try:
            data_dict[key] = json.loads(value)
        except ValueError:
            data_dict[key] = value

    return data_dict
