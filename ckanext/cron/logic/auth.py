"""Cron jobs run ckan commands on the server, so only sysadmins may manage them."""

from __future__ import annotations

from ckan import types


def cron_manage(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return {"success": False}


def cron_info(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_job_list(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_job_show(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_job_create(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_job_update(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_job_delete(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_job_trigger(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_worker_list(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_run_list(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_run_show(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)


def cron_run_purge(context: types.Context, data_dict: types.DataDict) -> types.AuthResult:
    return cron_manage(context, data_dict)
