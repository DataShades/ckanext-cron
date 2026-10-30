from __future__ import annotations

import shlex
from typing import Any

import sqlalchemy as sa

import ckan.plugins.toolkit as tk
from ckan import model, types

from ckanext.cron import job_commands
from ckanext.cron.model import CronJob


def cron_job_exists(value: Any, context: types.Context) -> Any:
    if not model.Session.get(CronJob, value):
        raise tk.Invalid(tk._("Cron job not found"))

    return value


def cron_job_name_available(
    key: types.FlattenKey,
    data: types.FlattenDataDict,
    errors: types.FlattenErrorDict,
    context: types.Context,
) -> Any:
    name = data.get(key)

    if not name or errors[key]:
        return

    stmt = sa.select(CronJob.id).where(CronJob.name == name)

    if job_id := data.get(("id",)):
        stmt = stmt.where(CronJob.id != job_id)

    if model.Session.scalar(stmt):
        errors[key].append(tk._("A job with this name already exists"))


def cron_command(value: Any, context: types.Context) -> Any:
    """Normalize a `ckan` command and ensure it names a runnable command."""
    try:
        args = job_commands.parse(value)
    except job_commands.InvalidCommand as e:
        raise tk.Invalid(str(e)) from None

    return shlex.join(args)


def cron_strip(value: Any, context: types.Context) -> Any:
    return value.strip() if isinstance(value, str) else value
